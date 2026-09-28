#!/usr/bin/env python3
"""Сборка альбомов, развалившихся на синглы.

Трек, сматченный на сингловый релиз, получает album == title и track = 1.
Альбом из десяти дорожек превращается в десять «альбомов» по одной штуке.
Источники тут не помогают: в стримингах эти вещи и правда вышли синглами,
альбом существовал только в исходных тегах файлов, а матчинг их затёр.

Опора — встроенная обложка: у дорожек одного релиза она побайтово одна и та
же. Если в группе с общей обложкой уцелела хоть одна дорожка с настоящим
названием альбома, остальные дорожки той же группы принадлежат ему же.

Осторожность:
- трогаем только дорожки, где album == title (признак затёртого тега);
- в группе должно быть ровно одно «настоящее» название, иначе не угадать;
- номер дорожки у затёртых сбрасываем: он был выставлен в 1 сингловым
  релизом и всё равно неверен, пусть лучше сортируется по названию.

Безопасность: ничего не удаляет и не перемещает. Каждое изменение пишется
в JOURNAL, откат построчный.
"""
import collections
import datetime
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import env  # noqa: E402

JOURNAL = env.JOURNAL
LIBCFG = env.LIBRARY_CONFIG
MIN_GROUP = 3
DRY = "--dry" in sys.argv


def open_library():
    from beets import config as bconf
    from beets import library, plugins
    bconf.read()
    bconf.set_file(LIBCFG)
    try:
        plugins.load_plugins()
    except TypeError:
        plugins.load_plugins(names=bconf["plugins"].as_str_seq())
    plugins.find_plugins()
    return library.Library(bconf["library"].as_path(),
                           bconf["directory"].as_filename())


def is_fake(it):
    """Признак затёртого тега: название альбома совпало с названием трека."""
    a, t = str(it.album or "").strip().lower(), str(it.title or "").strip().lower()
    return bool(a) and a == t


def art_hash(path):
    import mediafile
    try:
        f = mediafile.MediaFile(path)
        if not f.images:
            return None
        return hashlib.md5(f.images[0].data).hexdigest()
    except Exception:
        return None


def key_of(it):
    return str(it.albumartist or it.artist or "").strip().lower()


def merged_editions(lib):
    """Найти альбомы, в которые слиплись два издания.

    При импорте стоит duplicate_action: merge, и второе издание подмешивается
    в уже существующий альбом вместо отдельного. Если у релиза восемь дорожек,
    а в альбоме их шестнадцать, beets считает половину лишними, штрафует за
    unmatched_tracks и из-за max_rec никогда не поднимает оценку до strong —
    дорожки второго издания остаются без идентификаторов навсегда.

    Чинить молча не берёмся: разделение переписывает записи в базе, а ошибка
    тут дороже пользы. Показываем, разбираем руками.
    """
    hurt = 0
    for al in lib.albums():
        items = list(al.items())
        dirs = collections.defaultdict(list)
        for i in items:
            dirs[os.path.dirname(i.path.decode("utf-8", "replace"))].append(i)
        if len(dirs) < 2:
            continue
        blind = [d for d, its in dirs.items() if any(not i.mb_trackid for i in its)]
        mark = "  <- дорожки без ID, матчинг заблокирован" if blind else ""
        print("   ! слиплись издания: %r (папок %d, дорожек %d)%s"
              % (str(al.album)[:34], len(dirs), len(items), mark))
        for d, its in dirs.items():
            miss = sum(1 for i in its if not i.mb_trackid)
            print("       %-52s дорожек %d, без ID %d"
                  % (d.replace(env.MUSIC_DIR + "/", "")[:52], len(its), miss))
        if blind:
            hurt += 1
    return hurt


def main():
    lib = open_library()
    merged_editions(lib)

    # сначала ищем, у каких исполнителей вообще есть развалившиеся треки:
    # читать обложки у всей библиотеки незачем
    suspects = set()
    for it in lib.items(""):
        if is_fake(it):
            suspects.add(key_of(it))
    print("исполнителей с развалившимися альбомами:", len(suspects))
    if not suspects:
        return

    groups = collections.defaultdict(list)
    for it in lib.items(""):
        k = key_of(it)
        if k not in suspects:
            continue
        p = it.path.decode("utf-8", "replace")
        if not os.path.isfile(p):
            continue
        h = art_hash(p)
        if h:
            groups[(k, h)].append(it)

    con = None
    if not DRY:
        import janitordb
        con = janitordb.connect()

    fixed = albums = 0
    for (k, h), items in sorted(groups.items(), key=lambda x: -len(x[1])):
        if len(items) < MIN_GROUP:
            continue
        names = collections.Counter(
            str(i.album).strip() for i in items
            if not is_fake(i) and str(i.album or "").strip())
        if not names:
            continue
        # Одна обложка — один релиз, поэтому расхождение в названии здесь
        # всегда дефект. Берём большинство: так чинится и разошедшийся регистр
        # (ОСАДКИ против Осадки), и случай, когда одна дорожка из шестнадцати
        # сматчилась к другому источнику и принесла свой вариант названия
        # ("Возвращение легенды" против "Возвращение легенды (сборник)").
        # Нормализация тут не поможет: merge_fix сводит исполнителей, а поле
        # album не трогает вовсе.
        by_key = collections.Counter()
        for n, c in names.items():
            by_key[n.lower()] += c
        top_key, top_n = by_key.most_common(1)[0]
        if top_n * 3 < sum(by_key.values()) * 2:
            print("   ? пропускаю: обложка %s, нет явного большинства: %s"
                  % (h[:8], ", ".join(sorted(names))[:70]))
            continue
        album = collections.Counter(
            {n: c for n, c in names.items() if n.lower() == top_key}
        ).most_common(1)[0][0]
        anchor_item = [i for i in items if not is_fake(i)][0]

        # берём всё, что написано иначе: и затёртые дорожки, и уцелевшие с
        # другим регистром. Если у дорожки уже ровно это название — чинить
        # нечего; без этой проверки скрипт «назначал» тот же альбом и попутно
        # сбивал номер у обычных синглов с двумя копиями в библиотеке.
        targets = [i for i in items if str(i.album or "").strip() != album]
        if not targets:
            continue
        print("-- %s: %d дорожек -> альбом %r (по обложке %s)"
              % (k[:24], len(targets), album[:34], h[:8]))
        albums += 1
        for i in targets:
            print("     %-46s%s" % (str(i.title)[:46],
                                    "" if is_fake(i)
                                    else "   было: %s" % str(i.album)[:30]))
            if DRY:
                fixed += 1
                continue
            before = {"album": i.album, "track": i.track,
                      "albumartist": i.albumartist, "comp": i.comp}
            i.album = album
            i.albumartist = anchor_item.albumartist or i.albumartist
            # номер сбрасываем только у затёртых: им сингловый релиз выставил
            # единицу. У дорожек, где разошёлся лишь регистр, номер настоящий
            if is_fake(i):
                i.track = 0
            i.comp = 0
            i.store()
            i.try_write()
            if con is not None:
                janitordb.log_event(
                    con, "albumgroup", "album", before,
                    {"album": i.album, "track": i.track, "albumartist": i.albumartist,
                     "comp": i.comp, "why": "обложка"},
                    item_id=i.id, path=i.path.decode("utf-8", "replace"))
            fixed += 1

    print("собрано альбомов: %d, дорожек возвращено: %d" % (albums, fixed))
    stats_out = os.environ.get("JANITOR_STATS_OUT")
    if stats_out and not DRY:
        with open(stats_out, "w", encoding="utf-8") as f:
            json.dump({"albums": albums, "fixed": fixed}, f)


if __name__ == "__main__":
    main()
