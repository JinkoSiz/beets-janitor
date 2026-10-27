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

Отказ Spotify целиком (нет Premium у владельца приложения, предохранитель,
сеть) — не «трека нет»: релиз становится «не скачалось» с этой причиной, а
ночью follow.py повторяет его сам, не засчитывая такую попытку.

Звук downtify берёт с YouTube Music, откуда и подмены. После импорта
каждую скачанную дорожку сверит verify.py.

Поиск в самом downtify для этого не годится: он ищет на YouTube Music и на
запрос по ISRC отвечает посторонними песнями.

Поэтому видео YouTube Music ищем сами, по имени, и берём, только если
сошлись исполнитель, название и длина (youtube_match). Найденное видео
передаём downtify в youtube_id — тогда он качает его, а не то, что выберет
сам: «Скажи мне кто ты» он заменил на «Скажи Мне Кто Ты 2», на 21 секунду
короче (2026-10-27). Если трека нет в Spotify вовсе, такое видео — запасной
источник: дорожка уходит с тегами Deezer.

Нет и на YouTube Music — остаётся Soulseek. downtify ищет там сам (slskd),
если он у него включён и стоит в audio_providers первым; тогда дорожка
уходит в downtify с одними тегами Deezer (soulseek_on, deezer_song).
"""
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests  # noqa: E402

import discography as disco  # noqa: E402
import env  # noqa: E402
import janitordb as jdb  # noqa: E402

LEN_OK = 5          # секунд расхождения длины между Deezer и Spotify
# у длинных треков и доля длины: «Am I Evil?» по тому же ISRC в Deezer 465 с,
# в Spotify 470 — 5 секунд отбрасывали его как «нет в Spotify» (2026-10-25)
LEN_REL = 0.015
SPOTIFY_DOWN = "Spotify API не отвечает"   # по этому началу follow.py узнаёт такой сбой


# ---------------------------------------------------------------- Spotify
_token = {"value": None, "until": 0}


class SpotifyUnavailable(RuntimeError):
    """Spotify не ответил по существу — найден трек или нет, неизвестно."""

    def __init__(self, why):
        super().__init__("%s: %s; повторю ночью" % (SPOTIFY_DOWN, why))


def spotify_paused():
    """Открыт предохранитель sitecustomize — почему; закрыт — пустая строка."""
    try:
        until, _fails, *why = open(env.SPOTIFY_BREAKER_FILE).read().split()
        if time.time() < float(until):
            return " ".join(why) or "пауза после отказов"
    except Exception:
        pass
    return ""


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
    try:
        r = requests.post("https://accounts.spotify.com/api/token", data={"grant_type": "client_credentials"},
                          auth=(cid, secret), timeout=30)
        r.raise_for_status()
    except requests.exceptions.RequestException as e:
        raise SpotifyUnavailable("токен не выдан (%s)" % str(e)[:80])
    j = r.json()
    _token["value"], _token["until"] = j["access_token"], time.time() + int(j.get("expires_in", 3600))
    return _token["value"]


def spotify(path, **params):
    """Ответ Spotify. «Не нашлось» — это 200 с пустым списком; любой другой
    ответ значит, что Spotify не ответил вовсе, и это SpotifyUnavailable."""
    try:
        r = requests.get("https://api.spotify.com/v1/" + path, params=params,
                         headers={"Authorization": "Bearer " + token()}, timeout=30)
    except requests.exceptions.RequestException as e:
        # в том числе открытый предохранитель sitecustomize
        raise SpotifyUnavailable(spotify_paused() or str(e)[:80])
    if r.status_code == 403 and "premium" in r.text.lower():
        raise SpotifyUnavailable("нет Premium у владельца приложения")
    if r.status_code != 200:
        raise SpotifyUnavailable("ответ %d" % r.status_code)
    return r.json()


def len_ok(a, b):
    return abs(a - b) <= max(LEN_OK, LEN_REL * max(a, b))


def track_by_isrc(isrc, duration=None, alts=None):
    """id трека Spotify с этим ISRC. Тот же ISRC, но другой длины — в alts:
    это другая версия, брать её решает человек."""
    j = spotify("search", q="isrc:" + isrc, type="track", limit=5) or {}
    for t in (j.get("tracks") or {}).get("items") or []:
        if duration and not len_ok(t.get("duration_ms", 0) / 1000.0, duration):
            if alts is not None:
                alts.append(t)
            continue
        return t["id"]
    return None


def track_by_name(artist, title, duration, alts=None):
    """Запасной путь, когда ISRC не нашёлся: имя и длина должны сойтись.
    Тот же исполнитель и название, но другая длина — в alts."""
    q = 'track:"%s" artist:"%s"' % (disco.norm_title(title) or title, artist)
    j = spotify("search", q=q, type="track", limit=10) or {}
    for t in (j.get("tracks") or {}).get("items") or []:
        names = [a.get("name") for a in t.get("artists") or []]
        if not any(disco.name_keys(n) & disco.name_keys(artist) for n in names):
            continue
        if disco.norm_title(t.get("name")) != disco.norm_title(title):
            continue
        if duration and not len_ok(t.get("duration_ms", 0) / 1000.0, duration):
            if alts is not None:
                alts.append(t)
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


# ---------------------------------------------------------------- YouTube Music
# слова, которыми помечают другую запись той же песни: «Воскрешение» и
# «Воскрешение (Space Club Mix)» у ППК различаются длиной на 4 секунды
VERSION_WORD = re.compile(r"\b(mix|remix|edit|version|rework|bootleg|dub|vip|live|instrumental|acoustic|"
                          r"sped|slowed|intro|outro|interlude|reprise|skit|ремикс|версия|микс|лайв|"
                          r"инструментал|акустик|интро|аутро|скит)\b", re.I)


def versions(title):
    """Пометки версии из скобок и хвоста « - …»: {"space club mix"}."""
    s = str(title or "")
    tail = disco.DASH_TAIL.search(s)
    parts = re.findall(r"[\(\[](.*?)[\)\]]", s) + ([tail.group(0)] if tail else [])
    return {disco.norm_name(p) for p in parts if VERSION_WORD.search(p)}


def _star_norm(s):
    s = str(s or "").lower().replace("ё", "е")
    s = re.sub(r"[\(\[].*?[\)\]]", " ", s)
    return re.sub(r"[^a-zа-я0-9*]+", " ", s).strip()


def same_title(a, b):
    """Одно и то же название. Звёздочки цензуры («НЕ **ЛАНЬ», «си**ки») —
    любые буквы: на YouTube Music название бывает и заглушенным, и полным."""
    if versions(a) != versions(b):
        return False
    if disco.title_keys(a) & disco.title_keys(b):
        return True
    for x, y in ((a, b), (b, a)):
        x = _star_norm(x)
        if "*" in x:
            pat = "[a-zа-я0-9]*".join(re.escape(p) for p in re.split(r"\*+", x))
            if re.fullmatch(pat, _star_norm(y).replace("*", "")):
                return True
    return False


def youtube_match(artists, title, duration):
    """Видео YouTube Music с этой песней: исполнитель, название и длина
    сходятся. None — такого нет или downtify не ответил. Без длины подмену
    не отличить, поэтому и не ищем."""
    names = set()
    for a in artists:
        for n in disco.artist_names(a):
            names |= disco.name_keys(n)
    main = next((a for a in artists if a), "")
    if not duration or not names or not title:
        return None
    for q in ("%s %s" % (main, title), "%s %s" % (title, main)):
        try:
            r = requests.get(env.DOWNTIFY_URL + "/api/songs/search", params={"query": q}, timeout=120)
            r.raise_for_status()
            found = r.json()
        except (requests.exceptions.RequestException, ValueError):
            return None
        for s in found if isinstance(found, list) else []:
            if s.get("source") != "youtube" or not s.get("song_id"):
                continue
            if not any(disco.name_keys(n) & names for a in s.get("artists") or [] for n in disco.artist_names(a)):
                continue
            if same_title(title, s.get("name")) and len_ok(float(s.get("duration") or 0), duration):
                return s
    return None


def pin_youtube(songs):
    """Песням Spotify — проверенное видео YouTube Music, если оно нашлось.
    Не нашлось — downtify ищет сам, как раньше."""
    for s in songs:
        if s.get("youtube_id") or s.get("source") != "spotify":
            continue
        yt = youtube_match(s.get("artists") or [s.get("artist")], s.get("name"), s.get("duration"))
        if yt:
            s["youtube_id"] = yt["song_id"]


def youtube_song(rel, t, artist, yt):
    """Песня downtify для дорожки, которой нет в Spotify: звук — найденное
    видео, теги — Deezer."""
    song = dict(yt)
    song.update(deezer_tags(rel, t, artist))
    song["youtube_id"] = yt["song_id"]
    return song


def deezer_tags(rel, t, artist):
    info = jdb.loads(t["info"], {}) or {}
    who = info.get("artist") or artist
    tags = {"name": t["title"], "artists": [who], "artist": who, "album_name": rel["title"],
            "duration": t["duration"], "release_date": rel["release_date"] or "",
            "year": (rel["release_date"] or "")[:4]}
    if rel["cover"]:
        tags["cover_url"] = rel["cover"].replace("/250x250-", "/1000x1000-")
    return tags


# ---------------------------------------------------------------- Soulseek
def soulseek_on():
    """Ищет ли downtify звук в Soulseek раньше, чем на YouTube Music. Только
    тогда есть смысл отдавать ему дорожку без Spotify и без видео: иначе он
    возьмёт первое похожее с YouTube."""
    try:
        s = requests.get(env.DOWNTIFY_URL + "/api/settings", timeout=30).json()
    except (requests.exceptions.RequestException, ValueError):
        return False
    providers = s.get("audio_providers") or []
    return bool((s.get("slskd") or {}).get("enabled")) and providers[:1] == ["slskd"]


def deezer_song(rel, t, artist):
    """Песня downtify только с тегами Deezer: звук он ищет сам, в Soulseek."""
    song = deezer_tags(rel, t, artist)
    song.update({"song_id": "deezer-%s" % t["provider_id"], "source": "deezer"})
    return song


# ---------------------------------------------------------------- релиз
def note_track(con, t, sid, searched=True, alt=None, yt=None, slsk=None):
    """Запомнить у дорожки, под каким id Spotify она ушла в downtify.

    По этому id follow.py узнает её в фонотеке: название и имя исполнителя в
    Spotify бывают другими, чем в Deezer. spotify_id = None — искали, но в
    Spotify её нет; ключа нет вовсе — ушла с альбомом, id не сопоставился.
    spotify_alt — в Spotify есть только версия другой длины: по ней в пульте
    кнопка «Взять версию Spotify». youtube_id — в Spotify её нет, ушла видео
    YouTube Music: под этим id её знает очередь downtify. downtify_id — нет
    и на YouTube Music, ушла искаться в Soulseek под этим id.
    """
    info = jdb.loads(t["info"], {})
    if sid or searched:
        info["spotify_id"] = sid
    else:
        info.pop("spotify_id", None)
    if alt and not sid and not yt:
        info["spotify_alt"] = {"id": alt["id"], "length": round(alt.get("duration_ms", 0) / 1000.0)}
    else:
        info.pop("spotify_alt", None)
    if yt:
        info["youtube_id"] = yt["song_id"]
    else:
        info.pop("youtube_id", None)
    if slsk:
        info["downtify_id"] = slsk["song_id"]
    else:
        info.pop("downtify_id", None)
    # сбой прошлой попытки (follow.py) к новой отношения не имеет
    info.pop("fetch_error", None)
    con.execute("UPDATE release_tracks SET info=? WHERE id=?", (json.dumps(info, ensure_ascii=False), t["id"]))


def note_spotify_down(con, tracks):
    """Spotify не ответил: у дорожек вид сбоя «spotify» — по нему follow.py
    ночью повторит скачивание. Нашлась дорожка или нет, неизвестно, поэтому
    прежний ответ «в Spotify её нет» снимается."""
    for t in tracks:
        row = con.execute("SELECT info FROM release_tracks WHERE id=?", (t["id"],)).fetchone()
        info = jdb.loads(row["info"], {}) or {}
        if "spotify_id" in info and not info["spotify_id"]:
            info.pop("spotify_id")
        info["fetch_error"] = "spotify"
        con.execute("UPDATE release_tracks SET info=? WHERE id=?", (json.dumps(info, ensure_ascii=False), t["id"]))


def album_track_ids(tracks, songs):
    """Какой дорожке релиза какая песня альбома Spotify: по названию и длине,
    а не сошлось — по номеру, если длина та же (альбом найден по UPC, порядок
    дорожек тот же)."""
    out = {}
    for n, t in enumerate(tracks):
        for s in songs:
            if disco.title_keys(s.get("name")) & disco.title_keys(t["title"]) and \
                    len_ok(float(s.get("duration") or 0), t["duration"] or 0):
                out[t["id"]] = s.get("song_id")
                break
        else:
            s = songs[n] if len(songs) == len(tracks) else None
            if s is not None and len_ok(float(s.get("duration") or 0), t["duration"] or 0):
                out[t["id"]] = s.get("song_id")
    return out


def mmss(sec):
    return "%d:%02d" % (int(sec) // 60, int(sec) % 60)


COVERED = ("queued", "downloading", "in_library")


def orphans(con, rel, rows):
    """Повторы, которые не придут ни с каким другим релизом.

    Разбор помечает повтором дорожку, уже встреченную в другом релизе
    дискографии, и качаться она должна с тем релизом. Но если выбрали только
    сборник, а оригинальные альбомы — нет, его повторы не качались нигде: у
    Romantic Collection Скриптонита так пропало 16 треков из 23 (2026-10-25).
    Повтор качаем с этим релизом, если его двойник (тот же ISRC, а без ISRC —
    то же название и длина у того же исполнителя) не лежит в фонотеке и его
    релиз не качается и не скачан.
    """
    out = []
    for t in rows:
        decision = t["decision"] if "decision" in t.keys() else None
        if t["verdict"] != "dup" or decision:
            continue
        twins = con.execute(
            "SELECT x.title, x.duration, x.isrc, x.verdict, x.decision, r.status FROM release_tracks x "
            "JOIN releases r ON r.id = x.release_id WHERE r.artist_id=? AND x.release_id<>? AND "
            + ("x.isrc=?" if t["isrc"] else "(x.isrc IS NULL OR x.isrc='')"),
            (rel["artist_id"], rel["id"]) + ((t["isrc"],) if t["isrc"] else ())).fetchall()
        if not t["isrc"]:
            key = disco.norm_title(t["title"])
            twins = [x for x in twins if disco.norm_title(x["title"]) == key
                     and abs((x["duration"] or 0) - (t["duration"] or 0)) <= disco.LEN_SAME]
        covered = any(x["status"] in COVERED or x["verdict"] in ("have_isrc", "have_same")
                      or x["decision"] == "skip" for x in twins)
        if not covered:
            out.append(t)
    return out


def find_songs(con, rel, rows, want, artist, alt=False):
    """Песни downtify для дорожек want, то, что в Spotify не нашлось
    (названия и описания версий другой длины: «Making Promises» 2:24 вместо
    1:57), и что из ненайденного взято с YouTube Music или отдано искать в
    Soulseek. alt — версии другой длины брать, если на YouTube Music нет
    точной: человек так решил в пульте."""
    songs, missing, other, youtube, soulseek = [], [], [], [], []
    slsk_on = None
    rest = want
    # альбом Spotify по UPC релиза: дорожки придут с его названием и номерами.
    # Раньше так качался только релиз целиком, а часть — поиском по ISRC, и он
    # отдаёт первый попавшийся релиз с этой записью: «Slow Mo» со сборника
    # Скриптонита легла в альбом Armich «Смесь», «Локоны» — синглом (2026-10-25)
    if rel["provider"] == "deezer":
        alb = disco.deezer("album/%s" % rel["provider_id"]) or {}
        sid = album_by_upc(alb["upc"]) if alb.get("upc") else None
        if sid:
            album = downtify_songs("https://open.spotify.com/album/" + sid)
            ids = album_track_ids(rows, album)
            whole = len(want) == len(rows)
            take = {ids[t["id"]] for t in want if ids.get(t["id"])}
            songs = album if whole else [s for s in album if s.get("song_id") in take]
            for t in want:
                note_track(con, t, ids.get(t["id"]), searched=False)
            # весь релиз — альбомом целиком, как раньше; из части — по одной
            # только то, что в альбоме Spotify не сопоставилось
            rest = [] if whole else [t for t in want if not ids.get(t["id"])]
    if rest:
        for t in rest:
            alts = []
            sid = (track_by_isrc(t["isrc"], t["duration"], alts) if t["isrc"] else None) or \
                track_by_name(artist, t["title"], t["duration"], alts)
            yt = None
            if not sid:
                # в Spotify такой записи нет — запасной источник: YouTube Music
                info = jdb.loads(t["info"], {}) or {}
                yt = youtube_match([artist, info.get("artist")], t["title"], t["duration"])
            if not sid and not yt and alt and alts:
                sid = alts[0]["id"]
            slsk = None
            if not sid and not yt:
                if slsk_on is None:
                    slsk_on = soulseek_on()
                if slsk_on:
                    slsk = deezer_song(rel, t, artist)
            note_track(con, t, sid, alt=alts[0] if alts else None, yt=yt, slsk=slsk)
            if yt:
                songs.append(youtube_song(rel, t, artist, yt))
                youtube.append(t["title"])
                continue
            if slsk:
                songs.append(slsk)
                soulseek.append(t["title"])
                continue
            if not sid:
                missing.append(t["title"])
                if alts:
                    other.append("«%s» %s вместо %s" % (t["title"], mmss(alts[0].get("duration_ms", 0) / 1000.0),
                                                       mmss(t["duration"] or 0)))
                continue
            songs.extend(downtify_songs("https://open.spotify.com/track/" + sid))
    pin_youtube(songs)
    return songs, missing, other, youtube, soulseek


def release(con, release_id, artist_name=None, retry=False, alt=False):
    """Отправить в downtify то, что в релизе решено скачать. Вернуть отчёт.

    retry — повтор после «не скачалось»: качаем только то, что так и не
    доехало до фонотеки, иначе доехавшее легло бы в incoming второй копией.
    alt — брать и версии Spotify другой длины (кнопка в пульте).
    """
    rel = con.execute("SELECT r.*, a.name AS artist, a.deezer_name FROM releases r "
                      "LEFT JOIN artists a ON a.id = r.artist_id WHERE r.id=?", (release_id,)).fetchone()
    if rel is None:
        raise ValueError("нет такого релиза")
    rows = con.execute("SELECT * FROM release_tracks WHERE release_id=? ORDER BY id", (release_id,)).fetchall()
    want = [t for t in rows if disco.wanted(t)] + orphans(con, rel, rows)
    if want and retry:
        import follow
        want = follow.not_arrived(disco.Library(), rel, want)
        if not want:
            con.execute("UPDATE releases SET status='in_library', note=NULL WHERE id=?", (release_id,))
            return "всё из релиза уже в фонотеке"
    if not want:
        return "в релизе нечего качать"
    artist = artist_name or rel["deezer_name"] or rel["artist"] or ""
    try:
        songs, missing, other, youtube, soulseek = find_songs(con, rel, rows, want, artist, alt)
    except SpotifyUnavailable:
        note_spotify_down(con, want)
        raise
    if not songs:
        if other and len(other) == len(missing):
            raise RuntimeError("в Spotify только другая версия: %s" % ", ".join(other[:5]))
        raise RuntimeError("в Spotify не нашлось ни одной дорожки: %s%s" % (
            ", ".join(missing[:5]), "; другой длины: %s" % ", ".join(other[:5]) if other else ""))

    downtify_batch(songs)
    con.execute("UPDATE releases SET status='downloading', decided_at=?, note=NULL WHERE id=?",
                (jdb.now(), release_id))
    jdb.log_event(con, "download", "queue", None,
                  {"release": rel["title"], "artist": artist, "tracks": len(songs), "missing": missing,
                   "youtube": youtube, "soulseek": soulseek})
    msg = "в очередь downtify: %d дорожек «%s»" % (len(songs), rel["title"])
    if youtube:
        msg += "; нет в Spotify, взято с YouTube Music: %s" % ", ".join(youtube[:5])
    if soulseek:
        msg += "; нет ни в Spotify, ни на YouTube Music, ищется в Soulseek: %s" % ", ".join(soulseek[:5])
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
