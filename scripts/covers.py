#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Обложки для одиночных треков.

Плагин fetchart обслуживает только альбомы: у синглтона обложка не появится
никогда, сколько ни сканируй. Отсюда сотни треков с пустым квадратом в плеере.

Берём картинку не поиском по названию (так подставляется чужое: на запрос
"Mary Jane" однажды пришла обложка Mary J. Blige), а строго по идентификатору
релиза, который у трека уже проставлен источником. Формат идентификатора сам
говорит, куда идти:

  UUID с дефисами  -> MusicBrainz -> Cover Art Archive
  22 знака base62  -> Spotify     -> публичный oEmbed, ключа не требует
  только цифры     -> Deezer      -> открытый API

Картинку встраиваем в сам файл. Файл обложки рядом не кладём: синглтоны лежат
в общей папке исполнителя, и cover.jpg там относился бы ко всем сразу.
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests  # noqa: E402

import env  # noqa: E402
from retry import artist_ok  # noqa: E402

DRY = "--dry" in sys.argv
LIMIT = int(os.environ.get("COVERS_LIMIT", "0"))
PAUSE = 0.4
MIN_BYTES = 5000

UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
SPOTIFY = re.compile(r"^[0-9A-Za-z]{22}$")
DEEZER = re.compile(r"^\d+$")


def get(url, **kw):
    try:
        r = requests.get(url, timeout=30, **kw)
        return r if r.status_code == 200 else None
    except Exception:
        return None


def art_musicbrainz(tid):
    """У дорожки свой идентификатор, а обложка привязана к релизу — сначала
    спрашиваем, на каких релизах эта запись выходила."""
    r = get("https://musicbrainz.org/ws/2/recording/%s" % tid,
            params={"inc": "releases", "fmt": "json"})
    if not r:
        return None
    try:
        rels = r.json().get("releases") or []
    except Exception:
        return None
    for rel in rels[:3]:
        for size in ("front-500", "front"):
            img = get("https://coverartarchive.org/release/%s/%s" % (rel.get("id"), size))
            if img and len(img.content) > MIN_BYTES:
                return img.content
    return None


def art_spotify(tid):
    # oEmbed принимает ссылку на дорожку и отдаёт обложку её альбома
    r = get("https://open.spotify.com/oembed",
            params={"url": "https://open.spotify.com/track/" + tid})
    if not r:
        return None
    try:
        thumb = r.json().get("thumbnail_url")
    except Exception:
        return None
    if not thumb:
        return None
    # oEmbed отдаёт 300px, крупная версия — та же ссылка с другим префиксом
    for u in (thumb.replace("ab67616d00001e02", "ab67616d0000b273"), thumb):
        img = get(u)
        if img and len(img.content) > MIN_BYTES:
            return img.content
    return None


def art_deezer(tid):
    r = get("https://api.deezer.com/track/" + tid)
    if not r:
        return None
    try:
        alb = (r.json() or {}).get("album") or {}
    except Exception:
        return None
    for key in ("cover_xl", "cover_big", "cover_medium"):
        u = alb.get(key)
        if not u:
            continue
        img = get(u)
        if img and len(img.content) > MIN_BYTES:
            return img.content
    return None


def art_itunes(artist, title):
    """Поиск по названию — последняя очередь.

    Ключа не требует, покрытие широкое, но именно такой поиск однажды принёс
    на «Mary Jane» обложку Mary J. Blige. Поэтому берём картинку, только если
    у найденного исполнителя есть общее слово с нашим.
    """
    r = get("https://itunes.apple.com/search",
            params={"term": "%s %s" % (artist, title), "entity": "song", "limit": 5})
    if not r:
        return None
    try:
        results = (r.json() or {}).get("results") or []
    except Exception:
        return None
    for res in results:
        if not artist_ok(artist, res.get("artistName", "")):
            continue
        u = res.get("artworkUrl100") or res.get("artworkUrl60")
        if not u:
            continue
        for size in ("600x600bb", "100x100bb"):
            img = get(re.sub(r"\d+x\d+bb", size, u))
            if img and len(img.content) > MIN_BYTES:
                return img.content
    return None


def art_deezer_search(artist, title):
    r = get("https://api.deezer.com/search",
            params={"q": 'artist:"%s" track:"%s"' % (artist, title), "limit": 5})
    if not r:
        return None
    try:
        data = (r.json() or {}).get("data") or []
    except Exception:
        return None
    for res in data:
        who = ((res.get("artist") or {}).get("name")) or ""
        if not artist_ok(artist, who):
            continue
        alb = res.get("album") or {}
        for key in ("cover_xl", "cover_big"):
            u = alb.get(key)
            if not u:
                continue
            img = get(u)
            if img and len(img.content) > MIN_BYTES:
                return img.content
    return None


def fetch(tid, artist=None, title=None):
    """Вернуть (картинка, откуда).

    Порядок жёсткий: сначала точные способы по идентификатору релиза, и лишь
    когда там пусто — поиск по названию. Надёжные источники имеют приоритет,
    ненадёжный подключается последним и только с проверкой исполнителя.
    """
    tid = str(tid or "").strip()
    if UUID.match(tid):
        img = art_musicbrainz(tid)
        if img:
            return img, "MusicBrainz"
    elif DEEZER.match(tid):
        img = art_deezer(tid)
        if img:
            return img, "Deezer"
    elif SPOTIFY.match(tid):
        img = art_spotify(tid)
        if img:
            return img, "Spotify"

    if artist and title:
        img = art_itunes(artist, title)
        if img:
            return img, "iTunes*"
        img = art_deezer_search(artist, title)
        if img:
            return img, "Deezer*"
    return None, None


def main():
    import mediafile
    import retry
    lib = retry.open_library()

    todo = []
    for it in lib.items(""):
        if not it.singleton:
            continue
        p = it.path.decode("utf-8", "replace")
        if not os.path.isfile(p):
            continue
        try:
            if mediafile.MediaFile(p).images:
                continue
        except Exception:
            continue
        # берём и те, у кого идентификатора нет: для них сработает поиск
        # по названию, который подключается последней очередью
        todo.append((it, p, str(it.get("mb_trackid") or "").strip()))

    print("синглтонов без обложки: %d" % len(todo))
    if LIMIT:
        todo = todo[:LIMIT]
        print("ограничение прогона: %d" % LIMIT)

    ok = fail = 0
    for it, p, rid in todo:
        img, src = fetch(rid, str(it.artist), str(it.title))
        time.sleep(PAUSE)
        if not img:
            fail += 1
            print("   -- не нашлось: %-24s %s" % (str(it.artist)[:24], str(it.title)[:30]))
            continue
        print("   ++ %-9s %-24s %-30s %d КБ"
              % (src, str(it.artist)[:24], str(it.title)[:30], len(img) // 1024))
        if DRY:
            ok += 1
            continue
        try:
            f = mediafile.MediaFile(p)
            f.images = [mediafile.Image(data=img, desc=None,
                                        type=mediafile.ImageType.front)]
            f.save()
            ok += 1
        except Exception as e:
            fail += 1
            print("      !! записать не вышло: %s" % str(e)[:60])

    print("обложек проставлено: %d, не нашлось: %d" % (ok, fail))
    albums(lib, mediafile)


AUDIO_EXT = (".mp3", ".flac", ".opus", ".m4a", ".ogg", ".wav", ".aac", ".wma")


def folder_is_single_album(d, mediafile):
    """Все ли звуковые файлы папки принадлежат одному релизу.

    Смотрим теги самих файлов, а не базу beets: Navidrome читает именно их,
    и расхождение как раз и создаёт проблему — beets может считать полсотни
    синглов одним альбомом, а плеер видит сорок разных.
    """
    names = set()
    try:
        for f in os.listdir(d):
            if os.path.splitext(f)[1].lower() not in AUDIO_EXT:
                continue
            try:
                names.add(str(mediafile.MediaFile(os.path.join(d, f)).album or "").strip())
            except Exception:
                pass
            if len(names) > 1:
                return False
    except Exception:
        return False
    return len(names) <= 1


def albums(lib, mediafile):
    """Альбомы, которых не осилил fetchart.

    Штатный плагин ищет обложку по названию и на малоизвестных релизах
    возвращает пустоту (а иногда и чужую картинку). Здесь идём тем же путём,
    что и для синглтонов: по идентификатору дорожки, то есть наверняка.
    """
    todo = []
    for a in lib.albums():
        ap = a.artpath.decode("utf-8", "replace") if a.artpath else ""
        if ap and os.path.isfile(ap):
            continue
        its = list(a.items())
        if not its:
            continue
        tid = next((str(i.mb_trackid) for i in its if str(i.mb_trackid or "").strip()), "")
        todo.append((a, its, tid))

    print()
    print("альбомов без обложки: %d" % len(todo))
    if LIMIT:
        todo = todo[:LIMIT]

    ok = fail = 0
    for a, its, tid in todo:
        img, src = fetch(tid, str(a.albumartist), str(a.album))
        time.sleep(PAUSE)
        if not img:
            fail += 1
            print("   -- не нашлось: %-24s %s" % (str(a.albumartist)[:24], str(a.album)[:30]))
            continue
        d = os.path.dirname(its[0].path.decode("utf-8", "replace"))
        print("   ++ %-9s %-24s %-30s %d КБ"
              % (src, str(a.albumartist)[:24], str(a.album)[:30], len(img) // 1024))
        if DRY:
            ok += 1
            continue
        try:
            # Файл обложки в папке Navidrome ставит ВЫШЕ встроенной картинки
            # (его порядок — cover.*, folder.*, front.*, embedded). Если в
            # папке лежат разные релизы, один cover.jpg подменит собой все их
            # обложки разом — так 50 синглов АДЛИН получили одну картинку.
            # Поэтому кладём файл, только когда папка принадлежит одному
            # альбому; иначе ограничиваемся встраиванием в сами дорожки.
            if folder_is_single_album(d, mediafile):
                cover = os.path.join(d, "cover.jpg")
                open(cover, "wb").write(img)
                a.artpath = cover.encode("utf-8")
                a.store()
            else:
                print("      (папка общая — файл обложки не кладу)")
            for i in its:
                p = i.path.decode("utf-8", "replace")
                try:
                    f = mediafile.MediaFile(p)
                    if not f.images:
                        f.images = [mediafile.Image(data=img, desc=None,
                                                    type=mediafile.ImageType.front)]
                        f.save()
                except Exception:
                    pass
            ok += 1
        except Exception as e:
            fail += 1
            print("      !! записать не вышло: %s" % str(e)[:60])

    print("обложек альбомам проставлено: %d, не нашлось: %d" % (ok, fail))


if __name__ == "__main__":
    main()
