#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Что осталось в incoming после импорта.

beets пропускает дубли (duplicate_action: skip) и оставляет файл лежать в
incoming. Сторож пытался бы импортировать его каждые пять минут, а ночная
работа, которая ждёт пустого incoming, не запускалась бы вовсе. Так и вышло
26–28 сентября 2026: застрял один трек, и три ночи прошли впустую.

Дублем beets считает то же сочетание «исполнитель + название», звук он не
слушает. А разные версии одной песни мы не убираем (решение от 2026-09-22):
застрявший тогда трек оказался длиннее имеющегося на сорок секунд. Поэтому
каждый оставшийся файл сравниваем по звуку с тем, что уже есть:

  тот же звук и та же длина    -> в карантин (_dupes/_incoming) с записью
                                  в общей базе: оттуда можно импортировать
  звук другой                  -> импортируем всё равно, рядом с имеющейся
  в библиотеке такого нет      -> импорт сорвался по другой причине. Свежий
                                  файл ждёт следующей попытки, пролежавший
                                  дольше суток уходит в карантин, чтобы не
                                  держать очередь

  leftovers.py [--dry]
"""
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import env  # noqa: E402
import janitordb as jdb  # noqa: E402
from dedup import FP_MIN, LEN_TOLERANCE, same_audio  # noqa: E402

DRY = "--dry" in sys.argv
KEEP_CFG = os.path.join(env.CONFIG_DIR, "import-single-keep.yaml")
STALE_HOURS = 24
PARKED = os.path.join(env.DUPES_DIR, "_incoming")


def leftovers():
    out = []
    for dirpath, _, files in os.walk(env.INCOMING_DIR):
        for f in sorted(files):
            if f.lower().endswith(env.AUDIO_EXT):
                out.append(os.path.join(dirpath, f))
    return out


def tags(path):
    import mediafile
    try:
        m = mediafile.MediaFile(path)
        return str(m.artist or ""), str(m.title or ""), float(m.length or 0)
    except Exception:
        return "", "", 0.0


def park(con, path, reason, sim=None, kept=None, artist=None, title=None):
    """Убрать из incoming в карантин и записать, откуда и почему."""
    dst = os.path.join(PARKED, os.path.relpath(path, env.INCOMING_DIR))
    if DRY:
        return dst
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.move(path, dst)
    # срок хранения в карантине считается от даты файла
    os.utime(dst, None)
    try:
        jdb.quarantine_add(con, dst, path, reason, similarity=None if sim is None else round(sim, 3),
                           kept_path=kept, title=title, artist=artist, album_id=0)
        jdb.log_event(con, "leftovers", "quarantine", {"path": path}, {"path": dst, "why": reason})
    except Exception as e:
        print("   !! не записалось в базу: %s" % str(e)[:80])
    return dst


def import_anyway(con, path, sim, other):
    if DRY:
        return True
    if not os.path.exists(KEEP_CFG):
        print("   !! нет %s — разложите конфиги (render-config.py)" % KEEP_CFG)
        return False
    r = subprocess.run(["timeout", "600", "beet", "-c", KEEP_CFG, "import", "-q", "-s", path],
                       capture_output=True, text=True)
    if os.path.exists(path):
        print("   !! beets не взял и с keep (код %d): %s" % (r.returncode, (r.stderr or r.stdout).strip()[-120:]))
        return False
    try:
        jdb.log_event(con, "leftovers", "import_version", {"path": path},
                      {"similarity": None if sim is None else round(sim, 3), "alongside": other})
    except Exception:
        pass
    return True


def main():
    files = leftovers()
    if not files:
        return
    import retry
    lib = retry.open_library()
    by_key = {}
    for it in lib.items(""):
        by_key.setdefault((str(it.artist).strip().lower(), str(it.title).strip().lower()), []).append(it)
    con = None if DRY else jdb.connect()

    parked = imported = waiting = 0
    for f in files:
        artist, title, length = tags(f)
        rel = os.path.relpath(f, env.INCOMING_DIR)
        same = by_key.get((artist.strip().lower(), title.strip().lower()), [])
        same = [i for i in same if os.path.isfile(i.path.decode("utf-8", "replace"))]
        if not same:
            age = (time.time() - os.path.getmtime(f)) / 3600
            if age < STALE_HOURS:
                print("-- не импортирован, жду следующей попытки (%.0f ч): %s" % (age, rel))
                waiting += 1
                continue
            park(con, f, "import_failed", artist=artist, title=title)
            print("-- не импортируется дольше суток -> карантин: %s" % rel)
            parked += 1
            continue

        best, best_item = None, None
        for it in same:
            p = it.path.decode("utf-8", "replace")
            s = same_audio(f, p)
            if s is not None and (best is None or s > best):
                best, best_item = s, it
        other = best_item.path.decode("utf-8", "replace") if best_item else same[0].path.decode("utf-8", "replace")
        dl = abs(length - float(best_item.length or 0)) if best_item is not None and length else None
        if best is not None and best >= FP_MIN and dl is not None and dl <= LEN_TOLERANCE:
            park(con, f, "duplicate", sim=best, kept=other, artist=artist, title=title)
            print("-- дубль (%.0f%%) -> карантин: %s\n     уже есть: %s" % (best * 100, rel, env.in_music(other)))
            parked += 1
        else:
            why = ("звук %.0f%%" % (best * 100) if best is not None else "звук не сравнился") + \
                  ("" if dl is None else ", длина расходится на %.0f с" % dl)
            ok = import_anyway(con, f, best, other)
            print("-- другая версия (%s) -> %s: %s\n     рядом с: %s"
                  % (why, "импортирована" if ok else "НЕ импортирована", rel, env.in_music(other)))
            imported += ok

    print("остатки incoming: в карантин %d, импортировано как другая версия %d, ждут %d"
          % (parked, imported, waiting))
    stats_out = os.environ.get("JANITOR_STATS_OUT")
    if stats_out and not DRY:
        import json
        with open(stats_out, "w", encoding="utf-8") as f:
            json.dump({"leftovers_parked": parked, "leftovers_versions": imported,
                       "leftovers_waiting": waiting}, f)


if __name__ == "__main__":
    main()
