#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Выполнить решения, принятые в пульте.

Пульт сам файлы не трогает: он кладёт действие в очередь (таблица actions
общей базы), а выполняет его этот скрипт внутри beets-watch — единственного,
кто пишет в библиотеку. Вызывается на каждом цикле сторожа.

Поддерживаемые действия:

  quarantine  {"item_id"|"path", "reason", "similarity", "kept"}
              убрать дорожку в карантин
  restore     {"quarantine_id"}
              вернуть файл из карантина на прежнее место и в базу beets
  import      {"quarantine_id"}
              импортировать файл из карантина рядом с имеющейся копией —
              для того, что leftovers.py убрал из incoming как дубль
  set_cover   {"item_ids": [...], "image": путь к загруженной картинке}
              встроить обложку в файлы и, если папка принадлежит одному
              альбому, положить рядом cover.jpg
  rollback    {"event_id"}
              откатить запись журнала: теги — к прежним значениям, файл —
              на прежнее место
  run         {"what": "nightly"}
              запустить ночную работу на следующем цикле

Безопасность. Пульт смотрит в интернет, и захваченный пульт не должен
получить возможность двигать произвольные файлы. Поэтому каждый путь
сверяется: трогаем только то, что лежит внутри фонотеки или карантина, а
картинки для обложек — только из папки загрузок пульта.
"""
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import env  # noqa: E402
import janitordb as jdb  # noqa: E402

UPLOADS = os.path.join(env.CONFIG_DIR, "uploads")
RUN_FLAG = os.path.join(env.CONFIG_DIR, ".run-now")

# поля beets, которые разрешено возвращать откатом: только теги дорожки,
# а не служебные (id, path, album_id меняются своими действиями)
ROLLBACK_FIELDS = {"album", "albumartist", "artist", "title", "year", "month", "day", "disc",
                   "disctotal", "track", "comp", "mb_trackid", "mb_albumid", "data_source", "label", "genre"}


class Refused(Exception):
    """Действие отклонено проверкой — это не сбой, а защита."""


def inside(path, *roots):
    """Лежит ли путь внутри одного из корней (с раскрытием ссылок и ..)."""
    real = os.path.realpath(path)
    for r in roots:
        r = os.path.realpath(r)
        if real == r or real.startswith(r + os.sep):
            return True
    return False


def free_path(path):
    """Путь, не занятый существующим файлом: добавляет .1, .2…"""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    n = 1
    while os.path.exists("%s.%d%s" % (stem, n, ext)):
        n += 1
    return "%s.%d%s" % (stem, n, ext)


def open_library():
    import retry
    return retry.open_library()


# ---------------------------------------------------------------- действия
def do_quarantine(con, lib, p):
    item = lib.get_item(int(p["item_id"])) if p.get("item_id") else None
    path = item.path.decode("utf-8", "replace") if item else p.get("path")
    if not path or not inside(path, env.MUSIC_DIR):
        raise Refused("путь вне фонотеки: %s" % path)
    if not os.path.isfile(path):
        raise Refused("файла уже нет: %s" % path)
    reason = p.get("reason", "duplicate")
    root = env.BROKEN_DIR if reason == "broken" else env.DUPES_DIR
    dst = free_path(os.path.join(root, os.path.relpath(path, env.MUSIC_DIR)))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.move(path, dst)
    os.utime(dst, None)
    title = str(item.title) if item else os.path.basename(path)
    artist = str(item.artist) if item else None
    jdb.quarantine_add(con, dst, path, reason, similarity=p.get("similarity"), kept_path=p.get("kept"),
                       title=title, artist=artist, album_id=item.album_id if item else None)
    jdb.log_event(con, "panel", "quarantine", {"path": path}, {"path": dst, "reason": reason},
                  item_id=item.id if item else None, path=path)
    if item:
        item.remove(delete=False)
    return "убрано: %s" % os.path.relpath(path, env.MUSIC_DIR)


def do_restore(con, lib, p):
    row = con.execute("SELECT * FROM quarantine WHERE id=?", (int(p["quarantine_id"]),)).fetchone()
    if row is None:
        raise Refused("нет такой записи карантина")
    src, orig = row["path"], row["original_path"]
    if not inside(src, env.DUPES_DIR, env.BROKEN_DIR):
        raise Refused("путь вне карантина: %s" % src)
    if not inside(orig, env.MUSIC_DIR):
        raise Refused("возвращать можно только в фонотеку: %s" % orig)
    if not os.path.isfile(src):
        con.execute("UPDATE quarantine SET status='purged' WHERE id=?", (row["id"],))
        raise Refused("файла в карантине уже нет — удалён по сроку")
    dst = free_path(orig)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.move(src, dst)

    # В базу заводим напрямую, а не через `beet import`: импорт одного файла
    # делает из дорожки альбома одиночку, и следующий дедуп снова счёл бы её
    # пустышкой. Возвращаем в тот альбом, из которого убрали; если его уже
    # нет — в альбом из той же папки с теми же тегами. album_id = 0 значит,
    # что дорожка была одиночкой: такой она и вернётся. NULL — старая запись,
    # где альбом неизвестен, для неё и работает подбор по папке.
    from beets.library import Item
    item = Item.from_path(dst)
    lib.add(item)
    target = None
    if row["album_id"] == 0:
        pass
    elif row["album_id"] and lib.get_album(row["album_id"]) is not None:
        target = row["album_id"]
    else:
        folder = os.path.dirname(dst)
        for other in lib.items():
            op = other.path.decode("utf-8", "replace")
            if other.id == item.id or os.path.dirname(op) != folder or not other.album_id:
                continue
            if str(other.album) == str(item.album) and str(other.albumartist) == str(item.albumartist):
                target = other.album_id
                break
    if target:
        item.album_id = target
        item.store()
    con.execute("UPDATE quarantine SET status='restored' WHERE id=?", (row["id"],))
    jdb.log_event(con, "panel", "restore", {"path": src}, {"path": dst}, item_id=item.id, path=dst)
    return "возвращено: %s" % os.path.relpath(dst, env.MUSIC_DIR)


def do_import(con, lib, p):
    """Импортировать файл из карантина всё равно.

    Для того, что leftovers.py убрал из incoming как дубль: «на место» его
    не вернуть — в incoming beets снова его пропустит. Поэтому импортируем
    прямо из карантина с duplicate_action: keep, и move переносит файл в
    фонотеку по обычным правилам путей.
    """
    import subprocess
    row = con.execute("SELECT * FROM quarantine WHERE id=?", (int(p["quarantine_id"]),)).fetchone()
    if row is None:
        raise Refused("нет такой записи карантина")
    src = row["path"]
    if not inside(src, env.DUPES_DIR, env.BROKEN_DIR):
        raise Refused("путь вне карантина: %s" % src)
    if not os.path.isfile(src):
        con.execute("UPDATE quarantine SET status='purged' WHERE id=?", (row["id"],))
        raise Refused("файла в карантине уже нет — удалён по сроку")
    cfg = os.path.join(env.CONFIG_DIR, "import-single-keep.yaml")
    if not os.path.exists(cfg):
        raise Refused("нет %s — разложите конфиги (render-config.py)" % cfg)
    r = subprocess.run(["timeout", "600", "beet", "-c", cfg, "import", "-q", "-s", src],
                       capture_output=True, text=True)
    if os.path.exists(src):
        raise Exception("beets не взял файл (код %d): %s" % (r.returncode, (r.stderr or r.stdout).strip()[-200:]))
    con.execute("UPDATE quarantine SET status='restored' WHERE id=?", (row["id"],))
    jdb.log_event(con, "panel", "import", {"path": src}, {"from": "quarantine"}, path=src)
    return "импортировано: %s" % os.path.basename(src)


def do_set_cover(con, lib, p):
    import mediafile
    img = p.get("image", "")
    if not inside(img, UPLOADS) or not os.path.isfile(img):
        raise Refused("картинка должна лежать в папке загрузок пульта")
    data = open(img, "rb").read()
    if len(data) < 1000:
        raise Refused("файл картинки подозрительно мал")
    done, folders = 0, set()
    for iid in p.get("item_ids", []):
        item = lib.get_item(int(iid))
        if item is None:
            continue
        path = item.path.decode("utf-8", "replace")
        if not inside(path, env.MUSIC_DIR) or not os.path.isfile(path):
            continue
        f = mediafile.MediaFile(path)
        f.images = [mediafile.Image(data=data, desc=None, type=mediafile.ImageType.front)]
        f.save()
        folders.add(os.path.dirname(path))
        done += 1
        jdb.log_event(con, "panel", "cover", {"cover": None}, {"cover": os.path.basename(img)},
                      item_id=item.id, path=path)
    # cover.jpg в папку — только если в ней один альбом: Navidrome ставит файл
    # в папке выше встроенной картинки, и в общей папке он подменил бы собой
    # обложки всех остальных релизов
    import covers
    for d in folders:
        if covers.folder_is_single_album(d, mediafile):
            with open(os.path.join(d, "cover.jpg"), "wb") as fh:
                fh.write(data)
    return "обложка встроена в %d файлов" % done


def do_rollback(con, lib, p):
    ev = con.execute("SELECT * FROM events WHERE id=?", (int(p["event_id"]),)).fetchone()
    if ev is None:
        raise Refused("нет такой записи журнала")
    if ev["reverted_by"]:
        raise Refused("эта запись уже откачена")
    before, after = jdb.loads(ev["before"], {}), jdb.loads(ev["after"], {})
    if ev["op"] == "quarantine":
        row = con.execute("SELECT id FROM quarantine WHERE path=?", (after.get("path"),)).fetchone()
        if row is None:
            raise Refused("файла нет в карантине")
        msg = do_restore(con, lib, {"quarantine_id": row["id"]})
    elif isinstance(before, dict) and before.get("path") and after.get("path") and ev["op"] in ("move", "rename"):
        cur_path, old_path = after["path"], before["path"]
        if not inside(cur_path, env.MUSIC_DIR) or not inside(old_path, env.MUSIC_DIR):
            raise Refused("путь вне фонотеки")
        if not os.path.isfile(cur_path):
            raise Refused("файла на новом месте уже нет")
        dst = free_path(old_path)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(cur_path, dst)
        if ev["item_id"]:
            item = lib.get_item(ev["item_id"])
            if item is not None:
                from beets import util
                item.path = util.bytestring_path(dst)
                item.store()
        msg = "файл возвращён: %s" % os.path.relpath(dst, env.MUSIC_DIR)
    elif isinstance(before, dict) and ev["item_id"]:
        fields = {k: v for k, v in before.items() if k in ROLLBACK_FIELDS}
        if not fields:
            raise Refused("в этой записи нечего возвращать")
        item = lib.get_item(ev["item_id"])
        if item is None:
            raise Refused("дорожки больше нет в базе")
        for k, v in fields.items():
            item[k] = v
        item.store()
        item.try_write()
        msg = "теги возвращены: %s" % ", ".join(sorted(fields))
    else:
        raise Refused("такую запись откатить нельзя")
    rid = jdb.log_event(con, "panel", "rollback", {"event_id": ev["id"]}, {"done": msg},
                        item_id=ev["item_id"], path=ev["path"])
    con.execute("UPDATE events SET reverted_by=? WHERE id=?", (rid, ev["id"]))
    return msg


def do_run(con, lib, p):
    what = p.get("what", "nightly")
    if what != "nightly":
        raise Refused("неизвестный запуск: %s" % what)
    with open(RUN_FLAG, "w") as fh:
        fh.write(jdb.now())
    return "ночная работа начнётся на следующем цикле"


HANDLERS = {
    "quarantine": do_quarantine,
    "restore": do_restore,
    "import": do_import,
    "set_cover": do_set_cover,
    "rollback": do_rollback,
    "run": do_run,
}


def main():
    con = jdb.connect()
    rows = jdb.take_pending(con)
    if not rows:
        return
    lib = open_library()
    ok = bad = 0
    for r in rows:
        handler = HANDLERS.get(r["kind"])
        try:
            if handler is None:
                raise Refused("неизвестное действие: %s" % r["kind"])
            msg = handler(con, lib, jdb.loads(r["payload"], {}))
            jdb.finish_action(con, r["id"], True, msg)
            ok += 1
            print("-- действие %d %s: %s" % (r["id"], r["kind"], msg))
        except Refused as e:
            jdb.finish_action(con, r["id"], False, "отклонено: %s" % e)
            bad += 1
            print("!! действие %d %s отклонено: %s" % (r["id"], r["kind"], e))
        except Exception as e:
            jdb.finish_action(con, r["id"], False, "ошибка: %s" % str(e)[:300])
            bad += 1
            print("!! действие %d %s упало: %s" % (r["id"], r["kind"], e))
    print("-- действий из пульта: выполнено %d, не выполнено %d" % (ok, bad))


if __name__ == "__main__":
    main()
