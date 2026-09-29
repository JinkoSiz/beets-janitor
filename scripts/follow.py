#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Новинки исполнителей.

Раз в сутки у каждого отслеживаемого исполнителя смотрим список релизов в
Deezer. Что появилось с прошлого раза и вышло недавно, разбираем против
фонотеки (discography.py) и кладём в «Новинки» пульта. При auto_download=1
то, что решено скачать, сразу уходит в очередь на скачивание.

Список исполнителей собирается сам — из фонотеки, у кого не меньше
follow_min_tracks дорожек, — плюс добавленные в пульте вручную.
Исключённые в пульте пропускаются.

Кто есть кто в Deezer, решает пересечение с фонотекой: поиск по имени там
путается. Если однозначного кандидата нет, исполнитель помечается
«уточнить», и выбор делается в пульте.

При первой проверке исполнителя в новинки идёт только свежее — не старше
follow_new_days (60 дней); качается оно, как и любая новинка, по переключателю
auto_download. Остальное запоминается как известное, иначе в «Новинки» разом
высыпалась бы вся дискография. Недостающее из старого — это «Дискография».

  follow.py [--dry] [--artist ИМЯ]
  follow.py --arrivals     только отметить скачанные релизы, доехавшие до фонотеки
  follow.py --catch-up     разово: свежие релизы, которые прежняя версия при
                           первой проверке записала известными, — в новинки
"""
import datetime
import json
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import discography as disco  # noqa: E402
import janitordb as jdb  # noqa: E402

DRY = "--dry" in sys.argv
RESOLVE_PER_RUN = 40      # столько новых исполнителей опознаём в Deezer за ночь
NEW_DAYS = 60             # релиз старше — не новинка, а поздно добавленный старый
ARRIVE_HOURS = 6          # скачанное не доехало до фонотеки за столько — «не скачалось»
VA = {"various artists", "various", "va", "сборник", "разные исполнители", "v a"}


def parse_ts(s):
    try:
        return datetime.datetime.fromisoformat(str(s)[:19])
    except ValueError:
        return None


def owner(it):
    """Чей это трек: исполнитель альбома, если он не «сборник», иначе исполнитель трека."""
    aa = it["albumartist"]
    if aa and disco.norm_name(aa) not in VA:
        return disco.primary_artist(aa)
    return disco.primary_artist(it["artist"])


def sync_artists(con, lib, min_tracks):
    """Исполнители из фонотеки: новых заводим, у известных обновляем счётчик."""
    count, spelled = Counter(), defaultdict(Counter)
    for it in lib.items:
        name = owner(it)
        k = disco.norm_name(name)
        if not k or k in VA:
            continue
        count[k] += 1
        spelled[k][name] += 1
    existing = {}
    for r in con.execute("SELECT id, name FROM artists"):
        existing.setdefault(disco.norm_name(r["name"]), r["id"])
    added = 0
    for k, n in count.items():
        if k in existing:
            if not DRY:
                con.execute("UPDATE artists SET library_tracks=? WHERE id=?", (n, existing[k]))
        elif n >= min_tracks:
            added += 1
            if not DRY:
                con.execute("INSERT INTO artists(name, source, library_tracks, created_at) VALUES (?, 'auto', ?, ?)",
                            (spelled[k].most_common(1)[0][0], n, jdb.now()))
    return added


def resolve_ids(con, lib, only=None):
    """Опознать исполнителей в Deezer. Неоднозначных — пометить для пульта."""
    q = ("SELECT * FROM artists WHERE deezer_id IS NULL AND excluded=0 AND follow=1 AND attention IS NULL "
         "ORDER BY library_tracks DESC")
    rows = con.execute(q).fetchall()
    if only:
        rows = [r for r in rows if disco.name_keys(r["name"]) & disco.name_keys(only)]
    rows = rows[:RESOLVE_PER_RUN]
    done = unclear = 0
    for a in rows:
        cands = disco.search_artists(a["name"], lib)
        best = disco.pick_artist(cands)
        if best is not None:
            taken = con.execute("SELECT name FROM artists WHERE deezer_id=?", (str(best["id"]),)).fetchone()
            if taken is not None:
                # «ЛСП» и «LSP» — один исполнитель, записанный в фонотеке по-разному
                attention, info = "тот же исполнитель, что и «%s»" % taken["name"], None
            else:
                print("   = %-28s -> Deezer %s «%s» (пересечение %d)"
                      % (a["name"][:28], best["id"], best["name"], best["overlap"]))
                if not DRY:
                    con.execute("UPDATE artists SET deezer_id=?, deezer_name=?, picture=?, info=NULL WHERE id=?",
                                (str(best["id"]), best["name"], best["picture"], a["id"]))
                done += 1
                continue
        elif not cands:
            attention, info = "нет в Deezer", None
        else:
            exact = [c for c in cands if c["exact"]]
            if not exact:
                attention = "в Deezer не нашёлся, есть похожие"
            elif not any(c["overlap"] for c in exact):
                attention = "в Deezer тёзка без общих треков"
            else:
                attention = "несколько кандидатов"
            info = json.dumps(cands[:5], ensure_ascii=False)
        print("   ? %-28s %s" % (a["name"][:28], attention))
        unclear += 1
        if not DRY:
            con.execute("UPDATE artists SET attention=?, info=? WHERE id=?", (attention, info, a["id"]))
    return done, unclear


def check(con, a, lib, ok_t, auto):
    """Релизы одного исполнителя. Вернуть (новинок, в очередь)."""
    today = datetime.date.today().isoformat()
    # анонс с датой из будущего не записываем вовсе: иначе в день выхода он
    # был бы уже «известным» и в новинки не попал
    rels = [r for r in disco.releases_of(a["deezer_id"]) if (r.get("release_date") or "") <= today]
    if not rels:
        return 0, 0
    known = {r["provider_id"] for r in con.execute(
        "SELECT provider_id FROM releases WHERE provider='deezer' AND artist_id=?", (a["id"],))}
    fresh = [r for r in rels if str(r["id"]) not in known]
    latest = max((r.get("release_date") or "") for r in rels)
    days = int(jdb.setting(con, "follow_new_days", NEW_DAYS))
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    first = a["last_checked"] is None
    # свежее (не старше follow_new_days) — в новинки и при первой проверке;
    # качается по переключателю auto_download, как любая новинка
    new = [r for r in fresh if (r.get("release_date") or "") >= cutoff]
    old = [r for r in fresh if r not in new]

    queued = 0
    if not DRY:
        disco.store(con, a["id"], [disco.release_row(r) for r in old], "known")
    if new:
        resolved = disco.resolve(a["deezer_id"], a["deezer_name"] or a["name"], lib, ok_t=ok_t, releases=new)
        for r in resolved:
            c = r["counts"]
            print("   + %-24s %-10s %-34s есть %d, версий %d, спросить %d, скачать %d"
                  % (a["name"][:24], r["release_date"], str(r["title"])[:34], c.get("have", 0) + c.get("dup", 0),
                     c.get("version", 0), c.get("ask", 0), c.get("get", 0)))
        if not DRY:
            ids = disco.store(con, a["id"], resolved,
                              lambda r: "in_library" if not r["counts"].get("get") and not r["counts"].get("ask")
                              else "new")
            if auto:
                for r in resolved:
                    if r["counts"].get("get"):
                        jdb.enqueue(con, "download", {"release_id": ids[r["provider_id"]]})
                        con.execute("UPDATE releases SET status='queued' WHERE id=?", (ids[r["provider_id"]],))
                        queued += 1
    if not DRY:
        con.execute("UPDATE artists SET last_checked=?, last_release_date=? WHERE id=?",
                    (jdb.now(), latest or None, a["id"]))
    return len(new), queued


def catch_up(con, lib, ok_t):
    """Разово: свежие релизы, которые первая проверка записала известными.

    Прежняя версия при первой проверке исполнителя запоминала всё подряд, и
    вышедшее за последние недели в новинки не попадало. Здесь такие релизы
    разбираются против фонотеки: чего нет — «ждут решения», всё есть — «в
    фонотеке». Сами не качаются. Анонсы с датой из будущего убираются, чтобы
    в день выхода прийти новинками.
    """
    today = datetime.date.today().isoformat()
    days = int(jdb.setting(con, "follow_new_days", NEW_DAYS))
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    total = waiting = 0
    for a in con.execute("SELECT * FROM artists WHERE deezer_id IS NOT NULL AND follow=1 AND excluded=0").fetchall():
        rows = con.execute(
            "SELECT * FROM releases WHERE artist_id=? AND status='known' AND tracks_total IS NULL "
            "AND release_date >= ? AND release_date <= ?", (a["id"], cutoff, today)).fetchall()
        if not rows:
            continue
        rels = [{"id": r["provider_id"], "title": r["title"], "record_type": r["type"],
                 "release_date": r["release_date"], "cover_medium": r["cover"], "link": r["link"]} for r in rows]
        resolved = disco.resolve(a["deezer_id"], a["deezer_name"] or a["name"], lib, ok_t=ok_t, releases=rels)
        ids = disco.store(con, a["id"], resolved, "new")
        for r in resolved:
            c = r["counts"]
            st = "in_library" if not c.get("get") and not c.get("ask") else "new"
            con.execute("UPDATE releases SET status=? WHERE id=?", (st, ids[r["provider_id"]]))
            total += 1
            waiting += st == "new"
            print("   %s %-22s %-10s %-34s скачать %d%s" % ("+" if st == "new" else "=", a["name"][:22],
                  r["release_date"], str(r["title"])[:34], c.get("get", 0), "  фит" if r.get("feat") else ""))
    gone = con.execute("DELETE FROM releases WHERE status='known' AND release_date > ?", (today,)).rowcount
    print("свежих релизов разобрано: %d, ждут решения: %d, уже в фонотеке: %d; анонсов из будущего убрано: %d"
          % (total, waiting, total - waiting, gone))


def not_arrived(lib, rel, tracks):
    """Дорожки релиза, которых в фонотеке всё ещё нет.

    Доехавшую узнаём по id трека Spotify (downtify качает по нему, beets
    пишет его в mb_trackid) или по ISRC. Иначе — по названию и длине у того
    же исполнителя или в альбоме с тем же названием. Имя исполнителя в Spotify
    бывает другим («Endspiel» у «Эндшпиля», «Pasha Technique» у «Паши
    Техника»), его берём с дорожек, узнанных по id. Название сверяем и без
    хвоста « - …»: так Spotify пишет то, что у Deezer в скобках.
    """
    names = disco.name_keys(rel["artist"]) | disco.name_keys(rel["deezer_name"])
    rest = []
    for t in tracks:
        info = jdb.loads(t["info"], {}) or {}
        names |= disco.name_keys(info.get("artist"))
        it = lib.by_track_id.get(info.get("spotify_id") or "") or lib.by_isrc.get(t["isrc"] or "")
        if it is None:
            rest.append(t)
            continue
        for n in disco.artist_names(it["artist"]) + disco.artist_names(it["albumartist"]):
            names |= disco.name_keys(n)
    album = disco.title_keys(rel["title"])

    def by_name(t):
        for it in lib.titled(t["title"]):
            if abs(it["length"] - (t["duration"] or 0)) > disco.LEN_SAME:
                continue
            if disco.title_keys(it["album"]) & album:
                return True
            if any(disco.name_keys(n) & names for n in disco.artist_names(it["artist"]) + disco.artist_names(it["albumartist"])):
                return True
        return False

    return [t for t in rest if not by_name(t)]


def not_in_spotify(t):
    """Дорожку искали в Spotify и не нашли: downtify её не качал и не скачает."""
    info = jdb.loads(t["info"], {}) or {}
    return "spotify_id" in info and not info["spotify_id"]


def titles(tracks):
    names = ["«%s»" % t["title"] for t in tracks[:4]]
    return ", ".join(names) + (" и ещё %d" % (len(tracks) - 4) if len(tracks) > 4 else "")


def recheck_downloads(con, lib):
    """Скачанное доехало до фонотеки? Тогда релиз — «в фонотеке».

    Не доехавшее за ARRIVE_HOURS, найденное в Spotify не целиком и релизы,
    чьё скачивание упало, — «не скачалось» с причиной. Доедет позже — всё
    равно станет «в фонотеке». Вернуть (в фонотеку, не скачалось).
    """
    moved = failed = 0
    now = datetime.datetime.now()

    def put(rel, status, note):
        if not DRY:
            con.execute("UPDATE releases SET status=?, note=? WHERE id=?", (status, note, rel["id"]))
        print("   %s %-22s %-34s %s" % ("=" if status == "in_library" else "!", (rel["artist"] or "")[:22],
                                       rel["title"][:34], note or "в фонотеке"))

    for rel in con.execute("SELECT r.*, a.name AS artist, a.deezer_name FROM releases r "
                           "LEFT JOIN artists a ON a.id=r.artist_id "
                           "WHERE r.status IN ('queued', 'downloading', 'failed')").fetchall():
        tracks = [t for t in con.execute("SELECT * FROM release_tracks WHERE release_id=? ORDER BY id", (rel["id"],))
                  if disco.wanted(t)]
        if not tracks:
            continue
        rest = not_arrived(lib, rel, tracks)
        if not rest:
            moved += 1
            put(rel, "in_library", None)
            continue
        if rel["status"] == "queued":
            # скачивание не состоялось: действие упало, а релиз остался в очереди
            act = con.execute("SELECT status, result FROM actions WHERE kind='download' "
                              "AND json_extract(payload, '$.release_id')=? ORDER BY id DESC LIMIT 1",
                              (rel["id"],)).fetchone()
            if act is not None and act["status"] == "failed":
                failed += 1
                put(rel, "failed", re.sub(r"^(ошибка|отклонено): ", "", act["result"] or "") or "скачивание упало")
            continue
        if rel["status"] != "downloading":
            continue
        lost = [t for t in rest if not_in_spotify(t)]
        late = [t for t in rest if t not in lost]
        since = parse_ts(rel["decided_at"])
        if late and since is not None and now - since < datetime.timedelta(hours=ARRIVE_HOURS):
            continue
        failed += 1
        put(rel, "failed", "; ".join(x for x in (
            lost and "в Spotify не нашлось: " + titles(lost),
            late and "не доехало до фонотеки: " + titles(late)) if x))
    return moved, failed


def main():
    only = None
    if "--artist" in sys.argv:
        only = sys.argv[sys.argv.index("--artist") + 1]
    con = jdb.connect()
    if "--arrivals" in sys.argv:
        # только отметить доехавшее: сторож зовёт это сразу после разбора
        # incoming, чтобы скачанный релиз не висел «качается» до ночи
        arrived, failed = recheck_downloads(con, disco.Library())
        print("доехало до фонотеки релизов: %d, не скачалось: %d" % (arrived, failed))
        if os.environ.get("JANITOR_STATS_OUT") and not DRY:
            with open(os.environ["JANITOR_STATS_OUT"], "w", encoding="utf-8") as f:
                json.dump({"arrived": arrived, "failed": failed}, f)
        return
    if "--catch-up" in sys.argv:
        catch_up(con, disco.Library(), float(jdb.setting(con, "ref_ok")))
        return
    if jdb.setting(con, "follow_enabled") != "1" and not only:
        print("слежение выключено в настройках")
        return
    lib = disco.Library()
    ok_t = float(jdb.setting(con, "ref_ok"))
    auto = jdb.setting(con, "auto_download") == "1"

    added = sync_artists(con, lib, int(jdb.setting(con, "follow_min_tracks")))
    print("исполнителей из фонотеки добавлено: %d" % added)
    done, unclear = resolve_ids(con, lib, only)
    print("опознано в Deezer: %d, требуют выбора в пульте: %d" % (done, unclear))

    rows = con.execute("SELECT * FROM artists WHERE deezer_id IS NOT NULL AND follow=1 AND excluded=0 "
                       "ORDER BY last_checked IS NOT NULL, last_checked").fetchall()
    if only:
        rows = [r for r in rows if disco.name_keys(r["name"]) & disco.name_keys(only)]
    new = queued = firsts = 0
    for a in rows:
        if a["last_checked"] is None:
            firsts += 1
        try:
            n, q = check(con, a, lib, ok_t, auto)
        except Exception as e:
            print("   !! %s: %s" % (a["name"], str(e)[:80]))
            continue
        new += n
        queued += q
    arrived, failed = recheck_downloads(con, lib)
    print("проверено исполнителей: %d (впервые: %d) | новинок: %d | в очередь на скачивание: %d | "
          "доехало до фонотеки: %d | не скачалось: %d" % (len(rows), firsts, new, queued, arrived, failed))

    stats_out = os.environ.get("JANITOR_STATS_OUT")
    if stats_out and not DRY:
        with open(stats_out, "w", encoding="utf-8") as f:
            json.dump({"artists": len(rows), "added": added, "resolved": done, "unclear": unclear,
                       "new": new, "queued": queued, "arrived": arrived, "failed": failed}, f)


if __name__ == "__main__":
    main()
