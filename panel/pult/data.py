# -*- coding: utf-8 -*-
"""Данные пульта: общая база набора (janitor.db) и база beets (library.db).

janitor.db пульт читает и пишет: решения, действия, настройки, исполнители.
library.db — только читает. Файлы фонотеки пульт не трогает никогда: всё,
что меняет фонотеку, уходит действием в очередь, и выполняет его сторож.
"""
import datetime
import json
import os
import sqlite3
import time

import env
import janitordb as jdb


# ---------------------------------------------------------------- соединения
def jcon():
    return jdb.connect()


def lcon():
    con = sqlite3.connect("file:%s?mode=ro" % env.LIBRARY_DB, uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def loads(v, default=None):
    return jdb.loads(v, default)


def wake():
    """Разбудить сторожа: действие выполнится сразу, а не после паузы."""
    try:
        os.makedirs(env.JANITOR_DIR, exist_ok=True)
        with open(env.WAKE, "w") as f:
            f.write(jdb.now())
    except OSError:
        pass


def enqueue(con, kind, payload, review_id=None):
    aid = jdb.enqueue(con, kind, payload, review_id)
    wake()
    return aid


def abspath(p):
    """Путь из library.db: bytes и относительный к папке фонотеки."""
    if isinstance(p, bytes):
        p = p.decode("utf-8", "replace")
    p = str(p or "")
    return p if os.path.isabs(p) else os.path.join(env.MUSIC_DIR, p)


def short(p):
    """Путь для показа: без корня фонотеки или карантина."""
    p = str(p or "")
    for root in (env.DUPES_DIR, env.BROKEN_DIR, env.MUSIC_DIR, env.RESIDUE_DIR, env.INCOMING_DIR):
        if p.startswith(root + "/"):
            return p[len(root) + 1:]
    return p


def inside(path, *roots):
    real = os.path.realpath(path)
    return any(real == os.path.realpath(r) or real.startswith(os.path.realpath(r) + os.sep) for r in roots)


def parse_ts(ts):
    try:
        return datetime.datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- счётчики меню
def nav_counts():
    con = jcon()
    try:
        c = {k: 0 for k in ("review", "releases", "artists", "covers", "quarantine")}
        for r in con.execute("SELECT kind, count(*) n FROM reviews WHERE status='open' GROUP BY kind"):
            if r["kind"] == "cover":
                c["covers"] += r["n"]
            else:
                c["review"] += r["n"]
        c["releases"] = con.execute("SELECT count(*) FROM releases WHERE status='new'").fetchone()[0]
        c["artists"] = con.execute("SELECT count(*) FROM artists WHERE follow=1 AND excluded=0").fetchone()[0]
        c["quarantine"] = con.execute("SELECT count(*) FROM quarantine WHERE status='held'").fetchone()[0]
        return c
    finally:
        con.close()


def heartbeat():
    """Чем занят сторож: из файла, который он обновляет на каждом цикле."""
    try:
        with open(env.HEARTBEAT, encoding="utf-8") as f:
            parts = f.read().strip().split("|")
    except OSError:
        return None
    ts = parse_ts(parts[0])
    if ts is None:
        return None
    age = (datetime.datetime.now() - ts).total_seconds()
    hb = {"ts": ts, "state": parts[1] if len(parts) > 1 else "", "age": age, "next": None}
    if len(parts) > 2 and parts[2].isdigit():
        hb["next"] = max(0, int(parts[2]) - int(age))
    # сторож не отзывался дольше двух циклов — пульт должен об этом сказать
    hb["stale"] = age > 3 * 3600 if "ночн" in hb["state"] else age > 1200
    return hb


# ---------------------------------------------------------------- сводка
STEP_TEXT = {
    "retry": lambda s: "добито <b>%d</b> из %d, в «как есть» %d" % (s.get("matched", 0), s.get("processed", 0), s.get("asked", 0)),
    "albumgroup": lambda s: "собрано альбомов <b>%d</b>, дорожек %d" % (s.get("albums", 0), s.get("fixed", 0)),
    "dedup": lambda s: "копий в карантин <b>%d</b>, разных версий %d" % (s.get("moved", 0), s.get("versions", 0)),
    "consolidate": lambda s: "в карантин <b>%d</b>, слито <b>%d</b>, дисков %d, спорных %d" % (
        s.get("dupes", 0), s.get("merged", 0), s.get("discs", 0) + s.get("discs_multi", 0), s.get("unsure", 0) + s.get("alien", 0)),
    "verify": lambda s: "сверено <b>%d</b>, подмен <b>%d</b>, спорных %d" % (
        sum(v for k, v in s.items() if k not in ("new", "backlog")), s.get("substitution", 0), s.get("unsure", 0)),
    "follow": lambda s: "исполнителей %d, новинок <b>%d</b>, в очередь %d" % (s.get("artists", 0), s.get("new", 0), s.get("queued", 0)),
    "covers": lambda s: "обложек <b>+%d</b>, не нашлось %d" % (
        s.get("singles_found", 0) + s.get("albums_found", 0), s.get("singles_missing", 0) + s.get("albums_missing", 0)),
    "import": lambda s: "дублей в карантин %d, других версий %d" % (s.get("leftovers_parked", 0), s.get("leftovers_versions", 0)),
}


def step_rows(con, run_id):
    rows = []
    longest = 1
    for s in con.execute("SELECT * FROM steps WHERE run_id=? ORDER BY id", (run_id,)):
        a, b = parse_ts(s["started_at"]), parse_ts(s["finished_at"])
        secs = int((b - a).total_seconds()) if a and b else None
        stats = loads(s["stats"], {}) or {}
        fn = STEP_TEXT.get(s["name"])
        try:
            text = fn(stats) if fn and stats else ""
        except Exception:
            text = ""
        rows.append({"name": s["name"], "status": s["status"], "secs": secs, "text": text})
        longest = max(longest, secs or 0)
    for r in rows:
        r["bar"] = round(100.0 * (r["secs"] or 0) / longest, 1)
    return rows


def last_run(con, kind="nightly"):
    r = con.execute("SELECT * FROM runs WHERE kind=? ORDER BY id DESC LIMIT 1", (kind,)).fetchone()
    if r is None:
        return None
    a, b = parse_ts(r["started_at"]), parse_ts(r["finished_at"])
    return {"row": r, "start": a, "end": b, "mins": int((b - a).total_seconds() // 60) if a and b else None,
            "steps": step_rows(con, r["id"]),
            "failed": sum(1 for s in con.execute("SELECT status FROM steps WHERE run_id=?", (r["id"],))
                          if s["status"] != "ok")}


def library_stats():
    out = {"tracks": 0, "albums": 0, "artists": 0, "asis": 0}
    try:
        l = lcon()
    except sqlite3.Error:
        return out
    try:
        out["tracks"] = l.execute("SELECT count(*) FROM items").fetchone()[0]
        out["albums"] = l.execute("SELECT count(*) FROM albums").fetchone()[0]
        out["artists"] = l.execute(
            "SELECT count(DISTINCT lower(CASE WHEN albumartist != '' THEN albumartist ELSE artist END)) FROM items"
        ).fetchone()[0]
        out["asis"] = l.execute("SELECT count(*) FROM items WHERE mb_trackid IS NULL OR mb_trackid = ''").fetchone()[0]
    except sqlite3.Error:
        pass
    finally:
        l.close()
    return out


def read_first(path, default=""):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return default


def spotify_state():
    today = datetime.date.today().isoformat()
    used = 0
    parts = read_first(env.SPOTIFY_BUDGET_FILE).split()
    if len(parts) == 2 and parts[0] == today and parts[1].isdigit():
        used = int(parts[1])
    try:
        pace = float(read_first(env.SPOTIFY_PACE_FILE, "0.5"))
    except ValueError:
        pace = 0.5
    try:
        until = float((read_first(env.SPOTIFY_BREAKER_FILE, "0").split() or ["0"])[0])
    except ValueError:
        until = 0.0
    r429 = 0
    try:
        with open(env.NET_LOG, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 300000))
            for line in f.read().decode("utf-8", "replace").splitlines():
                if line.startswith(today) and " 429 " in line:
                    r429 += 1
    except OSError:
        pass
    daily = env.SPOTIFY_DAILY
    return {"used": used, "daily": daily, "pct": round(100.0 * used / daily, 1) if daily else 0,
            "pace": pace, "breaker": until > time.time(),
            "breaker_until": datetime.datetime.fromtimestamp(until) if until > time.time() else None,
            "r429": r429}


def recent_imports(con, limit=6):
    rows = con.execute(
        "SELECT e.ts, e.item_id, e.after, c.verdict, c.similarity FROM events e "
        "LEFT JOIN checks c ON c.item_id = e.item_id WHERE e.script='import' ORDER BY e.id DESC LIMIT ?",
        (limit,)).fetchall()
    out = []
    for r in rows:
        a = loads(r["after"], {}) or {}
        out.append({"ts": parse_ts(r["ts"]), "artist": a.get("artist"), "title": a.get("title"),
                    "source": a.get("source") or "", "verdict": r["verdict"], "similarity": r["similarity"]})
    return out


def expiring(con, within_days=2):
    """Сколько файлов карантина удалится в ближайшие дни."""
    dupes = int(jdb.setting(con, "dupes_days"))
    junk = int(jdb.setting(con, "junk_days"))
    now = datetime.datetime.now()
    n = 0
    for r in con.execute("SELECT reason, moved_at FROM quarantine WHERE status='held'"):
        t = parse_ts(r["moved_at"])
        if t is None:
            continue
        keep = junk if r["reason"] == "broken" else dupes
        if (t + datetime.timedelta(days=keep) - now).days < within_days:
            n += 1
    return n


def summary():
    con = jcon()
    try:
        opened = {r["kind"]: r["n"] for r in con.execute(
            "SELECT kind, count(*) n FROM reviews WHERE status='open' GROUP BY kind")}
        subs = con.execute("SELECT count(*) FROM reviews WHERE status='open' AND kind='substitution'").fetchone()[0]
        new_rel = con.execute(
            "SELECT r.title, a.name FROM releases r LEFT JOIN artists a ON a.id=r.artist_id "
            "WHERE r.status='new' ORDER BY r.release_date DESC LIMIT 5").fetchall()
        lib = library_stats()
        missing = jdb.meta_get(con, "art_missing")
        art_pct = None
        if missing is not None and lib["tracks"]:
            art_pct = round(100.0 * (lib["tracks"] - int(missing)) / lib["tracks"], 1)
        music = jdb.meta_get(con, "music_bytes")
        return {
            "run": last_run(con), "incoming": last_run(con, "incoming"),
            "reviews": sum(v for k, v in opened.items() if k != "cover"),
            "subs": subs, "dups": opened.get("duplicate", 0), "asis": opened.get("asis", 0),
            "covers": opened.get("cover", 0),
            "new_releases": con.execute("SELECT count(*) FROM releases WHERE status='new'").fetchone()[0],
            "new_names": ", ".join(sorted({r["name"] or "?" for r in new_rel})),
            "expiring": expiring(con),
            "lib": lib, "art_pct": art_pct, "music_bytes": int(music) if music and str(music).isdigit() else None,
            "spotify": spotify_state(), "imports": recent_imports(con),
            "checked": con.execute("SELECT count(*) FROM checks").fetchone()[0],
        }
    finally:
        con.close()


# ---------------------------------------------------------------- решения
def items_by_id(ids):
    ids = [int(i) for i in ids if i]
    if not ids:
        return {}
    l = lcon()
    try:
        q = "SELECT id, path, artist, title, album, length, bitrate, format FROM items WHERE id IN (%s)" % \
            ",".join("?" * len(ids))
        return {r["id"]: dict(r, path=abspath(r["path"])) for r in l.execute(q, ids)}
    finally:
        l.close()


def review_cards(con, kind, limit=60):
    rows = con.execute("SELECT * FROM reviews WHERE status='open' AND kind=? ORDER BY id LIMIT ?",
                       (kind, limit)).fetchall()
    cards = []
    ids = []
    for r in rows:
        p = loads(r["payload"], {}) or {}
        ids.extend([f.get("item_id") for f in p.get("files") or []])
        ids.append(p.get("item_id"))
        cards.append({"row": r, "p": p})
    live = items_by_id(ids)
    for c in cards:
        c["live"] = live
        c["verify"] = c["row"]["key"].startswith("verify:")
        files = c["p"].get("files") or []
        c["files"] = [dict(f, exists=f.get("item_id") in live) for f in files]
        c["exists"] = (c["p"].get("item_id") in live) if c["verify"] or kind == "asis" else all(f["exists"] for f in c["files"])
    return cards


def review_counts(con):
    return {r["kind"]: r["n"] for r in con.execute(
        "SELECT kind, count(*) n FROM reviews WHERE status='open' GROUP BY kind")}


# ---------------------------------------------------------------- карантин
REASON_TEXT = {"duplicate": "копия", "substitution": "подмена", "broken": "битый",
               "import_failed": "не импортировался", "skipped": "пропущен"}


def quarantine(con, reason=None, offset=0, limit=150):
    dupes = int(jdb.setting(con, "dupes_days"))
    junk = int(jdb.setting(con, "junk_days"))
    q = "SELECT * FROM quarantine WHERE status='held'"
    args = []
    if reason:
        q += " AND reason=?"
        args.append(reason)
    q += " ORDER BY moved_at DESC LIMIT ? OFFSET ?"
    args += [limit, offset]
    now = datetime.datetime.now()
    out = []
    for r in con.execute(q, args):
        t = parse_ts(r["moved_at"])
        keep = junk if r["reason"] == "broken" else dupes
        left = (t + datetime.timedelta(days=keep) - now).total_seconds() / 86400 if t else None
        from_incoming = str(r["original_path"]).startswith(env.INCOMING_DIR + "/")
        out.append({"row": r, "moved": t, "left": left, "keep": keep,
                    "used_pct": None if left is None else max(0, min(100, round(100 * (1 - left / keep)))),
                    "reason_text": REASON_TEXT.get(r["reason"], r["reason"]),
                    "from_incoming": from_incoming, "exists": os.path.exists(r["path"])})
    return out


def quarantine_summary(con):
    rows = con.execute("SELECT reason, count(*) n, sum(size) s FROM quarantine WHERE status='held' GROUP BY reason").fetchall()
    return {"by": {r["reason"]: r["n"] for r in rows}, "total": sum(r["n"] for r in rows),
            "bytes": sum(r["s"] or 0 for r in rows), "expiring": expiring(con),
            "dupes_days": int(jdb.setting(con, "dupes_days"))}


# ---------------------------------------------------------------- журнал
FILE_OPS = ("quarantine", "move", "rename", "restore", "replace", "import_version", "import")


def journal(con, f=None, offset=0, limit=100):
    q = "SELECT * FROM events WHERE 1=1"
    args = []
    if f == "tags":
        q += " AND op NOT IN (%s) AND script NOT IN ('import', 'download')" % ",".join("?" * len(FILE_OPS))
        args += FILE_OPS
    elif f == "files":
        q += " AND op IN (%s)" % ",".join("?" * len(FILE_OPS))
        args += FILE_OPS
    elif f == "hand":
        q += " AND script='panel'"
    # по времени, а не по номеру: записи из старых журналов перенесены в базу
    # не в хронологическом порядке
    q += " ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?"
    args += [limit, int(offset or 0)]
    out = []
    for r in con.execute(q, args):
        b, a = loads(r["before"], {}) or {}, loads(r["after"], {}) or {}
        out.append({"row": r, "ts": parse_ts(r["ts"]), "changes": diff(b, a), "title": a.get("title") or b.get("title"),
                    "can_rollback": can_rollback(r, b), "path": short(r["path"] or a.get("path") or b.get("path") or "")})
    return out


SKIP_KEYS = {"why", "distance", "date", "id", "album_id"}
# служебные поля: показываем, только если больше ничего не поменялось
TECH_KEYS = {"mb_trackid", "mb_albumid", "mb_artistid", "mb_albumartistid", "data_source", "label", "comp"}
KEY_ORDER = {"artist": 0, "title": 1, "album": 2, "albumartist": 3, "path": 4}


def diff(before, after):
    """Изменённые поля: [(поле, было, стало)], сначала содержательные."""
    if not isinstance(before, dict) or not isinstance(after, dict):
        return []
    out = []
    for k in set(before) | set(after):
        if k in SKIP_KEYS:
            continue
        b, a = before.get(k), after.get(k)
        if b == a or (b in (None, "") and a in (None, "")):
            continue
        if k == "path":
            b, a = short(b), short(a)
        out.append((k, b, a))
    main = [x for x in out if x[0] not in TECH_KEYS]
    out = main or out
    return sorted(out, key=lambda x: (KEY_ORDER.get(x[0], 9), x[0]))


def can_rollback(ev, before):
    if ev["reverted_by"] or ev["script"] in ("import", "download") or ev["op"] in ("rollback", "import_version", "import"):
        return False
    if ev["op"] in ("quarantine", "move", "rename"):
        return True
    return isinstance(before, dict) and bool(ev["item_id"]) and any(
        k in before for k in ("album", "albumartist", "artist", "title", "year", "disc", "track", "comp", "mb_trackid"))


# ---------------------------------------------------------------- новинки и исполнители
REL_STATUS = {"new": ("ждёт решения", "acc"), "queued": ("в очереди", "info"), "downloading": ("качается", "info"),
              "in_library": ("в фонотеке", "ok"), "skipped": ("пропущен", ""), "known": ("известен", "")}
TYPE_TEXT = {"album": "альбом", "single": "сингл", "ep": "EP", "compile": "сборник"}


def releases(con, f=None):
    where = {"wait": "r.status='new'", "work": "r.status IN ('queued','downloading')",
             "done": "r.status='in_library'", "skip": "r.status='skipped'"}.get(
        f, "r.status IN ('new','queued','downloading','in_library','skipped')")
    rows = con.execute(
        "SELECT r.*, a.name AS artist FROM releases r LEFT JOIN artists a ON a.id=r.artist_id "
        "WHERE %s ORDER BY r.release_date DESC, r.id DESC LIMIT 120" % where).fetchall()
    out = []
    for r in rows:
        c = loads(r["counts"], {}) or {}
        owner = None
        if r["feat"]:
            # у фита хозяин — главный исполнитель дорожек, а не тот, за кем следим
            t = con.execute("SELECT info FROM release_tracks WHERE release_id=? LIMIT 1", (r["id"],)).fetchone()
            owner = (loads(t["info"], {}) or {}).get("artist") if t else None
        out.append({"row": r, "counts": c, "status": REL_STATUS.get(r["status"], (r["status"], "")),
                    "type": TYPE_TEXT.get(r["type"], r["type"] or ""), "get": c.get("get", 0),
                    "have": c.get("have", 0) + c.get("dup", 0), "owner": owner})
    return out


def release_counts(con):
    c = {r["status"]: r["n"] for r in con.execute("SELECT status, count(*) n FROM releases GROUP BY status")}
    return {"all": sum(c.get(k, 0) for k in ("new", "queued", "downloading", "in_library", "skipped")),
            "wait": c.get("new", 0), "work": c.get("queued", 0) + c.get("downloading", 0),
            "done": c.get("in_library", 0), "skip": c.get("skipped", 0)}


def artists(con, f=None):
    rows = con.execute(
        "SELECT a.*, (SELECT title FROM releases WHERE artist_id=a.id ORDER BY release_date DESC LIMIT 1) AS last_title, "
        "(SELECT release_date FROM releases WHERE artist_id=a.id ORDER BY release_date DESC LIMIT 1) AS last_date, "
        "(SELECT status FROM releases WHERE artist_id=a.id ORDER BY release_date DESC LIMIT 1) AS last_status, "
        "(SELECT count(*) FROM releases WHERE artist_id=a.id AND status='new') AS new_count "
        "FROM artists a ORDER BY a.follow DESC, a.library_tracks DESC, a.name").fetchall()
    out = []
    for r in rows:
        k = "off" if (not r["follow"] or r["excluded"]) else ("fix" if r["attention"] else ("new" if r["new_count"] else "all"))
        out.append({"row": r, "k": k, "cands": loads(r["info"], []) if r["attention"] else []})
    if f in ("new", "fix", "off"):
        out = [a for a in out if a["k"] == f]
    else:
        out = [a for a in out if a["k"] != "off"]
    return out


def artist_counts(con):
    c = {"follow": 0, "fix": 0, "off": 0, "new": 0}
    for r in con.execute("SELECT follow, excluded, attention, deezer_id, "
                         "(SELECT count(*) FROM releases WHERE artist_id=artists.id AND status='new') n FROM artists"):
        if not r["follow"] or r["excluded"]:
            c["off"] += 1
            continue
        c["follow"] += 1
        if r["attention"]:
            c["fix"] += 1
        if r["n"]:
            c["new"] += 1
    return c


# ---------------------------------------------------------------- дискография
def discography(con, artist_id, albums=True, singles=True, versions=False, feats=True):
    a = con.execute("SELECT * FROM artists WHERE id=?", (artist_id,)).fetchone()
    if a is None:
        return None
    rels = con.execute("SELECT * FROM releases WHERE artist_id=? AND tracks_total IS NOT NULL "
                       "ORDER BY release_date, id", (artist_id,)).fetchall()
    import discography as disco
    total = {"have": 0, "dup": 0, "version": 0, "ask": 0, "get": 0}
    to_get, asks, vers, full = [], [], [], []
    feat_list = []
    for r in rels:
        tracks = con.execute("SELECT * FROM release_tracks WHERE release_id=? ORDER BY id", (r["id"],)).fetchall()
        is_single = r["type"] in ("single", "ep")
        is_feat = bool(r["feat"])
        allowed = (singles if is_single else albums) and (feats or not is_feat)
        grp = {"have": 0, "dup": 0, "version": 0, "ask": 0, "get": 0}
        sel = 0
        for t in tracks:
            g = disco.VERDICT_GROUP.get(t["verdict"], t["verdict"])
            grp[g] = grp.get(g, 0) + 1
            if t["verdict"] == "ask" and not t["decision"]:
                info = loads(t["info"], {}) or {}
                asks.append({"t": t, "rel": r, "info": info})
            want = disco.wanted(t) or (versions and t["verdict"] == "version" and t["decision"] != "skip")
            if want and allowed:
                sel += 1
        for k in total:
            total[k] += grp.get(k, 0)
        entry = {"rel": r, "grp": grp, "sel": sel, "tracks": len(tracks), "type": TYPE_TEXT.get(r["type"], r["type"]),
                 "allowed": allowed, "feat": is_feat}
        if is_feat:
            feat_list.append(entry)
        # разделы не исключают друг друга: у Deluxe бывают и бонусы к
        # скачиванию, и инструменталы среди других версий
        if sel:
            to_get.append(entry)
        if grp["version"]:
            vers.append(entry)
        if tracks and grp["have"] + grp["dup"] == len(tracks):
            full.append(entry)
    n = sum(total.values()) or 1
    selected = sum(e["sel"] for e in to_get)
    return {"artist": a, "total": total, "n": sum(total.values()), "selected": selected,
            "pct": {k: round(100.0 * v / n, 1) for k, v in total.items()},
            "to_get": to_get, "asks": asks, "versions": vers, "full": full, "feats": feat_list,
            "flags": {"albums": albums, "singles": singles, "versions": versions, "feats": feats}}


def job(con, job_id):
    r = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if r is not None and r["status"] == "running":
        t = parse_ts(r["started_at"])
        # поток, начатый до перезапуска пульта, уже не закончится
        if t and (datetime.datetime.now() - t).total_seconds() > 1800:
            con.execute("UPDATE jobs SET status='failed', result='прерван перезапуском пульта' WHERE id=?", (job_id,))
            r = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return r


# ---------------------------------------------------------------- обложки
def cover_cards(con, limit=80):
    rows = con.execute("SELECT * FROM reviews WHERE status='open' AND kind='cover' "
                       "ORDER BY json_extract(payload, '$.count') DESC, id LIMIT ?", (limit,)).fetchall()
    return [{"row": r, "p": loads(r["payload"], {}) or {}} for r in rows]


def recent_covers(con, limit=12):
    rows = con.execute("SELECT e.ts, e.script, e.path, e.after FROM events e WHERE e.op='cover' "
                       "ORDER BY e.id DESC LIMIT 300").fetchall()
    seen, out = set(), []
    for r in rows:
        folder = os.path.dirname(r["path"] or "")
        if folder in seen:
            continue
        seen.add(folder)
        a = loads(r["after"], {}) or {}
        out.append({"ts": parse_ts(r["ts"]), "folder": short(folder), "src": a.get("cover"),
                    "hand": r["script"] == "panel"})
        if len(out) >= limit:
            break
    return out
