#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Скачивание через downtify: то, что решено скачать, — в его очередь.

downtify качает только по ссылке на трек Spotify (или на видео YouTube),
а дискография разбирается по Deezer. Мост между ними — ISRC: он есть у
каждой дорожки Deezer, и поиск Spotify `isrc:…` находит тот же трек одним
запросом. Если качается весь релиз, хватает одного запроса по UPC альбома —
тогда downtify получает дорожки вместе с номерами и названием альбома.

Запросы к Spotify идут через sitecustomize.py: кэш, темп и суточный лимит
общие с остальными скриптами.

Звук downtify берёт с YouTube Music, откуда и подмены. После импорта
каждую скачанную дорожку сверит verify.py.

Поиск в самом downtify для этого не годится: он ищет на YouTube Music и на
запрос по ISRC отвечает посторонними песнями.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests  # noqa: E402

import discography as disco  # noqa: E402
import env  # noqa: E402
import janitordb as jdb  # noqa: E402

LEN_OK = 5          # секунд расхождения длины между Deezer и Spotify


# ---------------------------------------------------------------- Spotify
_token = {"value": None, "until": 0}


def credentials():
    """Ключи Spotify: из окружения, а если там нет — из конфига beets."""
    cid, secret = os.environ.get("SPOTIFY_CLIENT_ID"), os.environ.get("SPOTIFY_CLIENT_SECRET")
    if cid and secret:
        return cid, secret
    try:
        from beets import config
        config.read()
        return config["spotify"]["client_id"].as_str(), config["spotify"]["client_secret"].as_str()
    except Exception:
        return None, None


def token():
    if _token["value"] and time.time() < _token["until"] - 60:
        return _token["value"]
    cid, secret = credentials()
    if not cid:
        raise RuntimeError("нет ключей Spotify (SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET)")
    r = requests.post("https://accounts.spotify.com/api/token", data={"grant_type": "client_credentials"},
                      auth=(cid, secret), timeout=30)
    r.raise_for_status()
    j = r.json()
    _token["value"], _token["until"] = j["access_token"], time.time() + int(j.get("expires_in", 3600))
    return _token["value"]


def spotify(path, **params):
    r = requests.get("https://api.spotify.com/v1/" + path, params=params,
                     headers={"Authorization": "Bearer " + token()}, timeout=30)
    if r.status_code != 200:
        return None
    return r.json()


def track_by_isrc(isrc, duration=None):
    j = spotify("search", q="isrc:" + isrc, type="track", limit=5) or {}
    for t in (j.get("tracks") or {}).get("items") or []:
        if duration and abs(t.get("duration_ms", 0) / 1000.0 - duration) > LEN_OK:
            continue
        return t["id"]
    return None


def track_by_name(artist, title, duration):
    """Запасной путь, когда ISRC не нашёлся: имя и длина должны сойтись."""
    q = 'track:"%s" artist:"%s"' % (disco.norm_title(title) or title, artist)
    j = spotify("search", q=q, type="track", limit=10) or {}
    for t in (j.get("tracks") or {}).get("items") or []:
        names = [a.get("name") for a in t.get("artists") or []]
        if not any(disco.name_keys(n) & disco.name_keys(artist) for n in names):
            continue
        if duration and abs(t.get("duration_ms", 0) / 1000.0 - duration) > LEN_OK:
            continue
        if disco.norm_title(t.get("name")) != disco.norm_title(title):
            continue
        return t["id"]
    return None


def album_by_upc(upc):
    j = spotify("search", q="upc:" + upc, type="album", limit=3) or {}
    items = (j.get("albums") or {}).get("items") or []
    return items[0]["id"] if items else None


# ---------------------------------------------------------------- downtify
def downtify_songs(url):
    """Песни в формате downtify для ссылки Spotify (трек или альбом)."""
    r = requests.get(env.DOWNTIFY_URL + "/api/url", params={"url": url}, timeout=120)
    r.raise_for_status()
    j = r.json()
    return j if isinstance(j, list) else [j]


def downtify_batch(songs):
    r = requests.post(env.DOWNTIFY_URL + "/api/download/batch",
                      json={"songs": songs, "generate_m3u": False}, timeout=120)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------- релиз
def note_track(con, t, sid, searched=True):
    """Запомнить у дорожки, под каким id Spotify она ушла в downtify.

    По этому id follow.py узнает её в фонотеке: название и имя исполнителя в
    Spotify бывают другими, чем в Deezer. spotify_id = None — искали, но в
    Spotify её нет; ключа нет вовсе — ушла с альбомом, id не сопоставился.
    """
    info = jdb.loads(t["info"], {})
    if sid or searched:
        info["spotify_id"] = sid
    else:
        info.pop("spotify_id", None)
    con.execute("UPDATE release_tracks SET info=? WHERE id=?", (json.dumps(info, ensure_ascii=False), t["id"]))


def album_track_ids(tracks, songs):
    """Какой дорожке релиза какая песня альбома Spotify: по названию и длине,
    а не сошлось — по номеру, если длина та же (альбом найден по UPC, порядок
    дорожек тот же)."""
    out = {}
    for n, t in enumerate(tracks):
        for s in songs:
            if disco.title_keys(s.get("name")) & disco.title_keys(t["title"]) and \
                    abs(float(s.get("duration") or 0) - (t["duration"] or 0)) <= LEN_OK:
                out[t["id"]] = s.get("song_id")
                break
        else:
            s = songs[n] if len(songs) == len(tracks) else None
            if s is not None and abs(float(s.get("duration") or 0) - (t["duration"] or 0)) <= LEN_OK:
                out[t["id"]] = s.get("song_id")
    return out


def release(con, release_id, artist_name=None, retry=False):
    """Отправить в downtify то, что в релизе решено скачать. Вернуть отчёт.

    retry — повтор после «не скачалось»: качаем только то, что так и не
    доехало до фонотеки, иначе доехавшее легло бы в incoming второй копией.
    """
    rel = con.execute("SELECT r.*, a.name AS artist, a.deezer_name FROM releases r "
                      "LEFT JOIN artists a ON a.id = r.artist_id WHERE r.id=?", (release_id,)).fetchone()
    if rel is None:
        raise ValueError("нет такого релиза")
    rows = con.execute("SELECT * FROM release_tracks WHERE release_id=? ORDER BY id", (release_id,)).fetchall()
    want = [t for t in rows if disco.wanted(t)]
    if want and retry:
        import follow
        want = follow.not_arrived(disco.Library(), rel, want)
        if not want:
            con.execute("UPDATE releases SET status='in_library', note=NULL WHERE id=?", (release_id,))
            return "всё из релиза уже в фонотеке"
    if not want:
        return "в релизе нечего качать"
    artist = artist_name or rel["deezer_name"] or rel["artist"] or ""

    songs, missing = [], []
    # весь релиз — одним альбомом: так downtify сохранит номера дорожек
    if len(want) == len(rows) and rel["provider"] == "deezer":
        alb = disco.deezer("album/%s" % rel["provider_id"]) or {}
        sid = album_by_upc(alb["upc"]) if alb.get("upc") else None
        if sid:
            songs = downtify_songs("https://open.spotify.com/album/" + sid)
            ids = album_track_ids(want, songs)
            for t in want:
                note_track(con, t, ids.get(t["id"]), searched=False)
    if not songs:
        for t in want:
            sid = (track_by_isrc(t["isrc"], t["duration"]) if t["isrc"] else None) or \
                track_by_name(artist, t["title"], t["duration"])
            note_track(con, t, sid)
            if not sid:
                missing.append(t["title"])
                continue
            songs.extend(downtify_songs("https://open.spotify.com/track/" + sid))
    if not songs:
        raise RuntimeError("в Spotify не нашлось ни одной дорожки: %s" % ", ".join(missing[:5]))

    downtify_batch(songs)
    con.execute("UPDATE releases SET status='downloading', decided_at=?, note=NULL WHERE id=?",
                (jdb.now(), release_id))
    jdb.log_event(con, "download", "queue", None,
                  {"release": rel["title"], "artist": artist, "tracks": len(songs), "missing": missing})
    msg = "в очередь downtify: %d дорожек «%s»" % (len(songs), rel["title"])
    if missing:
        msg += "; в Spotify не нашлось: %s" % ", ".join(missing[:5])
    return msg


if __name__ == "__main__":
    # ручная проверка: download.py <id релиза> — без --go только показать,
    # какие дорожки нашлись бы в Spotify
    rid = int(sys.argv[1])
    c = jdb.connect()
    if "--go" in sys.argv:
        print(release(c, rid))
    else:
        for t in c.execute("SELECT * FROM release_tracks WHERE release_id=?", (rid,)).fetchall():
            if disco.wanted(t):
                print("%-40s %-12s -> %s" % (t["title"][:40], t["isrc"], track_by_isrc(t["isrc"], t["duration"])
                                            if t["isrc"] else "нет ISRC"))
