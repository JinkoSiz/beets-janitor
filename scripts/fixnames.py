#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Переименование файлов, у которых имя осталось абракадаброй.

fixenc.py чинит ТЕГИ, но имя файла beets задал при импорте по старому,
битому тегу. Клиенты вроде Substreamer это не показывают, но путь остаётся
нечитаемым, а смена пути заодно выбивает кэш клиентов по этим трекам.

Запускать ПОСЛЕ fixenc.py и beet update -M, иначе beets переименует по
старым значениям из базы.
"""
import os
import subprocess
import sys

SEP = "\x01"
DRY = "--dry" in sys.argv


def moji(s):
    """См. пояснение в fixenc.py: считаем не долю кириллицы в строке, а то,
    что все символы À-ÿ превращаются в кириллицу."""
    if not s:
        return False
    n = sum(1 for c in s if "À" <= c <= "ÿ")
    if n < 2:
        return False
    try:
        cand = s.encode("latin-1").decode("cp1251")
    except Exception:
        return False
    m = sum(1 for c in cand if "Ѐ" <= c <= "ӿ")
    return m >= n * 0.8


def bad_items():
    out = subprocess.run(["beet", "ls", "-f", "$id" + SEP + "$path"],
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        p = line.split(SEP)
        if len(p) != 2:
            continue
        if moji(os.path.basename(p[1])) or moji(os.path.dirname(p[1])):
            yield p


def main():
    items = list(bad_items())
    print("файлов с абракадаброй в пути: %d%s" % (len(items), " (вхолостую)" if DRY else ""))
    if DRY:
        for iid, path in items[:10]:
            print("   ", path)
        return
    for iid, _ in items:
        subprocess.run(["beet", "move", "id:" + iid], capture_output=True, text=True)
    print("осталось: %d" % len(list(bad_items())))


if __name__ == "__main__":
    main()
