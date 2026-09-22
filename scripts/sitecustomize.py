import datetime
import os
import socket
import sqlite3
import time
import zlib
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import env  # noqa: E402

socket.setdefaulttimeout(120)

LOG = env.NET_LOG
PACE_FILE = env.SPOTIFY_PACE_FILE
BUDGET_FILE = env.SPOTIFY_BUDGET_FILE
CACHE_DB = env.SPOTIFY_CACHE_DB
BREAKER_FILE = env.SPOTIFY_BREAKER_FILE
BREAKER_FAILS = 5
BREAKER_MAX = 6 * 3600
SPOTIFY_DAILY = env.SPOTIFY_DAILY
SLOW = 15.0
SLEEP_CAP = 45.0
PACE_MIN = 0.5
PACE_MAX = 3.0
PACE_STEP = 0.05
# Поиск живёт сутки: добивание раз в 1-2-4-8 дней должно СПРАШИВАТЬ ЗАНОВО,
# а не получать вчерашний ответ 'не найдено'. Внутри одного прогона кэш
# всё равно снимает повторы. Справочники (треки, альбомы по id) неизменны.
TTL_SEARCH = 86400
TTL_OBJECT = 90 * 86400

_orig_sleep = time.sleep


def _log(kind, url, elapsed, err=""):
    try:
        with open(LOG, "a") as f:
            f.write("%s %-6s %6.1fs %s %s\n" % (
                datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                kind, elapsed, str(url)[:110], err))
    except Exception:
        pass


# --------------------------------------------------------------- кэш Spotify
# Официальная рекомендация Spotify при упоре в лимит — кэшировать.
# track_for_id тратит 2 запроса на трек (трек + его альбом), причём альбом
# повторяется для каждой дорожки. Плюс ночное добивание переспрашивает одни
# и те же треки каждую ночь. Кэш убирает и то, и другое.
def _cache():
    try:
        db = sqlite3.connect(CACHE_DB, timeout=10)
        db.execute("create table if not exists c"
                   " (url text primary key, status int, body blob, ts int)")
        return db
    except Exception:
        return None


def _cache_get(url):
    db = _cache()
    if not db:
        return None
    try:
        row = db.execute("select status, body, ts from c where url=?", (url,)).fetchone()
        if not row:
            return None
        ttl = TTL_SEARCH if "/search" in url else TTL_OBJECT
        if time.time() - row[2] > ttl:
            return None
        return row[0], zlib.decompress(row[1])
    except Exception:
        return None
    finally:
        db.close()


def _cache_put(url, status, body):
    db = _cache()
    if not db:
        return
    try:
        db.execute("insert or replace into c values (?,?,?,?)",
                   (url, status, zlib.compress(body), int(time.time())))
        db.commit()
    except Exception:
        pass
    finally:
        db.close()


# ------------------------------------------------------- предохранитель
# Пока Spotify отдаёт 429, каждый запрос — потеря: ждём паузу, тратим бюджет,
# получаем отказ. Spotify в заголовке Retry-After сам говорит, сколько ждать —
# уважаем это и на время отключаемся, не трогая сеть вообще.
def _breaker_until():
    try:
        return float(open(BREAKER_FILE).read().split()[0])
    except Exception:
        return 0.0


def _breaker_fails():
    try:
        return int(open(BREAKER_FILE).read().split()[1])
    except Exception:
        return 0


def _breaker_set(until, fails):
    try:
        with open(BREAKER_FILE, "w") as f:
            f.write("%.0f %d" % (until, fails))
    except Exception:
        pass


def _breaker_open():
    return time.time() < _breaker_until()


def _breaker_hit(retry_after):
    fails = _breaker_fails() + 1
    until = 0.0
    if fails >= BREAKER_FAILS:
        wait = min(float(retry_after or 1800), BREAKER_MAX)
        until = time.time() + wait
        _log("BREAKER", "spotify отключён", wait, "после %d отказов подряд" % fails)
        fails = 0
    _breaker_set(until, fails)


def _breaker_ok():
    if _breaker_fails() or _breaker_until():
        _breaker_set(0.0, 0)


def _read_pace():
    try:
        return float(open(PACE_FILE).read().strip())
    except Exception:
        return PACE_MIN


_pace = [_read_pace()]
_last = [0.0]


def _save_pace():
    try:
        with open(PACE_FILE, "w") as f:
            f.write("%.2f" % _pace[0])
    except Exception:
        pass


def _slower():
    _pace[0] = min(_pace[0] * 1.5, PACE_MAX)
    _save_pace()


def _faster():
    if _pace[0] > PACE_MIN:
        _pace[0] = max(PACE_MIN, _pace[0] - PACE_STEP)
        _save_pace()


def _budget_ok():
    today = datetime.date.today().isoformat()
    day, used = today, 0
    try:
        day, raw = open(BUDGET_FILE).read().split()
        used = int(raw)
    except Exception:
        pass
    if day != today:
        used = 0
    if used >= SPOTIFY_DAILY:
        return False
    try:
        with open(BUDGET_FILE, "w") as f:
            f.write("%s %d" % (today, used + 1))
    except Exception:
        pass
    return True


def _throttle():
    wait = _pace[0] - (time.monotonic() - _last[0])
    if wait > 0:
        _orig_sleep(wait)
    _last[0] = time.monotonic()


try:
    import requests
    import requests.adapters as _ra

    _orig_send = _ra.HTTPAdapter.send

    def _make_response(request, status, body):
        r = requests.models.Response()
        r.status_code = status
        r._content = body
        r.url = request.url
        r.request = request
        r.encoding = "utf-8"
        r.headers["Content-Type"] = "application/json"
        return r

    def _send(self, request, **kw):
        url = request.url or ""
        spotify = "api.spotify.com" in url
        cacheable = spotify and request.method == "GET"

        if cacheable:
            hit = _cache_get(url)
            if hit:
                return _make_response(request, hit[0], hit[1])
        if spotify and _breaker_open():
            raise requests.exceptions.ConnectionError("spotify на паузе после отказов")
        if cacheable:
            if not _budget_ok():
                _log("BUDGET", url, 0, "суточный лимит %d исчерпан" % SPOTIFY_DAILY)
                raise requests.exceptions.ConnectionError("spotify daily budget exhausted")
            _throttle()
        elif spotify:
            if not _budget_ok():
                _log("BUDGET", url, 0, "суточный лимит исчерпан")
                raise requests.exceptions.ConnectionError("spotify daily budget exhausted")
            _throttle()

        if not kw.get("timeout"):
            kw["timeout"] = (30, 120)
        try:
            request.headers["Connection"] = "close"
        except Exception:
            pass
        t = time.time()
        try:
            r = _orig_send(self, request, **kw)
        except Exception as e:
            _log("FAIL", url, time.time() - t, type(e).__name__)
            raise
        d = time.time() - t
        if spotify:
            if getattr(r, "status_code", 0) == 429:
                _slower()
                _breaker_hit(r.headers.get("Retry-After"))
                _log("429", url, d, "пауза -> %.2fs" % _pace[0])
            else:
                _faster()
                _breaker_ok()
                if cacheable and r.status_code in (200, 404):
                    try:
                        _cache_put(url, r.status_code, r.content)
                    except Exception:
                        pass
        if d >= SLOW:
            _log("SLOW", url, d)
        return r

    _ra.HTTPAdapter.send = _send
except Exception:
    pass


try:
    import urllib.request as _ur

    _orig_open = _ur.OpenerDirector.open

    def _open(self, fullurl, *a, **kw):
        u = getattr(fullurl, "full_url", fullurl)
        t = time.time()
        try:
            r = _orig_open(self, fullurl, *a, **kw)
        except Exception as e:
            _log("FAIL", u, time.time() - t, type(e).__name__)
            raise
        d = time.time() - t
        if d >= SLOW:
            _log("SLOW", u, d)
        return r

    _ur.OpenerDirector.open = _open
except Exception:
    pass


def _capped_sleep(sec):
    try:
        if sec and sec > SLEEP_CAP:
            _log("SLEEP", "ожидание урезано", float(sec), "-> %ds" % int(SLEEP_CAP))
            sec = SLEEP_CAP
    except Exception:
        pass
    return _orig_sleep(sec)


time.sleep = _capped_sleep

try:
    import faulthandler
    import signal
    faulthandler.register(signal.SIGUSR1, all_threads=True)
except Exception:
    pass
