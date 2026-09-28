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
import collections
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests  # noqa: E402

import env  # noqa: E402
from retry import translit, words  # noqa: E402

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


def artist_ok(ours, theirs):
    """Проверка исполнителя для поиска по названию.

    Всё имя нашего исполнителя должно найтись в ответе, с поправкой на
    раскладку (Кровосток — Krovostok). Одного общего слова мало: «Lil Peep»
    и «Lil Tecca» делят «lil», и так на трек приезжает чужая обложка. Лишние
    слова в ответе допустимы — там бывают приглашённые: «Xcho & Gor».
    """
    a = {translit(w) for w in words(clean_artist(ours))}
    b = {translit(w) for w in words(theirs)}
    return bool(a) and a <= b


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
    исполнитель совпал (см. artist_ok).
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


# ---------------------------------------------------------------- поиск по тегу альбома
# Для старых альбомов поиск по названию трека не срабатывает, а по паре
# «исполнитель + альбом» каталоги отвечают. Результат берём, только если
# исполнитель совпал и название альбома похоже.
STOP = {"the", "a", "и", "feat", "ft", "single", "album", "ep", "remix", "by", "version"}
TRIED = ["Spotify", "Deezer", "iTunes", "MusicBrainz"]


def _words(s):
    s = re.sub(r"[\(\[].*?[\)\]]", " ", str(s or "").lower())
    return {w for w in re.findall(r"[a-zа-яё0-9']+", s) if w not in STOP}


def album_ok(mine, theirs):
    a, b = _words(mine), _words(theirs)
    if not a or not b:
        return False
    if a <= b or b <= a:
        return True
    return len(a & b) / min(len(a), len(b)) >= 0.5


def clean_artist(a):
    # в запрос идёт только основной исполнитель: «Xcho (Feat. Gor)» -> «Xcho»
    a = re.sub(r"[\(\[]\s*(feat|ft)\.?[^\)\]]*[\)\]]", " ", str(a or ""), flags=re.I)
    return re.split(r"\s*(?:,|&|\+| feat\.? | ft\.? | x )\s*", a, flags=re.I)[0].strip()


def clean_album(al):
    return re.sub(r"[\(\[]\s*(single|ep|album)\s*[\)\]]", " ", str(al or ""), flags=re.I).strip()


def fetch_album(artist, album):
    """Обложка по тегу альбома: Deezer, затем iTunes. Вернуть (картинка, откуда)."""
    r = get("https://api.deezer.com/search/album",
            params={"q": "%s %s" % (clean_artist(artist), clean_album(album)), "limit": 8})
    try:
        data = (r.json() if r else {}).get("data") or []
    except Exception:
        data = []
    for x in data:
        who = ((x.get("artist") or {}).get("name")) or ""
        if artist_ok(artist, who) and album_ok(album, x.get("title")):
            for key in ("cover_xl", "cover_big"):
                img = get(x[key]) if x.get(key) else None
                if img and len(img.content) > MIN_BYTES:
                    return img.content, "Deezer (альбом)"
    time.sleep(PAUSE)
    r = get("https://itunes.apple.com/search",
            params={"term": "%s %s" % (clean_artist(artist), clean_album(album)), "entity": "album", "limit": 8})
    try:
        data = (r.json() if r else {}).get("results") or []
    except Exception:
        data = []
    for x in data:
        if artist_ok(artist, str(x.get("artistName") or "")) and album_ok(album, x.get("collectionName")):
            u = x.get("artworkUrl100")
            img = get(re.sub(r"\d+x\d+bb", "600x600bb", u)) if u else None
            if img and len(img.content) > MIN_BYTES:
                return img.content, "iTunes (альбом)"
    return None, None


# ---------------------------------------------------------------- общая база
_con = None


def db():
    global _con
    if _con is None:
        import janitordb
        _con = janitordb.connect()
    return _con


def group_key(artist, album):
    return "cover:%s|%s" % (" ".join(sorted(_words(artist))), " ".join(sorted(_words(album))))


def note_found(key, items, src):
    """Обложка поставлена: в журнал и снять вопрос, если он стоял в пульте."""
    if DRY:
        return
    import janitordb
    for it in items:
        janitordb.log_event(db(), "covers", "cover", {"cover": None}, {"cover": src},
                            item_id=it.id, path=it.path.decode("utf-8", "replace"))
    db().execute("UPDATE reviews SET status='resolved', decision='found', decided_at=? "
                 "WHERE key=? AND status='open'", (janitordb.now(), key))


def ask_cover(key, artist, album, items):
    """Не нашлось нигде — в пульт: там можно положить картинку руками."""
    if DRY:
        return
    import janitordb
    payload = {"artist": artist, "album": album, "count": len(items),
               "item_ids": [it.id for it in items], "tried": TRIED,
               "paths": [it.path.decode("utf-8", "replace") for it in items[:3]]}
    janitordb.ask(db(), "cover", key, album or (items[0].title if items else ""), payload)


def embed(mediafile, p, img):
    f = mediafile.MediaFile(p)
    f.images = [mediafile.Image(data=img, desc=None, type=mediafile.ImageType.front)]
    f.save()


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

    ok = 0
    missing = []
    for it, p, rid in todo:
        img, src = fetch(rid, str(it.artist), str(it.title))
        time.sleep(PAUSE)
        if not img:
            missing.append((it, p))
            continue
        print("   ++ %-9s %-24s %-30s %d КБ"
              % (src, str(it.artist)[:24], str(it.title)[:30], len(img) // 1024))
        if DRY:
            ok += 1
            continue
        try:
            embed(mediafile, p, img)
            note_found(group_key(it.artist, it.album), [it], src)
            ok += 1
        except Exception as e:
            missing.append((it, p))
            print("      !! записать не вышло: %s" % str(e)[:60])

    # второй заход: то, что не нашлось по треку, — группами по тегу альбома.
    # 42 трека одного бутлега — это один вопрос в пульте, а не 42
    groups = collections.defaultdict(list)
    for it, p in missing:
        groups[(str(it.artist), str(it.album).strip())].append((it, p))
    by_album = asked = 0
    for (artist, album), members in sorted(groups.items(), key=lambda x: -len(x[1])):
        key = group_key(artist, album)
        items = [it for it, _ in members]
        img, src = (fetch_album(artist, album) if album else (None, None))
        time.sleep(PAUSE)
        if img:
            print("   ++ %-16s %-24s %-30s (%d)" % (src, artist[:24], album[:30], len(members)))
            if not DRY:
                for it, p in members:
                    try:
                        embed(mediafile, p, img)
                    except Exception as e:
                        print("      !! %s: %s" % (os.path.basename(p)[:30], str(e)[:50]))
                note_found(key, items, src)
            by_album += len(members)
            continue
        print("   -- не нашлось: %-24s %-30s (%d)" % (artist[:24], (album or "без альбома")[:30], len(members)))
        ask_cover(key, artist, album, items)
        asked += 1

    left = len(missing) - by_album
    print("обложек по треку: %d, по тегу альбома: %d, не нашлось: %d (вопросов в пульт: %d)"
          % (ok, by_album, left, asked))
    alb_ok, alb_fail, alb_fail_items = albums(lib, mediafile)
    if not DRY:
        # для сводки пульта: сколько дорожек осталось без картинки
        import janitordb
        janitordb.meta_set(db(), "art_missing", left + alb_fail_items)
    stats_out = os.environ.get("JANITOR_STATS_OUT")
    if stats_out and not DRY:
        with open(stats_out, "w", encoding="utf-8") as f:
            json.dump({"singles_found": ok + by_album, "singles_missing": left,
                       "albums_found": alb_ok, "albums_missing": alb_fail}, f)


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


def has_art(item, mediafile):
    try:
        return bool(mediafile.MediaFile(item.path.decode("utf-8", "replace")).images)
    except Exception:
        return False


def albums(lib, mediafile):
    """Альбомы, которых не осилил fetchart.

    Штатный плагин ищет обложку по названию и на малоизвестных релизах
    возвращает пустоту (а иногда и чужую картинку). Здесь идём тем же путём,
    что и для синглтонов: по идентификатору дорожки, то есть наверняка.
    """
    # По названию здесь ищем только альбом (fetch_album, с проверкой и
    # исполнителя, и названия). Поиск песни по названию альбома, который тут
    # стоял раньше, проверял одного исполнителя и приносил обложку любой его
    # песни — а при слабой проверке и чужой.
    todo = []
    embedded = 0
    for a in lib.albums():
        ap = a.artpath.decode("utf-8", "replace") if a.artpath else ""
        if ap and os.path.isfile(ap):
            continue
        its = list(a.items())
        if not its:
            continue
        # Картинка уже встроена во все дорожки — делать нечего. Файл обложки
        # в общую папку мы нарочно не кладём (см. ниже), и без этой проверки
        # такие альбомы каждую ночь «находились» бы заново: 35 одних и тех же
        # за ночь, с записью в журнал на каждую дорожку.
        if all(has_art(i, mediafile) for i in its):
            embedded += 1
            continue
        tid = next((str(i.mb_trackid) for i in its if str(i.mb_trackid or "").strip()), "")
        todo.append((a, its, tid))

    print()
    print("альбомов без обложки: %d (ещё %d без файла обложки, но с картинкой в дорожках — "
          "их не трогаю)" % (len(todo), embedded))
    if LIMIT:
        todo = todo[:LIMIT]

    ok = fail = fail_items = 0
    for a, its, tid in todo:
        img, src = fetch(tid)
        if not img:
            time.sleep(PAUSE)
            img, src = fetch_album(str(a.albumartist), str(a.album))
        time.sleep(PAUSE)
        key = group_key(a.albumartist, a.album)
        if not img:
            fail += 1
            fail_items += len(its)
            print("   -- не нашлось: %-24s %s" % (str(a.albumartist)[:24], str(a.album)[:30]))
            ask_cover(key, str(a.albumartist), str(a.album), its)
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
            note_found(key, its, src)
            ok += 1
        except Exception as e:
            fail += 1
            fail_items += len(its)
            print("      !! записать не вышло: %s" % str(e)[:60])

    print("обложек альбомам проставлено: %d, не нашлось: %d" % (ok, fail))
    return ok, fail, fail_items


if __name__ == "__main__":
    main()
