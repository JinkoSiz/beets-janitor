#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сверка звука с превью источника: та ли песня лежит под этими тегами.

downtify берёт звук только с YouTube Music и иногда приносит другую песню под
верными тегами — так в «Дакимакуре» и «ЗОНА ЭТО АД» оказались чужие записи.
Теги при этом безупречны: beets сматчил их по названию, и ни одна проверка по
тегам подмену не видит. Видно её только по звуку. У Spotify и Deezer есть
30-секундные превью, и отпечаток превью должен найтись внутри файла:

  от ref_ok  (0.85)   та же запись
  ниже ref_bad (0.60) другая песня: вопрос «подмена» в пульт
  между               вопрос «проверить» в пульт

Верные записи на проверке дали 92–98%, чужие песни — 53–56%. Не ровно 50:
берётся лучший из тысяч сдвигов, и уже одно это поднимает случайное
совпадение. Чем длиннее файл, тем больше сдвигов и тем выше этот шум.

Откуда превью — по формату идентификатора, как в covers.py:

  22 знака base62  -> Spotify: embed-страница трека, ключ не нужен
  только цифры     -> Deezer: открытый API
  UUID с дефисами  -> MusicBrainz -> ISRC -> Deezer

Отпечаток файла снимается целиком (fpcalc -length 0): по умолчанию fpcalc
берёт первые 120 секунд, а превью бывает вырезано и из третьей минуты — тогда
верная запись выглядела бы подменой.

Ограничение метода: инструментал и вокальная версия одной песни дают 86–96%,
звук у них общий. Такую подмену отпечаток не различает.

Файлы не трогает. Каждая дорожка сверяется один раз; повторно — только если
файл заменили (изменился размер).

  verify.py            новые импорты, затем остальная фонотека — не больше
                       verify_limit дорожек за прогон, свежие первыми
  verify.py --new      только новые импорты (сторож зовёт после incoming)
  verify.py --item N   сверить одну дорожку и подробно показать результат
  verify.py --dry      ничего не записывать
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests  # noqa: E402

import env  # noqa: E402
import janitordb as jdb  # noqa: E402

DRY = "--dry" in sys.argv
NEW_ONLY = "--new" in sys.argv
PAUSE = 1.0
NEW_CAP = 300          # столько новых импортов сверяем за раз, остальное — ночью
BREAKER = 8            # столько сбоев подряд — и источник считаем недоступным
RECHECK_FAILED = 1     # через сколько дней повторять сбойную сверку
RECHECK_NO_PREVIEW = 30

UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
SPOTIFY = re.compile(r"^[0-9A-Za-z]{22}$")
DEEZER = re.compile(r"^\d+$")

BROWSER = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
# MusicBrainz требует представиться, безымянных режет
MB_AGENT = {"User-Agent": "beets-janitor/1.0 (https://github.com/JinkoSiz/beets-janitor)"}


def get(url, headers=None, **kw):
    try:
        r = requests.get(url, timeout=30, headers=headers or BROWSER, **kw)
        return r if r.status_code == 200 else None
    except Exception:
        return None


def _json(r):
    try:
        return r.json() if r is not None else None
    except Exception:
        return None


# ---------------------------------------------------------------- откуда превью
# Каждая функция возвращает (ref, None) или (None, почему_нет).
# ref: provider, id, url превью, title, artists, duration (с), link.

def ref_spotify(tid):
    r = get("https://open.spotify.com/embed/track/" + tid)
    if r is None:
        return None, "failed"
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
    if not m:
        return None, "failed"
    try:
        data = json.loads(m.group(1))
        e = data["props"]["pageProps"]["state"]["data"]["entity"]
    except Exception:
        return None, "failed"
    url = (e.get("audioPreview") or {}).get("url")
    if not url:
        return None, "no_preview"
    return {"provider": "spotify", "id": tid, "url": url, "title": e.get("name"),
            "artists": [a.get("name") for a in e.get("artists") or [] if a.get("name")],
            "duration": round((e.get("duration") or 0) / 1000.0, 1),
            "link": "https://open.spotify.com/track/" + tid}, None


def _deezer_ref(j, isrc=None):
    who = (j.get("artist") or {}).get("name")
    ref = {"provider": "deezer", "id": str(j.get("id")), "url": j.get("preview"),
           "title": j.get("title"), "artists": [who] if who else [],
           "duration": j.get("duration"), "link": j.get("link")}
    if isrc:
        ref["isrc"] = isrc
    return ref


def ref_deezer(tid):
    j = _json(get("https://api.deezer.com/track/" + tid))
    if j is None:
        return None, "failed"
    if j.get("error"):
        return None, "no_ref"
    if not j.get("preview"):
        return None, "no_preview"
    return _deezer_ref(j), None


def ref_musicbrainz(tid):
    j = _json(get("https://musicbrainz.org/ws/2/recording/" + tid,
                  headers=MB_AGENT, params={"inc": "isrcs", "fmt": "json"}))
    if j is None:
        return None, "failed"
    isrcs = j.get("isrcs") or []
    if not isrcs:
        return None, "no_ref"
    for isrc in isrcs[:3]:
        time.sleep(PAUSE)
        d = _json(get("https://api.deezer.com/track/isrc:" + isrc))
        if d and not d.get("error") and d.get("preview"):
            return _deezer_ref(d, isrc), None
    return None, "no_preview"


def reference(tid):
    tid = str(tid or "").strip()
    if SPOTIFY.match(tid):
        return ref_spotify(tid)
    if DEEZER.match(tid):
        return ref_deezer(tid)
    if UUID.match(tid):
        return ref_musicbrainz(tid)
    return None, "no_ref"


# ---------------------------------------------------------------- отпечатки
def fingerprint(path):
    """Отпечаток всего файла: -length 0 снимает ограничение в 120 секунд."""
    try:
        out = subprocess.run([env.FPCALC, "-raw", "-json", "-length", "0", path],
                             capture_output=True, text=True, timeout=300).stdout
        return list(json.loads(out).get("fingerprint") or [])
    except Exception:
        return []


def preview_fingerprint(url):
    r = get(url)
    if r is None or len(r.content) < 10000:
        return []
    tf = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    try:
        tf.write(r.content)
        tf.close()
        return fingerprint(tf.name)
    finally:
        os.unlink(tf.name)


POP = bytes(bin(i).count("1") for i in range(256))


def _bits(x):
    x &= 0xFFFFFFFF
    return POP[x & 255] + POP[(x >> 8) & 255] + POP[(x >> 16) & 255] + POP[x >> 24]


def _score(short, long_, off, step=1):
    idx = range(0, len(short), step)
    err = sum(_bits(short[i] ^ long_[off + i]) for i in idx)
    return 1.0 - err / (32.0 * len(idx))


def best_match(short, long_):
    """Доля совпавших бит там, где превью лучше всего ложится в файл.

    Превью — кусок из середины трека, и где именно он вырезан, неизвестно:
    перебираем все сдвиги. Сначала грубо, по каждому восьмому значению, затем
    точно — вокруг лучших мест. Выходит в разы быстрее полного перебора, а
    ошибиться грубый проход не может: у той же записи совпадение ~95% бит,
    у чужой ~50%, и тридцати значений хватает, чтобы их различить.
    None — сравнить не удалось.
    """
    if len(short) < 40 or len(long_) < 40:
        return None
    if len(short) > len(long_):
        short, long_ = long_, short
    last = len(long_) - len(short)
    coarse = sorted(((_score(short, long_, off, 8), off) for off in range(last + 1)), reverse=True)[:8]
    best = 0.0
    for _, off in coarse:
        for o in range(max(0, off - 3), min(last, off + 3) + 1):
            best = max(best, _score(short, long_, o))
    return best


# ---------------------------------------------------------------- решения и журнал
def thresholds(con):
    def f(key, default):
        try:
            return float(jdb.setting(con, key, default) if con is not None else jdb.DEFAULTS.get(key, default))
        except Exception:
            return float(default)
    return f("ref_ok", "0.85"), f("ref_bad", "0.60")


def verdict_of(sim, ok_t, bad_t):
    if sim is None:
        return "failed"
    if sim >= ok_t:
        return "ok"
    return "substitution" if sim < bad_t else "unsure"


def review_key(item_id, size):
    # размер в ключе: заменённый файл — новый вопрос, а ответ «оставить»
    # на старый файл не должен заглушить проверку нового
    return "verify:%d:%d" % (item_id, size)


def record(con, it, path, size, ref, sim, verdict):
    if DRY or con is None:
        return
    con.execute(
        "INSERT INTO checks(item_id, path, size, provider, ref_id, similarity, verdict, checked_at) "
        "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(item_id) DO UPDATE SET path=excluded.path, "
        "size=excluded.size, provider=excluded.provider, ref_id=excluded.ref_id, "
        "similarity=excluded.similarity, verdict=excluded.verdict, checked_at=excluded.checked_at",
        (it.id, path, size, ref and ref["provider"], ref and ref["id"],
         None if sim is None else round(sim, 3), verdict, jdb.now()))
    if verdict in ("substitution", "unsure"):
        payload = {"path": path, "item_id": it.id, "artist": str(it.artist), "title": str(it.title),
                   "album": str(it.album), "length": round(float(it.length or 0), 1),
                   "similarity": round(sim, 3), "level": "bad" if verdict == "substitution" else "unsure",
                   "ref": {k: ref.get(k) for k in ("provider", "id", "title", "artists", "duration",
                                                     "link", "url", "isrc") if ref.get(k) is not None}}
        jdb.ask(con, "substitution", review_key(it.id, size),
                "%s — %s" % (it.artist, it.title), payload)
    elif verdict == "ok":
        # файл заменили, и теперь он верный — старый вопрос снимаем
        con.execute("UPDATE reviews SET status='resolved', decision='verified', decided_at=? "
                    "WHERE key LIKE ? AND status='open'", (jdb.now(), "verify:%d:%%" % it.id))


def due(row, size):
    """Нужна ли сверка: не сверяли, файл заменили или пора повторить неудачную."""
    if row is None or row["size"] != size:
        return True
    age = days_since(row["checked_at"])
    if row["verdict"] == "failed":
        return age >= RECHECK_FAILED
    if row["verdict"] == "no_preview":
        return age >= RECHECK_NO_PREVIEW
    return False


def days_since(ts):
    import datetime
    try:
        return (datetime.datetime.now() - datetime.datetime.fromisoformat(ts)).days
    except Exception:
        return 999


def log_imports(con, items):
    """Новые дорожки с прошлого раза — в журнал, для «Последних импортов».

    Граница — отметка added самой свежей дорожки, которую мы уже видели.
    На первом запуске её нет, и в журнал ничего не пишем: иначе вся
    фонотека разом оказалась бы «только что импортированной».
    """
    newest = max((float(it.added or 0) for it in items), default=0.0)
    mark = float(jdb.meta_get(con, "imports_seen_until", 0) or 0) if con is not None else 0.0
    if not mark:
        if not DRY and con is not None:
            jdb.meta_set(con, "imports_seen_until", newest)
        return []
    new = sorted((it for it in items if float(it.added or 0) > mark), key=lambda i: float(i.added or 0))
    if DRY or con is None:
        return new
    for it in new:
        jdb.log_event(con, "import", "import", None,
                      {"artist": str(it.artist), "title": str(it.title), "album": str(it.album),
                       "singleton": bool(it.singleton), "source": str(it.get("data_source") or ""),
                       "track_id": str(it.get("mb_trackid") or ""), "format": str(it.format),
                       "bitrate": int(it.bitrate or 0) // 1000},
                      item_id=it.id, path=it.path.decode("utf-8", "replace"))
    jdb.meta_set(con, "imports_seen_until", newest)
    return new


# ---------------------------------------------------------------- сверка
def check(con, it, ok_t, bad_t, verbose=False):
    """Сверить одну дорожку. Вернуть вердикт."""
    path = it.path.decode("utf-8", "replace")
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    ref, why = reference(it.get("mb_trackid"))
    if ref is None:
        record(con, it, path, size, None, None, why)
        if verbose:
            print("   превью нет: %s" % why)
        return why
    pf = preview_fingerprint(ref["url"])
    ff = fingerprint(path) if pf else []
    sim = best_match(pf, ff) if pf and ff else None
    verdict = verdict_of(sim, ok_t, bad_t)
    record(con, it, path, size, ref, sim, verdict)
    if verbose or verdict in ("substitution", "unsure"):
        mark = {"substitution": "!! ПОДМЕНА", "unsure": "?  проверить"}.get(verdict, "   " + verdict)
        print("%s %3s%%  %-26s %-30s <- %s «%s»%s"
              % (mark, "—" if sim is None else "%.0f" % (sim * 100),
                 str(it.artist)[:26], str(it.title)[:30], ref["provider"], str(ref.get("title"))[:30],
                 "" if not ref.get("duration") or not it.length
                 else "  (%.0fс против %.0fс)" % (float(it.length), float(ref["duration"]))))
    return verdict


def main():
    import retry
    lib = retry.open_library()
    con = None
    if not DRY or os.path.exists(env.JANITOR_DB):
        con = jdb.connect()
    ok_t, bad_t = thresholds(con)

    if "--item" in sys.argv:
        iid = int(sys.argv[sys.argv.index("--item") + 1])
        it = lib.get_item(iid)
        if it is None:
            sys.exit("нет дорожки %d" % iid)
        print("%s — %s  [%s]" % (it.artist, it.title, it.get("mb_trackid")))
        check(con, it, ok_t, bad_t, verbose=True)
        return

    items = [it for it in lib.items("") if os.path.isfile(it.path.decode("utf-8", "replace"))]
    new = log_imports(con, items)
    print("новых импортов: %d" % len(new))

    rows = {r["item_id"]: r for r in con.execute("SELECT * FROM checks")} if con is not None else {}

    def pending(it):
        try:
            return due(rows.get(it.id), os.path.getsize(it.path.decode("utf-8", "replace")))
        except OSError:
            return False

    # новые импорты первыми и целиком: подмена в свежей закачке важнее всего
    queue = [it for it in reversed(new) if pending(it)][:NEW_CAP]
    backlog = 0
    if not NEW_ONLY:
        limit = int(float(jdb.setting(con, "verify_limit") if con is not None else jdb.DEFAULTS["verify_limit"]))
        seen = {it.id for it in queue}
        rest = sorted((it for it in items if it.id not in seen and pending(it)),
                      key=lambda i: -float(i.added or 0))
        take = max(0, limit - len(queue))
        queue += rest[:take]
        backlog = max(0, len(rest) - take)
    print("к сверке: %d%s" % (len(queue), "" if NEW_ONLY else ", останется на следующие ночи: %d" % backlog))

    stats = {"new": len(new)}
    streak = 0
    for it in queue:
        v = check(con, it, ok_t, bad_t)
        if v is None:
            continue
        stats[v] = stats.get(v, 0) + 1
        streak = streak + 1 if v == "failed" else 0
        if streak >= BREAKER:
            print("!! %d сбоев подряд — источник не отвечает, остальное в следующий раз" % streak)
            break
        time.sleep(PAUSE)

    print("сверено: %s" % ", ".join("%s %d" % (k, v) for k, v in sorted(stats.items())))
    if con is not None:
        total = con.execute("SELECT count(*) FROM checks").fetchone()[0]
        print("всего сверено за всё время: %d из %d дорожек" % (total, len(items)))
    stats["backlog"] = backlog
    stats_out = os.environ.get("JANITOR_STATS_OUT")
    if stats_out and not DRY:
        with open(stats_out, "w", encoding="utf-8") as f:
            json.dump(stats, f)


if __name__ == "__main__":
    main()
