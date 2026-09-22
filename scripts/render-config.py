#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Разложить конфиги из config/ в CONFIG_DIR, подставив окружение.

Зачем отдельный шаг: confuse, на котором стоит конфиг beets, не подставляет
переменные окружения внутрь значений — `client_id: $SPOTIFY_CLIENT_ID`
доедет до Spotify ровно такой строкой. Поэтому ключи живут в окружении, а в
репозитории лежит шаблон, который разворачивается перед запуском.

Файлы `*.template` подставляются и теряют суффикс; остальные копируются как
есть. Готовый конфиг НЕ перезаписывается, если его правили руками после
разворачивания — иначе перезапуск контейнера стирал бы ручные правки. Для
принудительной перезаписи есть --force.

    python3 scripts/render-config.py [--force] [--dry]
"""
import os
import shutil
import string
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import env  # noqa: E402

SRC = os.path.join(os.path.dirname(HERE), "config")
FORCE = "--force" in sys.argv
DRY = "--dry" in sys.argv

# Что обязано быть в окружении, чтобы конфиг получился рабочим. Пустые ключи
# Spotify не ошибка сами по себе, но плагин с ними молча не находит ничего —
# лучше сказать об этом на старте, чем оставить гадать по пустым результатам.
REQUIRED = ("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET")

# Значения, подставляемые в шаблоны. Помимо ключей — пути, чтобы в конфиге
# beets не осталось ни одного зашитого каталога.
VALUES = {
    "MUSIC_DIR": env.MUSIC_DIR,
    "INCOMING_DIR": env.INCOMING_DIR,
    "RESIDUE_DIR": env.RESIDUE_DIR,
    "CONFIG_DIR": env.CONFIG_DIR,
}


def main():
    if not os.path.isdir(SRC):
        sys.exit("нет папки с конфигами: %s" % SRC)
    os.makedirs(env.CONFIG_DIR, exist_ok=True)

    values = dict(VALUES)
    missing = []
    for k in REQUIRED:
        v = os.environ.get(k, "")
        values[k] = v
        if not v:
            missing.append(k)

    written = kept = 0
    for name in sorted(os.listdir(SRC)):
        src = os.path.join(SRC, name)
        if not os.path.isfile(src):
            continue
        out_name = name[:-len(".template")] if name.endswith(".template") else name
        dst = os.path.join(env.CONFIG_DIR, out_name)

        if os.path.exists(dst) and not FORCE:
            print("  есть, не трогаю: %s" % out_name)
            kept += 1
            continue

        if name.endswith(".template"):
            text = open(src, encoding="utf-8").read()
            try:
                # safe_substitute, а не substitute: незнакомый ${...} лучше
                # оставить на месте, чем уронить разворачивание целиком
                text = string.Template(text).safe_substitute(values)
            except Exception as e:
                sys.exit("не подставилось в %s: %s" % (name, e))
            print("  %s -> %s" % (name, dst))
            if not DRY:
                with open(dst, "w", encoding="utf-8", newline="\n") as f:
                    f.write(text)
        else:
            print("  %s -> %s" % (name, dst))
            if not DRY:
                shutil.copyfile(src, dst)
        written += 1

    print()
    print("разложено: %d, оставлено как было: %d%s"
          % (written, kept, "   [СУХОЙ ПРОГОН]" if DRY else ""))
    if missing:
        print()
        print("!! не заданы: %s" % ", ".join(missing))
        print("   Spotify без них работать не будет: плагин промолчит, а треки")
        print("   останутся неопознанными. Ключи берутся на")
        print("   https://developer.spotify.com/dashboard (Create app).")


if __name__ == "__main__":
    main()
