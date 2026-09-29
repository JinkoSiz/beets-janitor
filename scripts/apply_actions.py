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
  download    {"release_id"}
              отправить в downtify то, что в релизе решено скачать
  accept      {"item_id", "track_id"}
              принять кандидата, которого retry отклонил: теги по нему
  replace     {"item_id", "file"}
              заменить звук дорожки загруженным файлом: старый — в
              карантин, теги и обложка — из базы beets в новый файл
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

UPLOADS = env.UPLOADS_DIR
RUN_FLAG = env.RUN_FLAG

# поля beets, которые разрешено возвращать откатом: только теги дорожки,
# а не служебные (id, path, album_id меняются своими действиями)
ROLLBACK_FIELDS = {"album", "albumartist", "artist", "title", "year", "month", "day", "disc",
                   "disctotal", "track", "comp", "mb_trackid", "mb_albumid", "data_source", "label", "genre",
                   "original_year", "original_month", "original_day"}


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


def do_accept(con, lib, p):
    """Принять кандидата из очереди «Как есть» — тем же путём, что retry."""
    import retry
    from beets.autotag import match as amatch
    item = lib.get_item(int(p["item_id"]))
    if item is None:
        raise Refused("дорожки больше нет в базе")
    tid = str(p.get("track_id") or "").strip()
    if not tid:
        raise Refused("у кандидата нет идентификатора")
    prop = amatch.tag_item(item, search_ids=[tid])
    cands = [c for c in prop.candidates if str(getattr(c.info, "track_id", "")) == tid] or prop.candidates[:1]
    if not cands:
        raise Refused("источник не отдал кандидата %s" % tid)
    retry.apply_match(cands[0], item, "пульт")
    return "принято: %s — %s" % (cands[0].info.artist, cands[0].info.title)


AUDIO_MAGIC = (b"ID3", b"fLaC", b"OggS", b"RIFF")


def do_replace(con, lib, p):
    """Заменить звук дорожки загруженным файлом.

    Так чинится подмена, когда правильный файл нашёлся руками: старый уходит
    в карантин (reason substitution), новый встаёт на его место, а теги и
    обложка переносятся из базы beets — их подмена не портила, испорчен
    был только звук.
    """
    import mediafile
    item = lib.get_item(int(p["item_id"]))
    if item is None:
        raise Refused("дорожки больше нет в базе")
    new = p.get("file", "")
    if not inside(new, UPLOADS) or not os.path.isfile(new):
        raise Refused("файл должен лежать в папке загрузок пульта")
    head = open(new, "rb").read(12)
    if not (head.startswith(AUDIO_MAGIC) or head[4:8] == b"ftyp" or head[:2] == b"\xff\xfb" or head[:2] == b"\xff\xf3"):
        raise Refused("это не похоже на звуковой файл")
    old = item.path.decode("utf-8", "replace")
    if not inside(old, env.MUSIC_DIR):
        raise Refused("путь вне фонотеки: %s" % old)
    images = []
    try:
        images = mediafile.MediaFile(old).images if os.path.isfile(old) else []
    except Exception:
        pass
    ext = os.path.splitext(new)[1].lower() or os.path.splitext(old)[1]
    target = os.path.splitext(old)[0] + ext
    if os.path.isfile(old):
        q = free_path(os.path.join(env.DUPES_DIR, os.path.relpath(old, env.MUSIC_DIR)))
        os.makedirs(os.path.dirname(q), exist_ok=True)
        shutil.move(old, q)
        os.utime(q, None)
        jdb.quarantine_add(con, q, old, "substitution", title=str(item.title), artist=str(item.artist),
                           album_id=item.album_id or 0)
    if os.path.exists(target):
        target = free_path(target)
    shutil.move(new, target)
    from beets import util
    item.path = util.bytestring_path(target)
    f = mediafile.MediaFile(target)
    item.length, item.bitrate, item.format = f.length, f.bitrate, f.format
    item.samplerate, item.bitdepth, item.channels = f.samplerate, f.bitdepth or 0, f.channels
    item.store()
    item.try_write()
    if images:
        f = mediafile.MediaFile(target)
        f.images = images
        f.save()
    jdb.log_event(con, "panel", "replace", {"path": old}, {"path": target, "size": os.path.getsize(target)},
                  item_id=item.id, path=target)
    return "звук заменён: %s" % os.path.relpath(target, env.MUSIC_DIR)


def do_download(con, lib, p):
    # файлы фонотеки не трогает: downtify кладёт скачанное в incoming, а
    # импортирует его сторож обычным порядком
    import download
    try:
        return download.release(con, int(p["release_id"]), retry=bool(p.get("retry")))
    except Exception as e:
        # иначе релиз так и висел бы «в очереди»: в пульте он станет
        # «не скачалось» с причиной и кнопкой «Ещё раз»
        con.execute("UPDATE releases SET status='failed', note=? WHERE id=?", (str(e)[:300], int(p["release_id"])))
        raise


def do_run(con, lib, p):
    what = p.get("what", "nightly")
    if what == "verify":
        # сверка свежих импортов коротка — выполняем сразу, не дожидаясь ночи
        import subprocess
        r = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify.py"),
                            "--new"], capture_output=True, text=True, timeout=1800)
        tail = (r.stdout or "").strip().splitlines()[-1:] or ["готово"]
        return "сверка: %s" % tail[0]
    if what != "nightly":
        raise Refused("неизвестный запуск: %s" % what)
    os.makedirs(os.path.dirname(RUN_FLAG), exist_ok=True)
    with open(RUN_FLAG, "w") as fh:
        fh.write(jdb.now())
    return "ночная работа начнётся на следующем цикле"


HANDLERS = {
    "quarantine": do_quarantine,
    "restore": do_restore,
    "import": do_import,
    "set_cover": do_set_cover,
    "rollback": do_rollback,
    "download": do_download,
    "accept": do_accept,
    "replace": do_replace,
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
