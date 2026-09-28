# -*- coding: utf-8 -*-
"""Страницы пульта и ответы на нажатия (htmx).

Правило, которое держит весь набор: пульт файлы фонотеки не трогает.
Решение пользователя записывается в janitor.db, а если оно что-то меняет в
фонотеке — кладётся действием в очередь, и выполняет его сторож
(apply_actions.py) на ближайшем цикле. Пульт будит его файлом wake, так что
«ближайший цикл» — это секунды, а не пять минут.
"""
import json
import mimetypes
import os
import re
import threading
import time
import traceback

import requests
from django.conf import settings
from django.http import (FileResponse, Http404, HttpResponse, HttpResponseBadRequest,
                         HttpResponseRedirect, StreamingHttpResponse)
from django.shortcuts import render
from django.views.decorators.http import require_POST

import env
import janitordb as jdb

from . import auth, data

mimetypes.add_type("audio/ogg", ".opus")
mimetypes.add_type("audio/mp4", ".m4a")
mimetypes.add_type("audio/flac", ".flac")


def toast(resp, text):
    # только ASCII: заголовок с кириллицей Django кодирует в MIME, и htmx его
    # уже не разбирает; \uXXXX внутри JSON он раскодирует сам
    resp["HX-Trigger"] = json.dumps({"toast": text})
    return resp


def nav(request):
    """Счётчики меню и состояние сторожа — для всех полных страниц."""
    if request.headers.get("HX-Request") or not request.session.get("ok"):
        return {}
    try:
        return {"navc": data.nav_counts(), "hb": data.heartbeat(), "page": request.resolver_match.url_name
                if request.resolver_match else ""}
    except Exception:
        return {"navc": {}, "hb": None}


# ---------------------------------------------------------------- сводка
def summary(request):
    return render(request, "pult/summary.html", {"s": data.summary()})


@require_POST
def run_now(request):
    what = request.POST.get("what", "nightly")
    if what not in ("nightly", "verify"):
        return HttpResponseBadRequest()
    con = data.jcon()
    data.enqueue(con, "run", {"what": what})
    text = "Ночная работа начнётся на ближайшем цикле сторожа" if what == "nightly" else "Сверка новых импортов запущена"
    return toast(HttpResponse(""), text)


# ---------------------------------------------------------------- решения
DECISION_TEXT = {
    "quarantine": "файл уйдёт в карантин", "keep": "отмечено как правильный трек",
    "keep_a": "оставлен A, B уйдёт в карантин", "keep_b": "оставлен B, A уйдёт в карантин",
    "keep_both": "оставлены обе копии", "different": "оставлены обе: разные песни или версии",
    "accept": "кандидат будет принят, теги перепишутся", "never": "больше не искать",
    "asis": "оставлено как есть", "replace": "звук заменится загруженным файлом", "dismiss": "вопрос снят",
}


def _card(request, con, rid):
    r = con.execute("SELECT * FROM reviews WHERE id=?", (rid,)).fetchone()
    if r is None:
        raise Http404
    cards = [c for c in data.review_cards(con, r["kind"], limit=100000) if c["row"]["id"] == rid]
    if not cards:
        p = data.loads(r["payload"], {}) or {}
        cards = [{"row": r, "p": p, "verify": r["key"].startswith("verify:"), "files": p.get("files") or [],
                  "exists": True, "live": {}}]
    return cards[0]


def review(request):
    tab = request.GET.get("tab", "subs")
    kind = {"subs": "substitution", "dups": "duplicate", "asis": "asis"}.get(tab, "substitution")
    con = data.jcon()
    return render(request, "pult/review.html", {
        "tab": tab, "cards": data.review_cards(con, kind), "counts": data.review_counts(con),
        "ref_ok": float(jdb.setting(con, "ref_ok")), "ref_bad": float(jdb.setting(con, "ref_bad"))})


@require_POST
def review_decide(request, rid):
    decision = request.POST.get("decision", "")
    con = data.jcon()
    r = con.execute("SELECT * FROM reviews WHERE id=?", (rid,)).fetchone()
    if r is None or r["status"] != "open":
        return toast(HttpResponse(""), "Этот вопрос уже решён")
    p = data.loads(r["payload"], {}) or {}
    kind = r["kind"]
    files = p.get("files") or []

    if kind == "substitution" and r["key"].startswith("verify:"):
        if decision == "quarantine":
            data.enqueue(con, "quarantine", {"item_id": p.get("item_id"), "reason": "substitution",
                                             "similarity": p.get("similarity")}, rid)
        elif decision not in ("keep", "dismiss"):
            return HttpResponseBadRequest()
    elif kind in ("substitution", "duplicate"):
        if decision in ("keep_a", "keep_b") and len(files) == 2:
            keep, drop = (files[0], files[1]) if decision == "keep_a" else (files[1], files[0])
            data.enqueue(con, "quarantine", {"item_id": drop.get("item_id"), "similarity": p.get("similarity"),
                                             "reason": "substitution" if kind == "substitution" else "duplicate",
                                             "kept": keep.get("path")}, rid)
        elif decision not in ("keep_both", "different", "dismiss"):
            return HttpResponseBadRequest()
    elif kind == "asis":
        if decision == "accept":
            cand = p.get("candidate") or {}
            if not cand.get("track_id"):
                return toast(HttpResponse(""), "У кандидата нет идентификатора — принять нечего")
            data.enqueue(con, "accept", {"item_id": p.get("item_id"), "track_id": cand["track_id"]}, rid)
        elif decision not in ("never", "asis", "dismiss"):
            return HttpResponseBadRequest()
    else:
        return HttpResponseBadRequest()

    con.execute("UPDATE reviews SET status='resolved', decision=?, decided_at=? WHERE id=?",
                (decision, jdb.now(), rid))
    c = _card(request, con, rid)
    tpl = "pult/_asis_row.html" if kind == "asis" else "pult/_card.html"
    return toast(render(request, tpl, {"c": c, "resolved_text": DECISION_TEXT.get(decision, decision)}),
                 "Решено: " + DECISION_TEXT.get(decision, decision))


@require_POST
def review_undo(request, rid):
    con = data.jcon()
    started = con.execute("SELECT id FROM actions WHERE review_id=? AND status IN ('running','done')", (rid,)).fetchone()
    if started is not None:
        return toast(HttpResponse(status=204), "Уже выполнено — вернуть можно в журнале или карантине")
    con.execute("UPDATE actions SET status='cancelled', finished_at=? WHERE review_id=? AND status='pending'",
                (jdb.now(), rid))
    con.execute("UPDATE reviews SET status='open', decision=NULL, decided_at=NULL WHERE id=?", (rid,))
    c = _card(request, con, rid)
    return toast(render(request, "pult/_card.html", {"c": c, "ref_ok": float(jdb.setting(con, "ref_ok")),
                                                     "ref_bad": float(jdb.setting(con, "ref_bad"))}),
                 "Решение отменено")


def _save_upload(f, prefix):
    if f.size > settings.UPLOAD_LIMIT:
        raise ValueError("файл больше %d МБ" % (settings.UPLOAD_LIMIT // 1048576))
    os.makedirs(env.UPLOADS_DIR, exist_ok=True)
    ext = os.path.splitext(f.name)[1].lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,5}", ext or ""):
        ext = ""
    path = os.path.join(env.UPLOADS_DIR, "%s-%d%s" % (prefix, int(time.time() * 1000), ext))
    with open(path, "wb") as out:
        for chunk in f.chunks():
            out.write(chunk)
    return path


AUDIO_EXT = (".mp3", ".flac", ".m4a", ".opus", ".ogg", ".wav", ".aac")


@require_POST
def review_replace(request, rid):
    con = data.jcon()
    r = con.execute("SELECT * FROM reviews WHERE id=?", (rid,)).fetchone()
    f = request.FILES.get("file")
    if r is None or f is None:
        return HttpResponseBadRequest()
    if os.path.splitext(f.name)[1].lower() not in AUDIO_EXT:
        return toast(HttpResponse(status=204), "Нужен звуковой файл: mp3, flac, m4a, opus, ogg, wav")
    p = data.loads(r["payload"], {}) or {}
    item_id = p.get("item_id") or ((p.get("files") or [{}])[0].get("item_id"))
    try:
        path = _save_upload(f, "replace-%d" % rid)
    except ValueError as e:
        return toast(HttpResponse(status=204), str(e))
    data.enqueue(con, "replace", {"item_id": item_id, "file": path}, rid)
    con.execute("UPDATE reviews SET status='resolved', decision='replace', decided_at=? WHERE id=?", (jdb.now(), rid))
    c = _card(request, con, rid)
    return toast(render(request, "pult/_card.html", {"c": c, "resolved_text": DECISION_TEXT["replace"]}),
                 "Файл принят: сторож заменит звук и перенесёт теги")


# ---------------------------------------------------------------- новинки
def releases(request):
    f = request.GET.get("f", "all")
    con = data.jcon()
    return render(request, "pult/releases.html", {
        "f": f, "rels": data.releases(con, f), "counts": data.release_counts(con),
        "auto": jdb.setting(con, "auto_download") == "1",
        "artists_n": con.execute("SELECT count(*) FROM artists WHERE follow=1 AND excluded=0 AND deezer_id IS NOT NULL").fetchone()[0]})


def _rel_card(request, con, rel_id, text):
    rels = [x for x in data.releases(con, None) if x["row"]["id"] == rel_id]
    if not rels:
        return toast(HttpResponse(""), text)
    return toast(render(request, "pult/_release.html", {"x": rels[0]}), text)


@require_POST
def release_get(request, rel_id):
    con = data.jcon()
    data.enqueue(con, "download", {"release_id": rel_id})
    con.execute("UPDATE releases SET status='queued', decided_at=? WHERE id=?", (jdb.now(), rel_id))
    return _rel_card(request, con, rel_id, "В очереди на скачивание: downtify, потом сверка по превью")


@require_POST
def release_skip(request, rel_id):
    con = data.jcon()
    con.execute("UPDATE releases SET status='skipped', decided_at=? WHERE id=?", (jdb.now(), rel_id))
    return _rel_card(request, con, rel_id, "Релиз пропущен, больше не предложу")


@require_POST
def set_flag(request):
    key = request.POST.get("key")
    if key not in ("auto_download", "follow_enabled"):
        return HttpResponseBadRequest()
    on = request.POST.get("value") in ("1", "on", "true")
    con = data.jcon()
    jdb.set_setting(con, key, "1" if on else "0")
    text = {"auto_download": ("Новинки будут качаться сразу", "Новинки ждут подтверждения"),
            "follow_enabled": ("Слежение за новинками включено", "Слежение за новинками выключено")}[key]
    return toast(HttpResponse(status=204), text[0] if on else text[1])


# ---------------------------------------------------------------- исполнители
def artists(request):
    f = request.GET.get("f", "all")
    con = data.jcon()
    return render(request, "pult/artists.html", {
        "f": f, "rows": data.artists(con, f), "counts": data.artist_counts(con),
        "min_tracks": jdb.setting(con, "follow_min_tracks")})


def _library():
    import discography as disco
    return disco.Library()


def _search(q):
    import discography as disco
    return disco.search_artists(q, _library())


def artists_search(request):
    q = request.GET.get("q", "").strip()
    if not q:
        return HttpResponse("")
    try:
        cands = _search(q)
    except Exception as e:
        return HttpResponse('<p class="muted">Deezer не ответил: %s</p>' % str(e)[:80])
    return render(request, "pult/_cands.html", {"cands": cands, "q": q, "mode": request.GET.get("mode", "follow")})


def _ensure_artist(con, deezer_id, name, picture, follow):
    """Строка исполнителя под этот Deezer-ID: найти или завести."""
    row = con.execute("SELECT * FROM artists WHERE deezer_id=?", (str(deezer_id),)).fetchone()
    if row is None:
        import discography as disco
        for r in con.execute("SELECT * FROM artists WHERE deezer_id IS NULL"):
            if disco.name_keys(r["name"]) & disco.name_keys(name):
                row = r
                break
    if row is None:
        aid = con.execute("INSERT INTO artists(name, source, follow, deezer_id, deezer_name, picture, created_at) "
                          "VALUES (?, 'manual', ?, ?, ?, ?, ?)",
                          (name, 1 if follow else 0, str(deezer_id), name, picture, jdb.now())).lastrowid
        return aid
    con.execute("UPDATE artists SET deezer_id=?, deezer_name=?, picture=COALESCE(?, picture), attention=NULL, info=NULL"
                "%s WHERE id=?" % (", follow=1, excluded=0" if follow else ""),
                (str(deezer_id), name, picture, row["id"]))
    return row["id"]


@require_POST
def artists_add(request):
    con = data.jcon()
    did, name = request.POST.get("deezer_id"), request.POST.get("name", "").strip()
    if not did or not name:
        return HttpResponseBadRequest()
    _ensure_artist(con, did, name, request.POST.get("picture") or None, follow=True)
    resp = toast(HttpResponse(""), "%s — в списке, новинки проверятся ночью" % name)
    resp["HX-Refresh"] = "true"
    return resp


@require_POST
def artist_follow(request, aid):
    con = data.jcon()
    on = request.POST.get("value") in ("1", "on", "true")
    con.execute("UPDATE artists SET follow=?, excluded=? WHERE id=?", (1 if on else 0, 0 if on else 1, aid))
    name = con.execute("SELECT name FROM artists WHERE id=?", (aid,)).fetchone()
    return toast(HttpResponse(status=204), "%s: %s" % (name["name"] if name else "", "слежу" if on else "не слежу"))


@require_POST
def artist_pick(request, aid):
    con = data.jcon()
    did, name = request.POST.get("deezer_id"), request.POST.get("name", "")
    other = con.execute("SELECT name FROM artists WHERE deezer_id=? AND id!=?", (did, aid)).fetchone()
    if other is not None:
        return toast(HttpResponse(status=204), "Этот исполнитель уже в списке как «%s»" % other["name"])
    con.execute("UPDATE artists SET deezer_id=?, deezer_name=?, picture=?, attention=NULL, info=NULL WHERE id=?",
                (did, name, request.POST.get("picture") or None, aid))
    resp = toast(HttpResponse(""), "Выбран %s" % name)
    resp["HX-Refresh"] = "true"
    return resp


# ---------------------------------------------------------------- дискография
def discography(request):
    con = data.jcon()
    aid = request.GET.get("artist")
    ctx = {"q": request.GET.get("q", "")}
    if aid and aid.isdigit():
        flags = {k: request.GET.get(k, "1" if k != "versions" else "0") == "1" for k in ("albums", "singles", "versions")}
        ctx["d"] = data.discography(con, int(aid), **flags)
        ctx["jobs_running"] = con.execute("SELECT id FROM jobs WHERE status='running' AND json_extract(params, '$.artist_id')=?",
                                          (int(aid),)).fetchone()
    ctx["recent"] = con.execute(
        "SELECT a.id, a.name, count(r.id) n FROM artists a JOIN releases r ON r.artist_id=a.id "
        "WHERE r.tracks_total IS NOT NULL GROUP BY a.id ORDER BY max(r.id) DESC LIMIT 8").fetchall()
    return render(request, "pult/discography.html", ctx)


def _run_job(job_id, artist_id, deezer_id, name):
    con = data.jcon()
    try:
        import discography as disco
        lib = disco.Library()
        ok_t = float(jdb.setting(con, "ref_ok"))

        def progress(n, total, title):
            con.execute("UPDATE jobs SET progress=? WHERE id=?", ("релиз %d из %d · %s" % (n, total, title), job_id))

        rels = disco.resolve(deezer_id, name, lib, ok_t=ok_t, progress=progress)
        disco.store(con, artist_id, rels, "known")
        con.execute("UPDATE jobs SET status='done', finished_at=?, result=? WHERE id=?",
                    (jdb.now(), json.dumps({"releases": len(rels)}), job_id))
    except Exception as e:
        traceback.print_exc()
        con.execute("UPDATE jobs SET status='failed', finished_at=?, result=? WHERE id=?",
                    (jdb.now(), str(e)[:400], job_id))


@require_POST
def disco_start(request):
    con = data.jcon()
    did, name = request.POST.get("deezer_id"), request.POST.get("name", "").strip()
    if not did or not name:
        return HttpResponseBadRequest()
    aid = _ensure_artist(con, did, name, request.POST.get("picture") or None, follow=False)
    job_id = con.execute("INSERT INTO jobs(kind, params, status, progress, started_at) VALUES "
                         "('discography', ?, 'running', 'получаю список релизов', ?)",
                         (json.dumps({"artist_id": aid, "deezer_id": did, "name": name}, ensure_ascii=False),
                          jdb.now())).lastrowid
    threading.Thread(target=_run_job, args=(job_id, aid, did, name), daemon=True).start()
    return render(request, "pult/_job.html", {"job": data.job(con, job_id), "name": name})


def disco_job(request, job_id):
    con = data.jcon()
    j = data.job(con, job_id)
    if j is None:
        raise Http404
    if j["status"] == "done":
        resp = HttpResponse("")
        resp["HX-Redirect"] = "/discography/?artist=%d" % data.loads(j["params"], {}).get("artist_id", 0)
        return resp
    p = data.loads(j["params"], {}) or {}
    return render(request, "pult/_job.html", {"job": j, "name": p.get("name", "")})


@require_POST
def disco_download(request, aid):
    con = data.jcon()
    versions = request.POST.get("versions") == "1"
    ids = [int(x) for x in request.POST.getlist("rel") if x.isdigit()]
    if not ids:
        return toast(HttpResponse(status=204), "Не выбрано ни одного релиза")
    n = 0
    for rid in ids:
        rel = con.execute("SELECT id FROM releases WHERE id=? AND artist_id=?", (rid, aid)).fetchone()
        if rel is None:
            continue
        if versions:
            con.execute("UPDATE release_tracks SET decision='get' WHERE release_id=? AND verdict='version' "
                        "AND decision IS NULL", (rid,))
        data.enqueue(con, "download", {"release_id": rid})
        con.execute("UPDATE releases SET status='queued', decided_at=? WHERE id=?", (jdb.now(), rid))
        n += 1
    resp = toast(HttpResponse(""), "В очередь на скачивание: %d %s" % (n, "релиз" if n == 1 else "релиза" if n < 5 else "релизов"))
    resp["HX-Redirect"] = "/releases/?f=work"
    return resp


@require_POST
def disco_track(request, tid):
    decision = request.POST.get("decision")
    if decision not in ("get", "skip"):
        return HttpResponseBadRequest()
    con = data.jcon()
    con.execute("UPDATE release_tracks SET decision=? WHERE id=?", (decision, tid))
    return toast(HttpResponse(""), "Будет скачан отдельно" if decision == "get" else "Отмечено: уже есть")


# ---------------------------------------------------------------- обложки
def covers(request):
    con = data.jcon()
    lib = data.library_stats()
    missing = jdb.meta_get(con, "art_missing")
    return render(request, "pult/covers.html", {
        "cards": data.cover_cards(con), "recent": data.recent_covers(con),
        "art_pct": round(100.0 * (lib["tracks"] - int(missing)) / lib["tracks"], 1) if missing and lib["tracks"] else None,
        "missing": missing})


@require_POST
def cover_upload(request, rid):
    from PIL import Image, UnidentifiedImageError
    con = data.jcon()
    r = con.execute("SELECT * FROM reviews WHERE id=? AND kind='cover'", (rid,)).fetchone()
    f = request.FILES.get("image")
    if r is None or f is None:
        return HttpResponseBadRequest()
    try:
        img = Image.open(f)
        img.load()
    except (UnidentifiedImageError, OSError):
        return toast(HttpResponse(status=204), "Это не картинка: нужен JPG, PNG или WebP")
    img = img.convert("RGB")
    img.thumbnail((1500, 1500))
    os.makedirs(env.UPLOADS_DIR, exist_ok=True)
    path = os.path.join(env.UPLOADS_DIR, "cover-%d-%d.jpg" % (rid, int(time.time())))
    img.save(path, "JPEG", quality=92)
    p = data.loads(r["payload"], {}) or {}
    data.enqueue(con, "set_cover", {"item_ids": p.get("item_ids") or [], "image": path}, rid)
    con.execute("UPDATE reviews SET status='resolved', decision='uploaded', decided_at=? WHERE id=?", (jdb.now(), rid))
    return toast(render(request, "pult/_cover_done.html", {"p": p, "w": img.width, "h": img.height}),
                 "Обложка ляжет в %d %s" % (len(p.get("item_ids") or []), "файл" if len(p.get("item_ids") or []) == 1 else "файлов"))


# ---------------------------------------------------------------- карантин
def quarantine(request):
    f = request.GET.get("f", "all")
    reason = {"dup": "duplicate", "sub": "substitution", "broken": "broken", "failed": "import_failed"}.get(f)
    offset = int(request.GET.get("offset", 0) or 0)
    con = data.jcon()
    rows = data.quarantine(con, reason, offset)
    ctx = {"f": f, "rows": rows, "sum": data.quarantine_summary(con), "next": offset + len(rows) if len(rows) >= 150 else None}
    if request.headers.get("HX-Request") and offset:
        return render(request, "pult/_q_rows.html", ctx)
    return render(request, "pult/quarantine.html", ctx)


@require_POST
def q_restore(request, qid):
    con = data.jcon()
    r = con.execute("SELECT * FROM quarantine WHERE id=?", (qid,)).fetchone()
    if r is None or r["status"] != "held":
        return toast(HttpResponse(status=204), "Этого файла в карантине уже нет")
    if r["reason"] == "broken":
        return toast(HttpResponse(status=204), "Битый файл возвращать бессмысленно — его надо перекачать")
    from_incoming = str(r["original_path"]).startswith(env.INCOMING_DIR + "/")
    data.enqueue(con, "import" if from_incoming else "restore", {"quarantine_id": qid})
    return toast(HttpResponse('<span class="pill info">возвращается</span>'),
                 "Импортируется рядом с имеющейся копией" if from_incoming else "Файл вернётся на прежнее место")


# ---------------------------------------------------------------- журнал
def journal(request):
    f = request.GET.get("f", "all")
    offset = int(request.GET.get("offset", 0) or 0)
    con = data.jcon()
    rows = data.journal(con, f, offset)
    ctx = {"f": f, "rows": rows, "next": offset + len(rows) if len(rows) >= 100 else None}
    if request.headers.get("HX-Request") and offset:
        return render(request, "pult/_j_rows.html", ctx)
    return render(request, "pult/journal.html", ctx)


@require_POST
def j_rollback(request, eid):
    con = data.jcon()
    data.enqueue(con, "rollback", {"event_id": eid})
    return toast(HttpResponse('<span class="pill info">откатывается</span>'), "Откат поставлен в очередь сторожа")


# ---------------------------------------------------------------- настройки
PCT = "pct"
SETTINGS = {
    "nightly_hour": (int, 0, 23), "interval": (int, 30, 3600),
    "fp_same": (PCT, 50, 99), "fp_same_short": (PCT, 50, 99), "fp_ask": (PCT, 40, 95),
    "ref_ok": (PCT, 60, 99), "ref_bad": (PCT, 30, 90),
    "dupes_days": (int, 1, 90), "junk_days": (int, 1, 30), "verify_limit": (int, 0, 5000),
    "follow_min_tracks": (int, 1, 200), "follow_new_days": (int, 7, 365),
}


def settings_view(request):
    con = data.jcon()
    vals = {}
    for k, (kind, lo, hi) in SETTINGS.items():
        v = jdb.setting(con, k)
        vals[k] = round(float(v) * 100) if kind == PCT else v
    return render(request, "pult/settings.html", {
        "v": vals, "follow_enabled": jdb.setting(con, "follow_enabled") == "1",
        "auto_download": jdb.setting(con, "auto_download") == "1",
        "env": [("MUSIC_DIR", env.MUSIC_DIR), ("INCOMING_DIR", env.INCOMING_DIR), ("RESIDUE_DIR", env.RESIDUE_DIR),
                ("CONFIG_DIR", env.CONFIG_DIR), ("JANITOR_DIR", env.JANITOR_DIR),
                ("LOOSE_DIRS", ", ".join(p.strip("/") for p in env.LOOSE_DIRS)),
                ("SPOTIFY_DAILY", env.SPOTIFY_DAILY), ("DOWNTIFY_URL", env.DOWNTIFY_URL)]})


@require_POST
def settings_set(request):
    key = request.POST.get("key")
    if key not in SETTINGS:
        return HttpResponseBadRequest()
    kind, lo, hi = SETTINGS[key]
    try:
        v = int(float(request.POST.get("value", "")))
    except ValueError:
        return toast(HttpResponse(status=204), "Нужно число")
    if not lo <= v <= hi:
        return toast(HttpResponse(status=204), "Допустимо от %d до %d" % (lo, hi))
    con = data.jcon()
    jdb.set_setting(con, key, "%.2f" % (v / 100.0) if kind == PCT else str(v))
    return toast(HttpResponse(status=204), "Сохранено")


# ---------------------------------------------------------------- звук
def _stream(request, path):
    size = os.path.getsize(path)
    ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
    m = re.match(r"bytes=(\d*)-(\d*)", request.META.get("HTTP_RANGE", ""))
    if m and (m.group(1) or m.group(2)):
        if m.group(1):
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else size - 1
        else:
            start, end = max(0, size - int(m.group(2))), size - 1
        end = min(end, size - 1)
        if start > end:
            resp = HttpResponse(status=416)
            resp["Content-Range"] = "bytes */%d" % size
            return resp
        fh = open(path, "rb")
        fh.seek(start)

        def body(left=end - start + 1):
            try:
                while left > 0:
                    chunk = fh.read(min(262144, left))
                    if not chunk:
                        break
                    left -= len(chunk)
                    yield chunk
            finally:
                fh.close()

        resp = StreamingHttpResponse(body(), status=206, content_type=ctype)
        resp["Content-Range"] = "bytes %d-%d/%d" % (start, end, size)
        resp["Content-Length"] = str(end - start + 1)
    else:
        resp = FileResponse(open(path, "rb"), content_type=ctype)
        resp["Content-Length"] = str(size)
    resp["Accept-Ranges"] = "bytes"
    resp["Cache-Control"] = "private, max-age=600"
    return resp


def audio_item(request, iid):
    l = data.lcon()
    try:
        r = l.execute("SELECT path FROM items WHERE id=?", (iid,)).fetchone()
    finally:
        l.close()
    if r is None:
        raise Http404
    path = data.abspath(r["path"])
    if not data.inside(path, env.MUSIC_DIR) or not os.path.isfile(path):
        raise Http404
    return _stream(request, path)


def audio_quarantine(request, qid):
    con = data.jcon()
    r = con.execute("SELECT path FROM quarantine WHERE id=?", (qid,)).fetchone()
    if r is None or not data.inside(r["path"], env.RESIDUE_DIR) or not os.path.isfile(r["path"]):
        raise Http404
    return _stream(request, r["path"])


def _deezer_preview(track_id):
    try:
        j = requests.get("https://api.deezer.com/track/%s" % int(track_id), timeout=20).json()
        return j.get("preview")
    except Exception:
        return None


def audio_ref(request, rid):
    """Эталон для карточки подмены: превью Spotify или свежая ссылка Deezer
    (у Deezer ссылки на превью живут недолго, хранить их бессмысленно)."""
    con = data.jcon()
    r = con.execute("SELECT payload FROM reviews WHERE id=?", (rid,)).fetchone()
    ref = (data.loads(r["payload"], {}) or {}).get("ref") if r else None
    if not ref:
        raise Http404
    url = _deezer_preview(ref.get("id")) if ref.get("provider") == "deezer" else ref.get("url")
    if not url or not url.startswith("https://"):
        raise Http404
    return HttpResponseRedirect(url)


def audio_deezer(request, track_id):
    url = _deezer_preview(track_id)
    if not url or not url.startswith("https://"):
        raise Http404
    return HttpResponseRedirect(url)
