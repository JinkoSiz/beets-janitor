#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Доводка свежих импортов: то, что downtify пишет не так, как нужно плееру.

1. «Various Artists» вместо исполнителя. downtify ставит исполнителем
   альбома Various Artists каждому треку, где исполнителей больше одного
   («RAM, BOOKER»), а сольным трекам того же альбома — «RAM». Navidrome
   собирает альбом по паре «исполнитель альбома + название» и показывает два
   альбома вместо одного; сингл с гостем целиком уезжает в Various Artists.
   Хозяин находится так: если у этого названия альбома в фонотеке ровно один
   настоящий исполнитель альбома и трек его — он; иначе, если у всех свежих
   дорожек альбома один главный исполнитель (или дорожка одна) — он.
   Настоящие сборники — разные исполнители, главного нет — не трогаются.

2. Нет исходной даты. Navidrome внутри года сортирует альбомы по исходной
   дате (original_date), а у скачанного она пустая: свежий релиз в «Recently
   released» оказывается ниже всех альбомов того же года. Проставляем
   исходную дату равной дате релиза, если исходной нет.

Трогает только дорожки, добавленные с прошлого запуска (отметка в общей
базе); каждое изменение пишется в журнал и откатывается из пульта.

  postimport.py [--dry] [--days N]    --days: взять добавленное за N дней
"""
import collections
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import discography as disco  # noqa: E402
import janitordb as jdb  # noqa: E402
from retry import artist_ok  # noqa: E402

DRY = "--dry" in sys.argv
VA = {"various artists", "various", "va", "сборник", "разные исполнители"}
FIRST_RUN_DAYS = 2


def is_va(s):
    return str(s or "").strip().lower() in VA


def main():
    import retry
    lib = retry.open_library()
    con = jdb.connect() if not DRY else None
    items = list(lib.items(""))
    if "--days" in sys.argv:
        since = time.time() - float(sys.argv[sys.argv.index("--days") + 1]) * 86400
    else:
        mark = float(jdb.meta_get(con, "postimport_seen_until", 0) or 0) if con is not None else 0
        since = mark or time.time() - FIRST_RUN_DAYS * 86400
    fresh = [i for i in items if float(i.added or 0) > since]

    def log(it, op, before, after):
        if con is not None:
            jdb.log_event(con, "postimport", op, before, after, item_id=it.id,
                          path=it.path.decode("utf-8", "replace"))

    # настоящие исполнители альбомов по названию — по всей фонотеке
    owners = collections.defaultdict(set)
    for i in items:
        al, aa = str(i.album or "").strip().lower(), str(i.albumartist or "").strip()
        if al and aa and not is_va(aa):
            owners[al].add(aa)

    groups = collections.defaultdict(list)
    for i in fresh:
        if is_va(i.albumartist) and not i.comp:
            groups[str(i.album or "").strip().lower()].append(i)

    fixed_aa = kept = 0
    for al, its in groups.items():
        own = owners.get(al, set()) if al else set()
        primaries = {disco.primary_artist(str(x.artist)).lower() for x in its}
        for i in its:
            new = None
            if len(own) == 1 and artist_ok(next(iter(own)), str(i.artist)):
                new = next(iter(own))
            elif len(primaries) == 1:
                new = disco.primary_artist(str(i.artist))
            if not new:
                kept += 1
                continue
            print("   %s — %s: исполнитель альбома Various Artists -> %s" % (str(i.artist)[:30], str(i.title)[:30], new))
            if DRY:
                fixed_aa += 1
                continue
            before = {"albumartist": str(i.albumartist), "comp": i.comp}
            i.albumartist = new
            i.comp = False
            i.store()
            i.try_write()
            log(i, "albumartist", before, {"albumartist": new, "comp": False, "why": "downtify: Various Artists"})
            fixed_aa += 1

    fixed_date = 0
    for i in fresh:
        if int(i.original_year or 0) or not int(i.year or 0):
            continue
        fixed_date += 1
        if DRY:
            continue
        before = {"original_year": i.original_year, "original_month": i.original_month, "original_day": i.original_day}
        i.original_year, i.original_month, i.original_day = i.year, i.month, i.day
        i.store()
        i.try_write()
        log(i, "original_date", before, {"original_year": i.year, "original_month": i.month,
                                         "original_day": i.day, "why": "дата релиза"})

    if con is not None:
        jdb.meta_set(con, "postimport_seen_until", max((float(i.added or 0) for i in items), default=time.time()))
    print("свежих дорожек: %d | исполнитель альбома поправлен: %d (сборников оставлено: %d) | исходная дата: %d"
          % (len(fresh), fixed_aa, kept, fixed_date))
    stats_out = os.environ.get("JANITOR_STATS_OUT")
    if stats_out and not DRY:
        with open(stats_out, "w", encoding="utf-8") as f:
            json.dump({"fresh": len(fresh), "albumartist": fixed_aa, "original_date": fixed_date}, f)


if __name__ == "__main__":
    main()
