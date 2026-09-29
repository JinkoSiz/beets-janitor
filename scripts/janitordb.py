#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Общая база beets-janitor: что сделано, что ждёт решения, что поставлено в очередь.

Раньше всё это уходило текстом в логи, которые никто не читал: спорные дубли,
подмены, треки без обложки. Теперь скрипты пишут сюда структурно, а пульт
читает отсюда и возвращает решения.

Главное правило раскладки: **файлами фонотеки распоряжается только
beets-watch.** Пульт своё решение кладёт в таблицу actions, а исполняет его
apply_actions.py на следующем цикле сторожа. Причина не только в порядке:
база beets — SQLite, и два процесса, пишущие в неё одновременно, рано или
поздно ловят блокировку.

Сама эта база тоже SQLite, но пишут в неё короткими транзакциями и в режиме
WAL — так сторож и пульт уживаются без блокировок.

Из shell (nightly.sh) доступны команды:
    janitordb.py run-start <kind>          -> печатает id прогона
    janitordb.py run-finish <id> <status>
    janitordb.py step-start <run_id> <name> -> печатает id шага
    janitordb.py step-finish <id> <status> [json со счётчиками]
    janitordb.py migrate
"""
import datetime
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import env  # noqa: E402

SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- прогон: ночная работа, разбор incoming или ручной запуск
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,              -- nightly | incoming | manual
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL DEFAULT 'running',  -- running | ok | failed
    note        TEXT
);

-- шаг прогона: fixenc, retry, consolidate...
CREATE TABLE IF NOT EXISTS steps (
    id          INTEGER PRIMARY KEY,
    run_id      INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL DEFAULT 'running',  -- running | ok | failed | timeout
    stats       TEXT                               -- json со счётчиками
);
CREATE INDEX IF NOT EXISTS steps_run ON steps(run_id);

-- журнал: каждая правка тегов и каждое перемещение, «было -> стало»
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,
    run_id      INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    script      TEXT NOT NULL,
    op          TEXT NOT NULL,
    item_id     INTEGER,                   -- id дорожки в базе beets, если есть
    path        TEXT,
    before      TEXT,                      -- json
    after       TEXT,                      -- json
    reverted_by INTEGER REFERENCES events(id)
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS events_item ON events(item_id);

-- очередь решений: то, в чём скрипт не уверен и сам не трогает.
-- key — устойчивое имя вопроса: по нему скрипт узнаёт, что уже спрашивал,
-- и не задаёт тот же вопрос второй раз, а применяет ответ.
CREATE TABLE IF NOT EXISTS reviews (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,             -- substitution | duplicate | asis | cover | release
    key         TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',  -- open | resolved | dismissed
    title       TEXT,
    payload     TEXT,                      -- json: файлы, проценты, подсказка
    decision    TEXT,
    decided_at  TEXT
);
CREATE INDEX IF NOT EXISTS reviews_open ON reviews(status, kind);

-- очередь действий для beets-watch: только он трогает файлы
CREATE TABLE IF NOT EXISTS actions (
    id          INTEGER PRIMARY KEY,
    created_at  TEXT NOT NULL,
    kind        TEXT NOT NULL,             -- quarantine | restore | set_cover | accept_candidate | download | rollback | run
    payload     TEXT,
    review_id   INTEGER REFERENCES reviews(id) ON DELETE SET NULL,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending | running | done | failed
    result      TEXT,
    started_at  TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS actions_pending ON actions(status, id);

-- карантин: что убрано, почему и откуда
CREATE TABLE IF NOT EXISTS quarantine (
    id            INTEGER PRIMARY KEY,
    path          TEXT NOT NULL UNIQUE,    -- где лежит сейчас
    original_path TEXT NOT NULL,           -- откуда убран
    reason        TEXT NOT NULL,           -- duplicate | substitution | broken
    similarity    REAL,
    kept_path     TEXT,                    -- какая копия осталась вместо
    album_id      INTEGER,                 -- из какого альбома beets убран: туда и вернётся;
                                           -- 0 — был одиночкой, NULL — неизвестно (старые записи)
    moved_at      TEXT NOT NULL,
    size          INTEGER,
    title         TEXT,
    artist        TEXT,
    status        TEXT NOT NULL DEFAULT 'held'  -- held | restored | purged
);
CREATE INDEX IF NOT EXISTS quarantine_status ON quarantine(status, moved_at);

-- исполнители, за которыми следит сторож
CREATE TABLE IF NOT EXISTS artists (
    id                INTEGER PRIMARY KEY,
    name              TEXT NOT NULL,
    source            TEXT NOT NULL DEFAULT 'auto',  -- auto | manual
    follow            INTEGER NOT NULL DEFAULT 1,
    excluded          INTEGER NOT NULL DEFAULT 0,
    spotify_id        TEXT,
    deezer_id         TEXT,
    library_tracks    INTEGER NOT NULL DEFAULT 0,
    attention         TEXT,                          -- что не так, если не так
    last_checked      TEXT,
    last_release_date TEXT,
    created_at        TEXT NOT NULL,
    deezer_name       TEXT,                          -- как исполнитель называется в Deezer
    picture           TEXT,
    info              TEXT                           -- json: кандидаты, если выбор неоднозначен
);
CREATE UNIQUE INDEX IF NOT EXISTS artists_spotify ON artists(spotify_id) WHERE spotify_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS artists_deezer ON artists(deezer_id) WHERE deezer_id IS NOT NULL;

-- релизы, которые сторож нашёл у исполнителей
CREATE TABLE IF NOT EXISTS releases (
    id            INTEGER PRIMARY KEY,
    artist_id     INTEGER REFERENCES artists(id) ON DELETE CASCADE,
    provider      TEXT NOT NULL,           -- deezer | spotify
    provider_id   TEXT NOT NULL,
    title         TEXT NOT NULL,
    type          TEXT,                    -- album | single | ep | compilation
    release_date  TEXT,
    tracks_total  INTEGER,
    status        TEXT NOT NULL DEFAULT 'new',  -- known | new | queued | downloading | in_library | skipped
    counts        TEXT,                    -- json: have / dup / version / ask / get
    found_at      TEXT NOT NULL,
    decided_at    TEXT,
    cover         TEXT,
    link          TEXT,
    feat          INTEGER,                 -- 1 — чужой релиз, исполнитель в нём гость
    UNIQUE(provider, provider_id)
);
CREATE INDEX IF NOT EXISTS releases_status ON releases(status, release_date);

-- дорожки релиза с вердиктом разбора дискографии
CREATE TABLE IF NOT EXISTS release_tracks (
    id             INTEGER PRIMARY KEY,
    release_id     INTEGER NOT NULL REFERENCES releases(id) ON DELETE CASCADE,
    provider_id    TEXT NOT NULL,
    isrc           TEXT,
    title          TEXT NOT NULL,
    duration       INTEGER,
    verdict        TEXT,                   -- have_isrc | have_same | dup | version | ask | get
    library_item   INTEGER,                -- с какой дорожкой библиотеки совпала
    similarity     REAL,
    decision       TEXT,                   -- решение из пульта: get | skip; перекрывает вердикт
    info           TEXT,                   -- json: почему так, превью, путь в фонотеке, id в Spotify
    UNIQUE(release_id, provider_id)
);
CREATE INDEX IF NOT EXISTS release_tracks_isrc ON release_tracks(isrc);

-- настройки, которые меняются из пульта: пороги, сроки, расписание.
-- Пути сюда не входят — это окружение контейнера.
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- долгие задачи пульта (разбор дискографии): пульт запускает их у себя в
-- фоне и показывает ход, а не держит запрос открытым по полминуты
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,             -- discography
    params      TEXT,                      -- json
    status      TEXT NOT NULL DEFAULT 'running',  -- running | done | failed
    progress    TEXT,                      -- «релиз 12 из 27»
    result      TEXT,                      -- json или текст ошибки
    started_at  TEXT NOT NULL,
    finished_at TEXT
);

-- сверка звука с превью источника (verify.py): та ли песня под этими тегами
CREATE TABLE IF NOT EXISTS checks (
    item_id     INTEGER PRIMARY KEY,       -- дорожка в базе beets
    path        TEXT,
    size        INTEGER,                   -- по размеру видно, что файл заменили
    provider    TEXT,                      -- spotify | deezer
    ref_id      TEXT,                      -- трек у источника, с чьим превью сверяли
    similarity  REAL,
    verdict     TEXT NOT NULL,             -- ok | unsure | substitution | no_preview | no_ref | failed
    checked_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS checks_verdict ON checks(verdict);
"""


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def connect(path=None):
    """Соединение с базой. Создаёт её и схему при первом обращении."""
    path = path or env.JANITOR_DB
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA foreign_keys=ON")
    migrate(con)
    return con


# Колонки, появившиеся после первой версии схемы. Новые таблицы база догоняет
# сама (CREATE ... IF NOT EXISTS), а новые колонки в старых таблицах —
# только через ALTER, который и делается здесь, если колонки ещё нет.
ADDED_COLUMNS = {
    "artists": [("deezer_name", "TEXT"), ("picture", "TEXT"), ("info", "TEXT")],
    "releases": [("cover", "TEXT"), ("link", "TEXT"), ("feat", "INTEGER")],
    "release_tracks": [("decision", "TEXT"), ("info", "TEXT")],
}


def migrate(con):
    con.executescript(SCHEMA)
    for table, cols in ADDED_COLUMNS.items():
        have = {r[1] for r in con.execute("PRAGMA table_info(%s)" % table)}
        for name, typ in cols:
            if name not in have:
                con.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, typ))
    if int(meta_get(con, "schema_version") or 0) < SCHEMA_VERSION:
        meta_set(con, "schema_version", SCHEMA_VERSION)


def meta_get(con, key, default=None):
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row is not None else default


def meta_set(con, key, value):
    con.execute("INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def _j(v):
    return None if v is None else json.dumps(v, ensure_ascii=False, default=str)


def loads(v, default=None):
    if not v:
        return default
    try:
        return json.loads(v)
    except Exception:
        return default


# ---------------------------------------------------------------- прогоны и шаги
def start_run(con, kind, note=None):
    cur = con.execute("INSERT INTO runs(kind, started_at, note) VALUES (?, ?, ?)", (kind, now(), note))
    return cur.lastrowid


def finish_run(con, run_id, status="ok"):
    con.execute("UPDATE runs SET finished_at=?, status=? WHERE id=?", (now(), status, run_id))


def start_step(con, run_id, name):
    cur = con.execute("INSERT INTO steps(run_id, name, started_at) VALUES (?, ?, ?)", (run_id, name, now()))
    return cur.lastrowid


def finish_step(con, step_id, status="ok", stats=None):
    con.execute("UPDATE steps SET finished_at=?, status=?, stats=? WHERE id=?",
                (now(), status, _j(stats), step_id))


def current_run():
    """id прогона, внутри которого нас запустили (nightly.sh кладёт его в окружение)."""
    v = os.environ.get("JANITOR_RUN_ID")
    return int(v) if v and v.isdigit() else None


# ---------------------------------------------------------------- журнал
def log_event(con, script, op, before=None, after=None, item_id=None, path=None):
    cur = con.execute(
        "INSERT INTO events(ts, run_id, script, op, item_id, path, before, after) VALUES (?,?,?,?,?,?,?,?)",
        (now(), current_run(), script, op, item_id, path, _j(before), _j(after)))
    return cur.lastrowid


# ---------------------------------------------------------------- решения
def review_state(con, key):
    """(status, decision) по ключу вопроса или (None, None), если не спрашивали."""
    row = con.execute("SELECT status, decision FROM reviews WHERE key=?", (key,)).fetchone()
    return (row["status"], row["decision"]) if row else (None, None)


def ask(con, kind, key, title, payload):
    """Поставить вопрос в очередь.

    Уже решённый вопрос не открывается заново — возвращается его решение, и
    скрипт должен ему следовать. Открытый обновляется свежими данными
    (проценты могли измениться после переcчёта). Возвращает (status, decision).
    """
    status, decision = review_state(con, key)
    if status in ("resolved", "dismissed"):
        return status, decision
    ts = now()
    if status is None:
        con.execute(
            "INSERT INTO reviews(kind, key, created_at, updated_at, title, payload) VALUES (?,?,?,?,?,?)",
            (kind, key, ts, ts, title, _j(payload)))
    else:
        con.execute("UPDATE reviews SET updated_at=?, title=?, payload=? WHERE key=?",
                    (ts, title, _j(payload), key))
    return "open", None


def resolve(con, review_id, decision):
    con.execute("UPDATE reviews SET status='resolved', decision=?, decided_at=? WHERE id=?",
                (decision, now(), review_id))


def pair_key(kind, *paths):
    """Устойчивое имя вопроса о наборе файлов: порядок не важен."""
    return "%s:%s" % (kind, "|".join(sorted(paths)))


# ---------------------------------------------------------------- действия
def enqueue(con, kind, payload=None, review_id=None):
    cur = con.execute("INSERT INTO actions(created_at, kind, payload, review_id) VALUES (?,?,?,?)",
                      (now(), kind, _j(payload), review_id))
    return cur.lastrowid


def take_pending(con, limit=50):
    """Забрать очередные действия. Помечаем running сразу, чтобы второй цикл
    сторожа, если вдруг наложится на первый, не выполнил их дважды."""
    rows = con.execute("SELECT * FROM actions WHERE status='pending' ORDER BY id LIMIT ?", (limit,)).fetchall()
    for r in rows:
        con.execute("UPDATE actions SET status='running', started_at=? WHERE id=? AND status='pending'",
                    (now(), r["id"]))
    return rows


def finish_action(con, action_id, ok, result=None):
    con.execute("UPDATE actions SET status=?, result=?, finished_at=? WHERE id=?",
                ("done" if ok else "failed", result, now(), action_id))


# ---------------------------------------------------------------- карантин
def quarantine_add(con, path, original_path, reason, similarity=None, kept_path=None,
                   title=None, artist=None, album_id=None):
    try:
        size = os.path.getsize(path)
    except OSError:
        size = None
    con.execute(
        "INSERT INTO quarantine(path, original_path, reason, similarity, kept_path, album_id, moved_at, size, title, artist) "
        "VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(path) DO UPDATE SET original_path=excluded.original_path, reason=excluded.reason, "
        "similarity=excluded.similarity, kept_path=excluded.kept_path, album_id=excluded.album_id, "
        "moved_at=excluded.moved_at, size=excluded.size, status='held'",
        (path, original_path, reason, similarity, kept_path, album_id, now(), size, title, artist))


# ---------------------------------------------------------------- настройки
DEFAULTS = {
    "fp_same": "0.65",
    "fp_same_short": "0.60",
    "fp_ask": "0.55",
    "ref_ok": "0.85",
    "ref_bad": "0.60",
    "dupes_days": "14",
    "junk_days": "3",
    "follow_min_tracks": "3",
    "follow_enabled": "1",
    "auto_download": "0",
    "follow_new_days": "60",
    "verify_limit": "300",
    "nightly_hour": "5",
    "interval": "300",
}


def setting(con, key, default=None):
    row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is not None:
        return row["value"]
    return DEFAULTS.get(key, default)


def set_setting(con, key, value):
    con.execute("INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


# ---------------------------------------------------------------- обслуживание
def sync_quarantine(con):
    """Карантин против диска.

    Файлы, удалённые по сроку, помечаются purged — иначе пульт показывал бы
    их с кнопкой «Вернуть», которой нечего возвращать. Файлы, лежащие в
    карантине без записи (их убрали до появления этой базы), регистрируются:
    исходный путь восстанавливается по относительному, как это делает
    unquarantine.py.
    """
    purged = 0
    for r in con.execute("SELECT id, path, size FROM quarantine WHERE status='held'").fetchall():
        if not os.path.exists(r["path"]):
            con.execute("UPDATE quarantine SET status='purged' WHERE id=?", (r["id"],))
            purged += 1
        elif r["size"] is None:
            # у записей из старых журналов размера нет — без него пульт
            # занижал бы объём карантина
            con.execute("UPDATE quarantine SET size=? WHERE id=?", (os.path.getsize(r["path"]), r["id"]))
    known = {r["path"] for r in con.execute("SELECT path FROM quarantine")}
    added = 0
    for root_dir, reason in ((env.DUPES_DIR, "duplicate"), (env.BROKEN_DIR, "broken")):
        if not os.path.isdir(root_dir):
            continue
        for dirpath, _, files in os.walk(root_dir):
            for f in files:
                if not f.lower().endswith(env.AUDIO_EXT):
                    continue
                p = os.path.join(dirpath, f)
                if p in known:
                    continue
                rel = os.path.relpath(p, root_dir)
                orig = os.path.join(env.MUSIC_DIR, rel) if reason == "duplicate" else os.path.join(env.INCOMING_DIR, rel)
                moved = datetime.datetime.fromtimestamp(os.path.getmtime(p)).isoformat(timespec="seconds")
                con.execute(
                    "INSERT OR IGNORE INTO quarantine(path, original_path, reason, moved_at, size, title) "
                    "VALUES (?,?,?,?,?,?)",
                    (p, orig, reason, moved, os.path.getsize(p), os.path.splitext(f)[0]))
                added += 1
    return purged, added


def import_legacy(con):
    """Перенести в базу журналы, которые писались до неё.

    consolidate.journal — JSONL с полем op; applied.jsonl — записи retry.py и
    albumgroup.py без op (albumgroup помечен why="обложка"). Повторный запуск
    ничего не задваивает: уже перенесённые строки узнаются по отметке времени,
    пути и операции.
    """
    seen = {(r["ts"], r["op"], r["path"]) for r in con.execute("SELECT ts, op, path FROM events")}
    n = q = 0

    def add(ts, script, op, path, item_id, before, after):
        nonlocal n
        if (ts, op, path) in seen:
            return
        con.execute("INSERT INTO events(ts, script, op, item_id, path, before, after) VALUES (?,?,?,?,?,?,?)",
                    (ts, script, op, item_id, path, _j(before), _j(after)))
        seen.add((ts, op, path))
        n += 1

    if os.path.exists(env.CONSOLIDATE_JOURNAL):
        for line in open(env.CONSOLIDATE_JOURNAL, encoding="utf-8", errors="replace"):
            try:
                rec = json.loads(line)
            except Exception:
                continue
            op = rec.pop("op", "?")
            ts = rec.pop("date", now())
            item_id = rec.pop("id", None)
            before, after = rec.pop("before", None), rec.pop("after", None)
            src, dst = rec.pop("src", None), rec.pop("dst", None)
            path = rec.pop("path", None) or src
            if src or dst:
                before = dict(before or {}, **({"path": src} if src else {}))
                after = dict(after or {}, **({"path": dst} if dst else {}))
            if rec:
                after = dict(after or {}, **rec)
            add(ts, "consolidate", op, path, item_id, before, after)
            if op == "quarantine" and dst:
                why = str((after or {}).get("why", ""))
                reason = "broken" if "бит" in why else "duplicate"
                sim = None
                digits = "".join(ch for ch in why if ch.isdigit())
                if digits:
                    sim = int(digits) / 100.0
                cur = con.execute(
                    "INSERT OR IGNORE INTO quarantine(path, original_path, reason, similarity, album_id, moved_at, title, artist) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (dst, src, reason, sim, (after or {}).get("album_id"), ts,
                     (after or {}).get("title"), (after or {}).get("artist")))
                q += cur.rowcount

    if os.path.exists(env.JOURNAL):
        for line in open(env.JOURNAL, encoding="utf-8", errors="replace"):
            try:
                rec = json.loads(line)
            except Exception:
                continue
            why = rec.get("why", "")
            script = "albumgroup" if why == "обложка" else "retry"
            op = "album" if script == "albumgroup" else "match"
            after = dict(rec.get("after") or {})
            if "d" in rec:
                after["distance"] = rec["d"]
            if why:
                after["why"] = why
            add(rec.get("date", now()), script, op, rec.get("path"), None, rec.get("before"), after)
    return n, q


# ---------------------------------------------------------------- CLI для shell
def _cli(argv):
    con = connect()
    cmd = argv[0] if argv else "migrate"
    if cmd == "migrate":
        print("ok")
    elif cmd == "run-start":
        print(start_run(con, argv[1] if len(argv) > 1 else "manual"))
    elif cmd == "run-finish":
        finish_run(con, int(argv[1]), argv[2] if len(argv) > 2 else "ok")
    elif cmd == "step-start":
        print(start_step(con, int(argv[1]), argv[2]))
    elif cmd == "step-finish":
        stats = loads(argv[3]) if len(argv) > 3 else None
        finish_step(con, int(argv[1]), argv[2] if len(argv) > 2 else "ok", stats)
    elif cmd == "get":
        # значение настройки для shell: janitordb.py get nightly_hour "$NIGHTLY_HOUR".
        # Заданное в пульте главнее; не задано — значение из окружения, а
        # встроенное умолчание — последним
        row = con.execute("SELECT value FROM settings WHERE key=?", (argv[1],)).fetchone()
        if row is not None:
            print(row["value"])
        else:
            print(argv[2] if len(argv) > 2 and argv[2] != "" else DEFAULTS.get(argv[1], ""))
    elif cmd == "meta-set":
        meta_set(con, argv[1], argv[2])
    elif cmd == "sync-quarantine":
        purged, added = sync_quarantine(con)
        print("карантин: удалено по сроку %d, зарегистрировано без записи %d" % (purged, added))
    elif cmd == "import-legacy":
        n, q = import_legacy(con)
        purged, added = sync_quarantine(con)
        print("перенесено событий: %d, записей карантина из журнала: %d, найдено на диске без записи: %d"
              % (n, q, added))
    else:
        sys.exit("неизвестная команда: %s" % cmd)


if __name__ == "__main__":
    _cli(sys.argv[1:])
