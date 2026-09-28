#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Один альбом — один экземпляр.

Navidrome собирает альбом по паре «исполнитель альбома + название». Всё, что
у нас числится под одной такой парой, для слушателя — один альбом, и внутри
него не должно быть ни двух копий дорожки, ни двух изданий, ни Disc 0 рядом
с Disc 1. dedup.py такие случаи нарочно обходил («второе издание, не трогаю»),
отсюда тройные Slim Shady LP и файлы вида `02 My Name Is.1.mp3`.

Сначала общий шаг 0: старый `duplicate_action: merge` сливал в один объект
beets всё, что считал дублем, и в базе остались объекты на полторы сотни
дорожек с сотней разных тегов альбома. Плеер смотрит на теги и такого не
видит, а любой скрипт, который верит объекту (обложки, dedup, слияние
изданий), берёт из него чужое имя. Поэтому такие объекты разбираются по
тегам: каждая группа тегов — свой объект, одиночная дорожка — одиночка.

Порядок для каждой пары:
  1. обломки сборников: дорожки под Various Artists, у которых есть
     одноимённый альбом настоящего исполнителя и сам исполнитель в авторах,
     переезжают к нему;
  2. дубли: одинаковое название (без feat.) и длительность в пределах 2с,
     подтверждённые отпечатком; остаётся лучшая копия (без потерь > битрейт >
     папка основного издания > имя без суффикса .1), остальные — в карантин;
  3. слияние: если пара всё ещё разложена по нескольким объектам beets
     (изданиям), всё сводится к основному — самому полному; файлы переезжают в
     его папку, альбомные поля берутся у него;
  4. диски: если номера дисков только 0 и 1 — это не два диска, это один;
  5. файлы с нулевой длительностью — в карантин битых.

Сухой прогон (--dry) ничего не пишет и не двигает.
"""
import collections
import json
import os
import re
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from beets import util  # noqa: E402
from beets.library import Album  # noqa: E402

import dedup  # noqa: E402
import env  # noqa: E402
import janitordb as jdb  # noqa: E402
import retry  # noqa: E402
from retry import artist_ok  # noqa: E402

DRY = "--dry" in sys.argv
DUPES = env.DUPES_DIR
BROKEN = env.BROKEN_DIR
REPORT = env.CONSOLIDATE_LOG
# Длина — лишь грубый фильтр, решает отпечаток. У изданий одного альбома
# дорожки расходятся на 2–4 с (с паузами между треками и без), а радиоверсия
# короче оригинала на полминуты и больше — потому допуск 15 с.
LEN_TOL = 15
LEN_TIGHT = 2
# Пороги ниже, чем в dedup.py (0.90), и это осознанно: сюда попадают только
# пары с одинаковым альбомом, названием и близкой длиной, и вопрос лишь в
# том, та же это запись с другого пресса или другая. Разные песни дают ~50%,
# инструментал против вокала — до ~60%, а тот же трек с другим мастерингом —
# 70–85%; короткие скиты (речь) расходятся сильнее.
FP_SAME = 0.65          # одна и та же запись
FP_SAME_SHORT = 0.60    # то же для дорожек короче 90 с
FP_SAME_LENDIFF = 0.65  # разная длина сама по себе планку не поднимает: издания
                        # без пауз между треками короче на 2–4 с при тех же 70–85%
FP_MAYBE = 0.55         # похоже, но не уверен — только в отчёт
FP_ALIEN = 0.55         # ниже — одинаковые теги, но другой звук: подмена
SHORT = 90
FP_OFFSET = 120         # сдвиг при сравнении, кадров (~15 с): у изданий разный разбег тишины
VA = ("various artists", "разные исполнители", "сборник")
LOSSLESS = ("FLAC", "ALAC", "APE", "WAV", "AIFF")
SUFFIX = re.compile(r"\.(\d+)(\.[A-Za-z0-9]+)$")

# те же гомоглифы, что в normalize.py: «4K» латиницей и «4К» кириллицей — одно
CYR = "АВЕКМНОРСТУХаеорсух"
LAT = "ABEKMHOPCTYXaeopcyx"
TBL = str.maketrans(CYR, LAT)

lines = []
_con = None


def log(s=""):
    lines.append(s)


def db():
    """Соединение с общей базой. В сухом прогоне только читаем, и если базы
    ещё нет — не создаём её: сухой прогон не должен оставлять следов."""
    global _con
    if _con is None:
        if DRY and not os.path.exists(env.JANITOR_DB):
            return None
        _con = jdb.connect()
    return _con


def journal(**rec):
    """Запись в журнал общей базы: по ней пульт показывает «было -> стало»
    и умеет откатить. src/dst превращаются в пути до и после, остальные
    поля ложатся подробностями рядом с «после»."""
    if DRY:
        return
    op = rec.pop("op")
    item_id = rec.pop("id", None)
    before = rec.pop("before", None)
    after = rec.pop("after", None)
    src = rec.pop("src", None)
    dst = rec.pop("dst", None)
    path = rec.pop("path", None) or src
    if src is not None or dst is not None:
        before = dict(before or {})
        after = dict(after or {})
        if src is not None:
            before.setdefault("path", src)
        if dst is not None:
            after.setdefault("path", dst)
    if rec:
        after = dict(after or {})
        after.update(rec)
    jdb.log_event(db(), "consolidate", op, before, after, item_id=item_id, path=path)


def file_info(it):
    """Что пульт покажет о файле в карточке решения."""
    p = path_of(it)
    return {"path": p, "item_id": it.id, "format": str(it.format), "bitrate": int(it.bitrate or 0) // 1000,
            "length": round(float(it.length or 0), 1), "title": str(it.title), "artist": str(it.artist)}


def ask_pair(kind, a, b, sim, hint=None):
    """Поставить спорную пару в очередь решений. Возвращает решение, если
    на этот вопрос уже отвечали, иначе None. В сухом прогоне только читает."""
    key = jdb.pair_key(kind, path_of(a), path_of(b))
    con = db()
    if con is None:
        return None
    if DRY:
        status, decision = jdb.review_state(con, key)
        return decision if status in ("resolved", "dismissed") else None
    payload = {"album": str(a.album), "albumartist": str(a.albumartist or a.artist),
               "similarity": round(sim, 3), "files": [file_info(a), file_info(b)], "hint": hint}
    status, decision = jdb.ask(con, kind, key, str(a.title), payload)
    return decision if status in ("resolved", "dismissed") else None


def norm(s):
    s = str(s or "").translate(TBL).lower().replace("ё", "е")
    return re.sub(r"[^a-zа-я0-9]+", " ", s).strip()


def is_va(s):
    return norm(s) in VA


# Пометки изданий: «…And Justice for All» и «…And Justice for All (Remastered)»
# для слушателя один альбом, а «(II)», «(Instrumental)», «(Live)» — другие
# работы. Скобка или хвост после тире снимаются, только если состоят целиком
# из таких слов и годов.
EDITION_WORDS = {
    "remaster", "remastered", "remasters", "deluxe", "expanded", "special", "limited",
    "anniversary", "explicit", "bonus", "digital", "edition", "version",
    "reissue", "re", "issue", "tour", "collector", "collectors", "s", "ed",
    "ремастер", "делюкс", "издание", "переиздание", "версия", "расширенное",
    "юбилейное", "специальное",
}
BRACKET = re.compile(r"\s*[\(\[]([^\)\]]*)[\)\]]\s*$")
DASH_TAIL = re.compile(r"\s+[-–—]\s+([^-–—]+)$")


def _edition_only(text):
    toks = [t for t in re.split(r"[^a-zа-яё0-9]+", text.lower()) if t]
    return bool(toks) and all(t in EDITION_WORDS or re.fullmatch(r"(19|20)\d{2}", t) for t in toks)


def strip_edition(name):
    s = str(name or "").strip()
    for _ in range(3):
        m = BRACKET.search(s)
        if m and _edition_only(m.group(1)):
            s = s[:m.start()].strip()
            continue
        m = DASH_TAIL.search(s)
        if m and _edition_only(m.group(1)):
            s = s[:m.start()].strip()
            continue
        break
    return s


def key_of(it):
    aa = str(it.albumartist or "").strip() or str(it.artist or "")
    return (norm(aa), norm(strip_edition(it.album)))


# ----------------------------------------------------------------- отпечатки
POP = bytes(bin(i).count("1") for i in range(256))


def bits(x):
    x &= 0xFFFFFFFF
    return POP[x & 255] + POP[(x >> 8) & 255] + POP[(x >> 16) & 255] + POP[(x >> 24) & 255]


def same_audio(a, b):
    """Доля совпавших бит при лучшем сдвиге; None — отпечаток не снялся.

    dedup.same_audio допускает сдвиг лишь в полторы секунды, а у разных
    изданий вступительная тишина расходится сильнее — потому сдвиг шире.
    """
    fa, fb = dedup.fingerprint(a), dedup.fingerprint(b)
    if not fa or not fb:
        return None
    # перекрытие не короче 60% меньшего отпечатка: у скита в 15 секунд
    # отпечаток крохотный, и требовать от него 25 секунд бессмысленно
    min_overlap = max(40, int(0.6 * min(len(fa), len(fb))))
    best = 0.0
    for off in range(-FP_OFFSET, FP_OFFSET + 1):
        err = pairs = 0
        lo, hi = max(0, -off), min(len(fa), len(fb) - off)
        if hi - lo < min_overlap:
            continue
        for i in range(lo, hi):
            err += bits(fa[i] ^ fb[i + off])
        pairs = hi - lo
        sim = 1.0 - err / (pairs * 32.0)
        if sim > best:
            best = sim
    return best


# ----------------------------------------------------------------- утилиты
def path_of(it):
    return it.path.decode("utf-8", "replace")


def is_loose(p):
    """Свалка одиночных файлов, а не папка альбома (список — из dedup.py)."""
    return any(j in p for j in dedup.LOOSE)


def has_suffix(p):
    return bool(SUFFIX.search(os.path.basename(p)))


def rank(it, master_dir):
    """Меньше — лучше."""
    p = path_of(it)
    return (0 if str(it.format).upper() in LOSSLESS else 1,
            -int(it.bitrate or 0),
            0 if os.path.dirname(p) == master_dir else 1,
            1 if has_suffix(p) else 0,
            -os.path.getsize(p) if os.path.isfile(p) else 0)


def quarantine(it, root, why, reason="duplicate", similarity=None, kept=None):
    """Убрать файл в карантин. reason — duplicate | broken | substitution:
    по нему пульт группирует карантин и решает, есть ли что возвращать."""
    p = path_of(it)
    rel = env.in_music(p) if p.startswith(env.MUSIC_DIR + "/") else p.lstrip("/")
    dst = os.path.join(root, rel)
    log("      -> карантин (%s): %s" % (why, rel))
    if DRY:
        return
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.move(p, dst)
    os.utime(dst, None)
    # 0 — точно одиночка, при возврате альбом ей не подбирать;
    # NULL остаётся за старыми записями, где это неизвестно
    album_id = it.album_id or 0
    iid, artist, title, album = it.id, str(it.artist), str(it.title), str(it.album)
    # сначала база beets, потом журнал: если журнал не запишется, beets всё
    # равно не должен ссылаться на уехавший файл. Запись карантина потом
    # восстановит sync_quarantine по самому файлу
    it.remove(delete=False)
    try:
        journal(op="quarantine", why=why, src=p, dst=dst, id=iid, artist=artist,
                title=title, album=album, album_id=album_id)
        jdb.quarantine_add(db(), dst, p, reason, similarity=similarity, kept_path=kept,
                           title=title, artist=artist, album_id=album_id)
    except Exception as e:
        log("      !! не записалось в общую базу: %s" % str(e)[:80])


def move_into(it, dst_dir):
    """Перенести файл в папку основного издания, не затирая чужие."""
    src = path_of(it)
    if os.path.dirname(src) == dst_dir:
        return
    base = os.path.basename(src)
    dst = os.path.join(dst_dir, base)
    n = 0
    while os.path.exists(dst) and dst != src:
        n += 1
        stem, ext = os.path.splitext(base)
        dst = os.path.join(dst_dir, "%s.%d%s" % (stem, n, ext))
    log("      переезд: %s -> %s" % (src.replace(env.MUSIC_DIR + "/", ""), dst.replace(env.MUSIC_DIR + "/", "")))
    MOVES.append((src, dst))
    if DRY:
        return
    os.makedirs(dst_dir, exist_ok=True)
    shutil.move(src, dst)
    journal(op="move", src=src, dst=dst, id=it.id)
    it.path = util.bytestring_path(dst)
    it.store()


MOVES = []


def fix_playlists(stats):
    """Плейлисты m3u ссылаются на пути; после переездов правим ссылки, иначе
    Navidrome при пересинхронизации выкинет эти дорожки из списка."""
    if not MOVES:
        return
    by_src = dict(MOVES)
    for root, dirs, files in os.walk(env.MUSIC_DIR):
        for f in files:
            if not f.lower().endswith((".m3u", ".m3u8")):
                continue
            p = os.path.join(root, f)
            try:
                text = open(p, encoding="utf-8", errors="surrogateescape").read()
            except Exception:
                continue
            out, changed = [], 0
            for line in text.splitlines():
                s = line.strip()
                cand = s if s.startswith("/") else os.path.normpath(os.path.join(root, s))
                if cand in by_src:
                    dst = by_src[cand]
                    line = dst if s.startswith("/") else os.path.relpath(dst, root)
                    changed += 1
                out.append(line)
            if changed:
                log("плейлист %s: поправлено ссылок %d" % (p.replace(env.MUSIC_DIR + "/", ""), changed))
                stats["playlists_fixed"] += 1
                if not DRY:
                    open(p, "w", encoding="utf-8", errors="surrogateescape").write("\n".join(out) + "\n")
                    journal(op="playlist_fix", path=p, changed=changed)


def apply_vals(it, vals):
    """Проставить альбомные поля дорожке. True, если что-то поменялось."""
    changed = False
    for f, v in vals.items():
        try:
            cur = it[f]
        except KeyError:
            cur = None
        if cur != v:
            it[f] = v
            changed = True
    return changed


def try_write(it):
    try:
        it.write()
    except Exception as e:
        log("        !! теги не записались (%s): %s" % (os.path.basename(path_of(it))[:30], str(e)[:60]))


def playable(p):
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "csv=p=0", p], capture_output=True, text=True, timeout=60).stdout
        return float(out.strip() or 0) > 0
    except Exception:
        return False


# ----------------------------------------------------------------- шаги
def album_values(items):
    """Альбомные поля, какими их видит плеер: самое частое значение по
    дорожкам. Объекту beets тут не верим — см. шаг 0."""
    vals = {}
    for f in Album.item_keys:
        c = collections.Counter()
        for it in items:
            try:
                v = it[f]
            except KeyError:
                continue
            if isinstance(v, list):
                v = tuple(v)
            c[v] += 1
        if c:
            v = c.most_common(1)[0][0]
            vals[f] = list(v) if isinstance(v, tuple) else v
    return vals


def step_sync_from_files(lib, items, stats):
    """Шаг -1: база ← файл там, где они разошлись.

    Плеер читает теги из файлов, и решать, что дубль, а что нет, надо по той
    же картине. Все наши правки пишутся и в базу, и в файл, так что
    расхождение — это либо несостоявшаяся запись, либо правка тегов извне;
    в обоих случаях файл главнее.
    """
    import mediafile
    for it in items:
        p = path_of(it)
        try:
            f = mediafile.MediaFile(p)
        except Exception:
            continue
        changed = [fld for fld in ("album", "albumartist", "artist", "title")
                   if norm(it.get(fld)) != norm(getattr(f, fld, None))]
        if not changed:
            continue
        log("база/файл разошлись (%s): %s — %s: база «%s», файл «%s» -> беру из файла"
            % (",".join(changed), str(it.artist)[:20], str(it.title)[:24],
               str(it.get(changed[0]))[:30], str(getattr(f, changed[0], ""))[:30]))
        stats["synced"] += 1
        try:
            before = {fld: str(it.get(fld)) for fld in changed}
            # в память читаем всегда, чтобы сухой прогон видел ту же картину
            it.read()
            if not DRY:
                journal(op="sync_from_file", id=it.id, path=p, before=before,
                        after={fld: str(it.get(fld)) for fld in changed})
                it.store()
        except Exception as e:
            log("   !! не перечиталось: %s" % str(e)[:60])
            stats["errors"] += 1


def step_split_mixed(lib, items, stats):
    """Шаг 0: объект beets с дорожками разных альбомов -> по объекту на альбом."""
    by_album = collections.defaultdict(list)
    for it in items:
        if not it.singleton:
            by_album[it.album_id].append(it)
    for aid, its in by_album.items():
        a = lib.get_album(aid)
        if a is None:
            continue
        by_key = collections.defaultdict(list)
        for it in its:
            by_key[key_of(it)].append(it)
        if len(by_key) == 1:
            # теги едины, но само имя объекта могло разойтись с ними
            vals = album_values(its)
            if norm(a.album) != norm(vals.get("album")) or norm(a.albumartist) != norm(vals.get("albumartist")):
                log("объект «%s» переименован по тегам -> «%s» / %s"
                    % (str(a.album)[:40], str(vals.get("album"))[:40], vals.get("albumartist")))
                stats["renamed"] += 1
                if not DRY:
                    for f, v in vals.items():
                        a[f] = v
                    # inherit=False: иначе beets раскатает поля объекта на все его
                    # дорожки в базе, минуя файлы и наши объекты в памяти
                    a.store(inherit=False)
            continue
        keys = sorted(by_key, key=lambda k: -len(by_key[k]))
        log("")
        log("смешанный объект «%s» / %s: %d дорожек, %d альбомов по тегам -> разбираю"
            % (str(a.album)[:40], str(a.albumartist)[:20], len(its), len(by_key)))
        stats["split_objects"] += 1
        for n, k in enumerate(keys):
            grp = by_key[k]
            vals = album_values(grp)
            if n == 0:
                log("      остаётся в объекте: %3d x «%s»" % (len(grp), str(vals.get("album"))[:40]))
                if not DRY:
                    for f, v in vals.items():
                        a[f] = v
                    a.store(inherit=False)
                continue
            if len(grp) == 1:
                log("      одиночка:             «%s» — %s" % (str(grp[0].album)[:30], str(grp[0].title)[:30]))
                if not DRY:
                    journal(op="split_single", id=grp[0].id, path=path_of(grp[0]), before={"album_id": aid}, after={"album_id": None})
                    grp[0].album_id = None
                    grp[0].store()
                else:
                    SYNTH[grp[0].id] = "s%d" % grp[0].id
            else:
                log("      новый объект:         %3d x «%s»" % (len(grp), str(vals.get("album"))[:40]))
                if not DRY:
                    new = lib.add_album(grp)
                    journal(op="split_album", ids=[i.id for i in grp], before={"album_id": aid}, after={"album_id": new.id})
                else:
                    for it in grp:
                        SYNTH[it.id] = "m%s:%d" % (a.id, n)
            stats["split_groups"] += 1


# в сухом прогоне база не меняется, поэтому разбор смешанных объектов
# запоминаем здесь, чтобы дальнейшие шаги видели ту же картину, что и боевой
SYNTH = {}


def obj_key(it):
    if it.id in SYNTH:
        return SYNTH[it.id]
    return it.album_id if not it.singleton else "s%d" % it.id


def step_minor_albumartist(lib, groups, stats):
    """Одно название альбома, разные albumartist -> к хозяину большинства.

    step_va чинит только куски, помеченные Various Artists. Но на сборнике
    ремиксов дорожка может прийти под именем автора оригинала: «In My System
    (Skeler Remix)» лежала с albumartist=Devilish Trio, и в плеере релиз
    Skeler распадался надвое. albumartist описывает релиз, а не дорожку,
    поэтому исполнителя дорожки (artist) не трогаем.

    Условия жёсткие: у хозяина не меньше трёх дорожек и не меньше двух третей
    релиза, у меньшинства — не больше четверти. Настоящий сборник под этот
    порог не попадает, там большинства нет.

    Одного большинства мало: названия альбомов повторяются у разных артистов
    («No Love» есть и у Face, и у Eminem; «Феникс» — у Арии и у ЛСП), и по
    большинству такая дорожка уехала бы к чужому релизу. Поэтому меньшинство
    принимается, только если имя исполнителя родственно хозяйскому («Eminem»
    против «Eminem feat. Nate Dogg», «Endshpil» против «Эндшпиль») либо
    совпадает год и номер дорожки попадает в дыру в нумерации хозяина —
    признак того, что это кусок того же релиза.
    """
    by_album = collections.defaultdict(list)
    for (aa, al), items in groups.items():
        if al:
            by_album[al].append((aa, items))
    for al, parts in by_album.items():
        if len(parts) < 2:
            continue
        total = sum(len(its) for _, its in parts)
        parts.sort(key=lambda p: -len(p[1]))
        owner_aa, owner_items = parts[0]
        if is_va(owner_aa) or len(owner_items) < 3 or len(owner_items) * 3 < total * 2:
            continue
        owner_name = next((str(i.albumartist) for i in owner_items if str(i.albumartist or "").strip()), None)
        if not owner_name:
            continue
        owner_years = {int(i.year or 0) for i in owner_items if int(i.year or 0)}
        owner_slots = {(int(i.disc or 0), int(i.track or 0)) for i in owner_items}
        for aa, its in parts[1:]:
            if len(its) * 4 > total:
                continue
            kin = artist_ok(owner_name, str(its[0].albumartist or ""))
            if not kin:
                years = {int(i.year or 0) for i in its}
                slots = {(int(i.disc or 0), int(i.track or 0)) for i in its}
                same_year = bool(owner_years) and years and years <= owner_years
                free = not (slots & owner_slots) and all(t for _, t in slots)
                if not (same_year and free):
                    log("")
                    log("«%s»: %d дорожек под albumartist=%r — чужой релиз (%s), не трогаю"
                        % (str(its[0].album)[:40], len(its), str(its[0].albumartist),
                           "год не совпал" if not same_year else "номер дорожки занят"))
                    stats["minor_skipped"] += 1
                    continue
            log("")
            log("«%s»: %d дорожек под albumartist=%r, хозяин релиза %r (%d из %d) -> переношу"
                % (str(its[0].album)[:40], len(its), str(its[0].albumartist), owner_name, len(owner_items), total))
            for i in list(its):
                log("      %-34s (исполнитель %s остаётся)" % (str(i.title)[:34], str(i.artist)[:24]))
                if not DRY:
                    journal(op="minor_albumartist", id=i.id, path=path_of(i),
                            before={"albumartist": str(i.albumartist), "comp": i.comp},
                            after={"albumartist": owner_name, "comp": False})
                    i.albumartist = owner_name
                    i.comp = False
                    i.store()
                    try_write(i)
                groups[(owner_aa, al)].append(i)
                its.remove(i)
                stats["minor_aa"] += 1
            if not its:
                del groups[(aa, al)]


def step_va(lib, groups):
    """Обломки сборников -> к настоящему исполнителю."""
    moved = 0
    real_by_album = collections.defaultdict(list)
    for (aa, al), items in groups.items():
        if not is_va(aa) and al:
            real_by_album[al].append(aa)
    for (aa, al), items in list(groups.items()):
        if not is_va(aa) or al not in real_by_album:
            continue
        owners = real_by_album[al]
        if len(owners) != 1:
            continue
        owner = owners[0]
        owner_name = next((str(i.albumartist) for i in groups[(owner, al)] if str(i.albumartist or "").strip()), None)
        if not owner_name:
            continue
        fit = [i for i in items if artist_ok(owner_name, str(i.artist))]
        if not fit:
            continue
        log("VA-обломок «%s»: %d из %d дорожек -> %s" % (str(items[0].album)[:40], len(fit), len(items), owner_name))
        for i in fit:
            log("      %s — %s" % (str(i.artist)[:30], str(i.title)[:40]))
            if not DRY:
                journal(op="va", id=i.id, path=path_of(i),
                        before={"albumartist": str(i.albumartist), "comp": i.comp},
                        after={"albumartist": owner_name, "comp": False})
                i.albumartist = owner_name
                i.comp = False
                i.store()
                try_write(i)
            groups[(owner, al)].append(i)
            items.remove(i)
            moved += 1
        if not items:
            del groups[(aa, al)]
    return moved


def step_group(lib, key, items, stats):
    aa, al = key
    if len(items) < 2:
        return
    # объекты beets в группе: альбомы и одиночки
    objs = collections.defaultdict(list)
    for it in items:
        objs[obj_key(it)].append(it)

    # основное издание: из нормальной папки альбома (не из свалки), самое
    # полное, при равенстве — с лучшим битрейтом; одиночка основным быть не
    # может, пока есть хоть один альбом
    def obj_rank(k):
        its = objs[k]
        return (1 if str(k).startswith("s") else 0,
                1 if is_loose(path_of(its[0])) else 0,
                -len(its),
                -sum(int(i.bitrate or 0) for i in its) / max(1, len(its)))
    master_key = sorted(objs, key=obj_rank)[0]
    master_items = objs[master_key]
    master_dir = os.path.dirname(path_of(master_items[0]))
    master_is_album = not str(master_key).startswith("s")
    # если и основное издание лежит в свалке, переезды бессмысленны:
    # альбом в плеере собирается по тегам, а свалку ими только замусорим
    master_loose = is_loose(master_dir + "/")
    master_vals = album_values(master_items)

    header_done = [False]

    def header():
        if not header_done[0]:
            header_done[0] = True
            log("")
            log("== %s — %s   (%d дорожек, объектов %d, папка %s)"
                % (str(items[0].albumartist or items[0].artist)[:30], str(items[0].album)[:40],
                   len(items), len(objs), master_dir.replace(env.MUSIC_DIR + "/", "")))

    # --- дубли
    by_title = collections.defaultdict(list)
    for it in items:
        by_title[dedup.norm_title(it.title)].append(it)
    gone, stay = set(), set()
    for t, lst in by_title.items():
        if len(lst) < 2:
            continue
        # Сравниваем все пары отпечатками и собираем связные группы «одна и та
        # же запись». Мерить всех об одного нельзя: стоит в кластер попасть
        # другой версии с лучшим битрейтом, и настоящие дубли друг друга
        # разбегаются по 60% — так «The Monster» из четырёх копий не увидел
        # ни одной. Длина — только грубый фильтр пар.
        n = len(lst)
        parent = list(range(n))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        sims = {}
        for i in range(n):
            for j in range(i + 1, n):
                li, lj = float(lst[i].length or 0), float(lst[j].length or 0)
                if abs(li - lj) > LEN_TOL:
                    continue
                s = same_audio(path_of(lst[i]), path_of(lst[j]))
                sims[(i, j)] = s
                if s is None:
                    continue
                bar = FP_SAME_SHORT if min(li, lj) < SHORT else FP_SAME
                if abs(li - lj) > LEN_TIGHT:
                    bar = max(bar, FP_SAME_LENDIFF)
                if s >= bar:
                    parent[find(i)] = find(j)
        comps = collections.defaultdict(list)
        for i in range(n):
            comps[find(i)].append(i)
        groups_ = sorted(comps.values(), key=lambda g: -len(g))
        if len(groups_) == n:
            # все разные: скажем об этом, если пары были сравнимы по длине
            for (i, j), s in sims.items():
                if s is None:
                    continue
                li, lj = float(lst[i].length or 0), float(lst[j].length or 0)
                header()
                if s >= FP_MAYBE:
                    why, key = "ПОХОЖЕ, НО НЕ УВЕРЕН", "unsure"
                elif abs(li - lj) <= LEN_TIGHT:
                    why, key = "ОДИНАКОВЫЕ ТЕГИ, РАЗНЫЙ ЗВУК (подмена?)", "alien"
                else:
                    why, key = "разные версии", "versions"
                log("   «%s»: %s (%.0f%%): %s  <->  %s" % (str(lst[i].title)[:40], why, s * 100,
                    path_of(lst[i]).replace(env.MUSIC_DIR + "/", ""), path_of(lst[j]).replace(env.MUSIC_DIR + "/", "")))
                stats[key] += 1
                if key != "versions":
                    # спорную пару не двигаем и не сводим: пусть лежит, где лежала,
                    # а вопрос уходит в пульт (или уже решён там)
                    stay.add(lst[i].id)
                    stay.add(lst[j].id)
                    decided = ask_pair("substitution" if key == "alien" else "duplicate", lst[i], lst[j], s)
                    if decided:
                        log("      решено раньше: %s" % decided)
            continue
        for g in groups_:
            if len(g) < 2:
                continue
            g = sorted(g, key=lambda i: rank(lst[i], master_dir))
            keep = lst[g[0]]
            header()
            log("   дубль «%s»: остаётся [%s %d кбит] %s"
                % (str(keep.title)[:40], keep.format, int(keep.bitrate or 0) // 1000,
                   path_of(keep).replace(env.MUSIC_DIR + "/", "")))
            for idx in g[1:]:
                v = lst[idx]
                a, b = sorted((g[0], idx))
                s = sims.get((a, b))
                if s is None:
                    # напрямую с остающимся не сравнивались (связаны через
                    # третью копию) — сравним сейчас, для отчёта
                    s = same_audio(path_of(keep), path_of(v)) or 0.0
                quarantine(v, DUPES, "дубль %.0f%%" % (s * 100), reason="duplicate",
                           similarity=round(s, 3), kept=path_of(keep))
                gone.add(v.id)
                stats["dupes"] += 1
        # то, что осталось в одиночестве рядом с группой дублей: другая версия
        # или подмена — оставляем на месте и говорим об этом
        lone = [i for grp in groups_ if len(grp) == 1 for i in grp]
        if lone and any(len(grp) > 1 for grp in groups_):
            main_keep = min(groups_[0], key=lambda i: rank(lst[i], master_dir))
            for i in lone:
                a, b = sorted((main_keep, i))
                s = sims.get((a, b))
                li, lk = float(lst[i].length or 0), float(lst[main_keep].length or 0)
                if s is None:
                    why, key = "другая длина, не сравнивал", "versions"
                    s = 0.0
                elif s >= FP_MAYBE:
                    why, key = "ПОХОЖЕ, НО НЕ УВЕРЕН", "unsure"
                elif abs(li - lk) <= LEN_TIGHT:
                    why, key = "ОДИНАКОВЫЕ ТЕГИ, РАЗНЫЙ ЗВУК (подмена?)", "alien"
                else:
                    why, key = "разные версии", "versions"
                log("      %s (%.0f%%), оставляю на месте: %s" % (why, s * 100, path_of(lst[i]).replace(env.MUSIC_DIR + "/", "")))
                stats[key] += 1
                if key != "versions":
                    stay.add(lst[i].id)
                    decided = ask_pair("substitution" if key == "alien" else "duplicate",
                                       lst[main_keep], lst[i], s,
                                       hint="рядом есть копия, совпавшая с остальными")
                    if decided:
                        log("         решено раньше: %s" % decided)
    items = [i for i in items if i.id not in gone]
    # подмены и «не уверен» остаются где были и в слиянии не участвуют
    items = [i for i in items if i.id not in stay]

    # --- слияние изданий
    objs = collections.defaultdict(list)
    for it in items:
        objs[obj_key(it)].append(it)
    if len(objs) > 1 and master_is_album:
        header()
        others = [i for k, its in objs.items() if k != master_key for i in its]
        log("   слияние: %d дорожек из %d других объектов -> «%s» (%s, %s)"
            % (len(others), len(objs) - 1, str(master_vals.get("album"))[:40],
               master_vals.get("albumartist"), master_vals.get("year")))
        master_album = None
        if not DRY:
            master_album = lib.get_album(master_items[0])
            if master_album is None:
                log("   !! объект основного издания не найден, пропускаю")
                stats["errors"] += 1
                return
            for f, v in master_vals.items():
                master_album[f] = v
            master_album.store(inherit=False)
            # дорожки основного издания подравниваем сами: у меньшинства могло
            # разойтись написание, и в файл это должно уйти тоже
            for it in master_items:
                if it.id in gone or it.id in stay:
                    # ушёл в карантин или оставлен как спорный — не трогаем
                    continue
                if apply_vals(it, master_vals):
                    it.store()
                    try_write(it)
        for it in others:
            log("      + %s (%s, %s)" % (str(it.title)[:40],
                "одиночка" if str(obj_key(it)).startswith("s") else "издание %s" % (it.year or "?"),
                path_of(it).replace(env.MUSIC_DIR + "/", "")))
            if not DRY:
                journal(op="merge", id=it.id, path=path_of(it),
                        before={"album_id": it.album_id, "album": str(it.album),
                                "albumartist": str(it.albumartist), "year": it.year,
                                "mb_albumid": str(it.mb_albumid)},
                        after={"album_id": master_album.id, "album": str(master_vals.get("album")),
                               "albumartist": str(master_vals.get("albumartist")),
                               "year": master_vals.get("year"), "mb_albumid": str(master_vals.get("mb_albumid"))})
                it.album_id = master_album.id
                apply_vals(it, master_vals)
                it.store()
                try_write(it)
            if not master_loose:
                move_into(it, master_dir)
            stats["merged"] += 1
        if not DRY:
            for k in list(objs):
                if k == master_key or not isinstance(k, int):
                    continue
                a = lib.get_album(k)
                if a is not None and not list(a.items()):
                    a.remove(delete=False, with_items=False)
                    stats["albums_removed"] += 1
    elif len(objs) > 1:
        # одни одиночки с общим тегом альбома: в плеере это уже один альбом,
        # собирать из них объект — работа albumgroup.py
        stats["singles_only"] += 1

    # --- диски
    discs = {int(i.disc or 0) for i in items}
    if len(discs) > 1 and max(discs) <= 1:
        header()
        log("   диски: %s -> все 1" % sorted(discs))
        stats["discs"] += 1
        if not DRY:
            for it in items:
                if int(it.disc or 0) != 1 or int(it.disctotal or 0) != 1:
                    journal(op="disc", id=it.id, path=path_of(it),
                            before={"disc": it.disc, "disctotal": it.disctotal}, after={"disc": 1, "disctotal": 1})
                    it.disc, it.disctotal = 1, 1
                    it.store()
                    try_write(it)
    elif 0 in discs and len(discs) > 1:
        header()
        # один ненулевой номер — значит, это он и есть (второй диск делюкса,
        # у которого часть дорожек пришла без номера); несколько — берём 1
        nonzero = sorted(d for d in discs if d)
        target = nonzero[0] if len(nonzero) == 1 else 1
        log("   диски: %s -> нули в %d (многодисковый, проверить глазами)" % (sorted(discs), target))
        stats["discs_multi"] += 1
        if not DRY:
            for it in items:
                if int(it.disc or 0) == 0:
                    journal(op="disc", id=it.id, path=path_of(it),
                            before={"disc": it.disc}, after={"disc": target})
                    it.disc = target
                    it.store()
                    try_write(it)


AUDIO_EXT = (".mp3", ".flac", ".opus", ".m4a", ".ogg", ".wav", ".aac", ".wma")
JUNK_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".txt", ".nfo", ".cue", ".log",
            ".sfv", ".md5", ".url", ".ini", ".db", ".lrc")
KEEP_DIRS = ("_playlists",)


def step_empty_dirs(stats):
    """Папки, в которых после переездов не осталось звука: пустые — снести,
    с парой мусорных файлов (обложка, cue, лог) — в residue, как делает
    watch.sh с incoming. Плейлисты, сканы, буклеты и вообще всё, что больше
    двух файлов или не мусорного типа, не трогаем: residue чистится через
    три дня, а это уже потеря."""
    for root, dirs, files in os.walk(env.MUSIC_DIR, topdown=False):
        if root == env.MUSIC_DIR:
            continue
        rel = env.in_music(root)
        if any(part.lower() in KEEP_DIRS for part in rel.split("/")):
            continue
        has_audio = False
        for r2, d2, f2 in os.walk(root):
            if any(f.lower().endswith(AUDIO_EXT) for f in f2):
                has_audio = True
                break
        if has_audio:
            continue
        leftovers = [f for _, _, fs in os.walk(root) for f in fs]
        if leftovers:
            if len(leftovers) > 2 or not all(f.lower().endswith(JUNK_EXT) for f in leftovers):
                log("папка без звука, оставляю как есть (%d файлов: %s): %s"
                    % (len(leftovers), ", ".join(leftovers[:3])[:50], rel[:60]))
                stats["dirs_kept"] += 1
                continue
            log("папка без звука, остатки (%d) -> residue: %s" % (len(leftovers), rel[:70]))
            stats["dirs_residue"] += 1
            if not DRY:
                dst = os.path.join(env.RESIDUE_DIR, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if os.path.exists(dst):
                    dst = dst + ".%d" % int(os.path.getmtime(root))
                shutil.move(root, dst)
                journal(op="dir_residue", src=root, dst=dst, files=leftovers[:20])
        else:
            stats["dirs_removed"] += 1
            if not DRY:
                try:
                    os.rmdir(root)
                except OSError:
                    pass


def step_broken(lib, stats):
    for it in lib.items(""):
        p = path_of(it)
        if not os.path.isfile(p):
            continue
        if float(it.length or 0) < 1 and not playable(p):
            log("")
            log("битый (длительность 0): %s — %s" % (str(it.artist)[:30], str(it.title)[:40]))
            quarantine(it, BROKEN, "битый", reason="broken")
            stats["broken"] += 1


def load_thresholds():
    """Пороги из настроек пульта. Без базы — значения из кода выше."""
    global FP_SAME, FP_SAME_SHORT, FP_SAME_LENDIFF, FP_MAYBE, FP_ALIEN
    con = db()
    if con is None:
        return
    try:
        FP_SAME = FP_SAME_LENDIFF = float(jdb.setting(con, "fp_same"))
        FP_SAME_SHORT = float(jdb.setting(con, "fp_same_short"))
        FP_MAYBE = FP_ALIEN = float(jdb.setting(con, "fp_ask"))
    except (TypeError, ValueError):
        pass


def main():
    load_thresholds()
    lib = retry.open_library()
    stats = collections.Counter()
    items = [it for it in lib.items("") if os.path.isfile(path_of(it))]
    log("дорожек: %d%s" % (len(items), "   [СУХОЙ ПРОГОН]" if DRY else ""))
    step_sync_from_files(lib, items, stats)
    step_split_mixed(lib, items, stats)

    groups = collections.defaultdict(list)
    for it in items:
        k = key_of(it)
        if k[1]:
            groups[k].append(it)
    log("групп (исполнитель альбома + название): %d" % len(groups))
    stats["va"] = step_va(lib, groups)
    step_minor_albumartist(lib, groups, stats)
    for key in sorted(groups):
        try:
            step_group(lib, key, groups[key], stats)
        except Exception as e:
            log("!! ошибка в группе %s: %s" % (key, str(e)[:100]))
            stats["errors"] += 1
    step_broken(lib, stats)
    fix_playlists(stats)
    step_empty_dirs(stats)

    head = " | ".join([
        "база←файл: %d" % stats["synced"],
        "смешанных объектов разобрано: %d (на %d групп, переименовано %d)"
        % (stats["split_objects"], stats["split_groups"], stats["renamed"]),
        "VA-обломков возвращено: %d" % stats["va"],
        "меньшинству проставлен albumartist: %d (отклонено как чужой релиз: %d)"
        % (stats["minor_aa"], stats["minor_skipped"]),
        "дублей в карантин: %d" % stats["dupes"],
        "дорожек слито в основное издание: %d" % stats["merged"],
        "лишних альбомов снято: %d" % stats["albums_removed"],
        "дисков выровнено: %d (+%d многодисковых)" % (stats["discs"], stats["discs_multi"]),
        "битых: %d" % stats["broken"],
        "не уверен (оставил): %d" % stats["unsure"],
        "одинаковые теги, разный звук: %d" % stats["alien"],
        "разных версий: %d" % stats["versions"],
        "групп из одних одиночек: %d" % stats["singles_only"],
        "пустых папок снесено: %d, с остатками в residue: %d, оставлено: %d"
        % (stats["dirs_removed"], stats["dirs_residue"], stats["dirs_kept"]),
        "плейлистов поправлено: %d" % stats["playlists_fixed"],
        "ошибок: %d" % stats["errors"],
    ])
    print(head)
    with open(REPORT + (".dry" if DRY else ""), "w", encoding="utf-8") as f:
        f.write(head + "\n" + "\n".join(lines) + "\n")
    # счётчики для сводки пульта: nightly.sh подхватит их в шаг прогона
    stats_json = os.environ.get("JANITOR_STATS_OUT")
    if stats_json and not DRY:
        with open(stats_json, "w", encoding="utf-8") as f:
            json.dump(dict(stats), f, ensure_ascii=False)


if __name__ == "__main__":
    main()
