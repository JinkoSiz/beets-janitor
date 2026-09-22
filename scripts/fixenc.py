#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Починка тегов, записанных в CP1251 и прочитанных как латиница.

Старые русские рипы часто содержат ID3 в кодировке Windows-1251. Плеер и beets
читают их как latin-1, получается "Ìîÿ øëþõà" вместо "Моя шлюха". Поиск по
такой строке не найдёт ничего ни в одном каталоге, и трек ложится as-is.

Запускать НАДО ДО импорта: тогда beets прочитает нормальные теги и сматчит.

  python3 fixenc.py /incoming [--dry]
"""
import os
import sys

import mutagen

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import env  # noqa: E402

FIELDS = ("artist", "title", "album", "albumartist", "composer")
EXT = (".mp3", ".flac", ".opus", ".m4a", ".ogg", ".wav", ".aac", ".wma")


def repair(s):
    """Вернёт исправленную строку или None.

    Признак: каждый символ из диапазона À-ÿ после перекодирования становится
    кириллицей. Раньше требовалась треть кириллицы во всей строке — из-за
    этого пропускалось "Áóìì feat. Czar & D Vers", где русского всего слово.
    """
    if not s:
        return None
    n = sum(1 for c in s if "À" <= c <= "ÿ")
    if n < 2:
        return None
    try:
        cand = s.encode("latin-1").decode("cp1251")
    except Exception:
        return None
    m = sum(1 for c in cand if "Ѐ" <= c <= "ӿ")
    return cand if m >= n * 0.8 else None


def process(path, dry):
    try:
        f = mutagen.File(path, easy=True)
    except Exception:
        return 0
    if f is None:
        return 0
    changed = 0
    for field in FIELDS:
        vals = f.get(field)
        if not vals:
            continue
        new = []
        hit = False
        for v in vals:
            r = repair(v)
            if r:
                hit = True
                new.append(r)
            else:
                new.append(v)
        if hit:
            print("   %s: %s -> %s" % (field, vals[0][:40], new[0][:40]))
            if not dry:
                f[field] = new
            changed += 1
    if changed and not dry:
        try:
            f.save()
        except Exception as e:
            print("   ОШИБКА записи: %s" % e)
            return 0
    return 1 if changed else 0


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry" in sys.argv
    root = args[0] if args else env.INCOMING_DIR
    total = fixed = 0
    for dirpath, _, files in os.walk(root):
        for name in files:
            if not name.lower().endswith(EXT):
                continue
            total += 1
            p = os.path.join(dirpath, name)
            if process(p, dry):
                fixed += 1
                print("   ^ %s" % p)
    print("проверено файлов: %d, исправлено: %d%s" % (total, fixed, " (вхолостую)" if dry else ""))


if __name__ == "__main__":
    main()
