#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Нормализация имён исполнителей.

Три класса проблем, все правятся И на уровне треков, И на уровне альбомов —
если поправить только треки, альбомная запись сохранит старое значение и
Navidrome продолжит показывать двойника.

  1. склейки:   "1nonly, Freddie Dredd" -> "1nonly"
  2. регистр:   "Bones" -> "BONES"       (побеждает самое частое написание)
  3. гомоглифы: "4K" (лат.) -> "4К" (кир.)  — выглядят одинаково, строки разные
"""
import collections
import re
import subprocess
import sys

DRY = "--dry" in sys.argv
CYR = "АВЕКМНОРСТУХаеорсух"
LAT = "ABEKMHOPCTYXaeopcyx"
TBL = str.maketrans(CYR, LAT)


def beet(*args):
    return subprocess.run(["beet", *args], capture_output=True, text=True).stdout


def apply(field, old, new, album_level):
    q = "%s::^%s$" % (field, re.escape(old))
    print("   %s %s: %r -> %r" % ("альбом" if album_level else "трек ", field, old, new))
    if DRY:
        return
    cmd = ["beet", "modify", "-y"]
    if album_level:
        cmd.append("-a")
    cmd += [q, "%s=%s" % (field, new)]
    subprocess.run(cmd, capture_output=True, text=True)


def collab_fix(album_level):
    args = ["ls", "-a", "-f", "$albumartist"] if album_level else ["ls", "-f", "$albumartist"]
    vals = collections.Counter(v for v in beet(*args).splitlines() if "," in v)
    for val in vals:
        apply("albumartist", val, val.split(",")[0].strip(), album_level)
    return len(vals)


def merge_fix(field, album_level):
    """Регистр и гомоглифы: группируем по нормализованному ключу,
    побеждает самое частое написание, при ничьей — то, что стоит на треках с MBID."""
    fmt = "$%s\t$mb_trackid" % field
    args = ["ls", "-a", "-f", fmt] if album_level else ["ls", "-f", fmt]
    counts, with_mbid = collections.Counter(), collections.Counter()
    for line in beet(*args).splitlines():
        p = line.split("\t")
        val = p[0]
        if not val.strip():
            continue
        counts[val] += 1
        if len(p) > 1 and p[1] and not p[1].startswith("$"):
            with_mbid[val] += 1

    groups = collections.defaultdict(list)
    for val in counts:
        groups[val.translate(TBL).lower()].append(val)

    fixed = 0
    for variants in groups.values():
        if len(variants) < 2:
            continue
        variants.sort(key=lambda v: (-counts[v], -with_mbid[v], v))
        top = variants[0]
        if counts[variants[0]] == counts[variants[1]]:
            best = max(variants, key=lambda v: with_mbid[v])
            if with_mbid[best] > 0:
                top = best
        for old in variants:
            if old != top:
                apply(field, old, top, album_level)
                fixed += 1
    return fixed



def va_fix():
    """Фрагмент сборника: альбом помечен Various Artists, а на деле все треки
    одного исполнителя — значит от сборника у нас один-два трека, и они должны
    лежать под своим артистом, а не в общей куче."""
    SEP = ""
    rows = beet("ls", "-f", SEP.join(["$id", "$album", "$artist"]),
                "albumartist:Various Artists").splitlines()
    by_album = collections.defaultdict(list)
    for line in rows:
        p = line.split(SEP)
        if len(p) == 3:
            by_album[p[1]].append(p)

    def primary(a):
        for sep in (" feat. ", " Feat. ", " ft. ", ","):
            if sep in a:
                a = a.split(sep)[0]
        return a.strip()

    fixed = 0
    for album, items in by_album.items():
        # сравниваем не сырые строки, а ведущего исполнителя: альбом рвётся
        # надвое, когда сольные треки идут под своим именем, а совместные
        # уезжают в Various Artists
        prim = {primary(i[2]) for i in items if primary(i[2])}
        if len(prim) != 1:
            continue
        new = prim.pop()
        if not new:
            continue
        print("   сборник-фрагмент %r (%d трек) -> albumartist=%r" % (album[:30], len(items), new))
        if not DRY:
            for i in items:
                subprocess.run(["beet", "modify", "-y", "id:" + i[0],
                                "albumartist=" + new, "comp=0"], capture_output=True, text=True)
        fixed += 1

    # то же самое на уровне альбомных записей: там значение живёт отдельно
    for line in beet("ls", "-a", "-f", SEP.join(["$id", "$album", "$albumartist"])).splitlines():
        q = line.split(SEP)
        if len(q) != 3 or q[2] != "Various Artists":
            continue
        arts = {x.split(SEP)[0] for x in
                beet("ls", "-f", "$artist" + SEP + "$id", "album_id:" + q[0]).splitlines()
                if SEP in x}
        prim = {primary(a) for a in arts if primary(a)}
        if len(prim) != 1:
            continue
        new = prim.pop()
        if not new:
            continue
        print("   сборник-фрагмент (альбом) %r -> albumartist=%r" % (q[1][:30], new))
        if not DRY:
            subprocess.run(["beet", "modify", "-a", "-y", "id:" + q[0],
                            "albumartist=" + new, "comp=0"], capture_output=True, text=True)
        fixed += 1
    return fixed


def empty_albumartist_fix():
    """Альбом без albumartist разваливается в плеере.

    Navidrome при пустом поле берёт исполнителя дорожки, и релиз, где на
    каждой вещи свой гость, превращается в десяток альбомов по одной
    дорожке — так рассыпался Communio Lupatum у Powerwolf.

    Решение: если у дорожек есть один преобладающий ведущий исполнитель, он
    и становится albumartist. Если ведущих много и большинства нет, это
    настоящий сборник, ставим Various Artists.
    """
    SEP = "\x01"
    rows = beet("ls", "-f", SEP.join(["$id", "$album", "$artist"]),
                "albumartist::^$")
    by_album = collections.defaultdict(list)
    for line in rows.splitlines():
        q = line.split(SEP)
        if len(q) == 3 and q[1].strip():
            by_album[q[1]].append(q)

    def primary(a):
        for sep in (" feat. ", " Feat. ", " ft. ", ","):
            if sep in a:
                a = a.split(sep)[0]
        return a.strip()

    fixed = 0
    for album, items in by_album.items():
        prim = collections.Counter(primary(i[2]) for i in items if primary(i[2]))
        if not prim:
            continue
        top, n = prim.most_common(1)[0]
        if n * 2 > len(items):
            new, comp = top, 0
        else:
            new, comp = "Various Artists", 1
        print("   без albumartist %r (%d дорожек, ведущих %d) -> %r"
              % (album[:30], len(items), len(prim), new))
        if not DRY:
            for i in items:
                subprocess.run(["beet", "modify", "-y", "id:" + i[0],
                                "albumartist=" + new, "comp=%d" % comp],
                               capture_output=True, text=True)
        fixed += 1
    return fixed


if __name__ == "__main__":
    total = 0
    total += va_fix()
    total += empty_albumartist_fix()
    for lvl in (False, True):
        total += collab_fix(lvl)
    for lvl in (False, True):
        total += merge_fix("albumartist", lvl)
    total += merge_fix("artist", False)
    print("исправлено значений:", total)
