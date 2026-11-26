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

3. Исполнитель под чужим именем. Скачанное по пульту приходит с именем из
   Spotify: «Agatha Christie», «Kino», «GUF», когда в фонотеке и в пульте
   есть «Агата Кристи», «Кино», «Гуф». Navidrome заводит второго
   исполнителя и делит альбомы. Все имена фонотеки и пульта собираются в
   группы «одно и то же имя» (транслит, регистр, ё; латиница с кириллицей —
   ещё и по сходству длинных имён), у группы одно каноническое имя:
   кириллица, затем настоящий MusicBrainz ID, затем имя из пульта, затем
   самое частое. Группа без кириллицы не трогается. Группа с двумя разными MusicBrainz ID — разные люди, её не
   трогают. Правится каждое имя в artist, albumartist, artists и
   albumartists по всей фонотеке, гости с другими именами остаются. Старых
   дорожек за раз — не больше MAX_OLD_NAMES, иначе только свежие (массовая
   правка — повод посмотреть --dry). Значение многозначного тега «A, B»
   делится на «A» и «B», если оба — известные отдельные имена. Свежая дорожка, чьё имя группой не
   распознано, получает имя исполнителя скачанного релиза (по id Spotify или
   альбому + названию), если оно похоже в латинице.

4. Дата не как у остального альбома. Navidrome собирает альбом с учётом
   даты: старые дорожки «Избранного» с датой 2008, докачанные — с
   2008-01-01, и альбом показывался трижды. Свежая дорожка получает дату,
   которая у большинства уже лежащих дорожек этого альбома, — если год тот
   же. Другой год не трогается: это уже не запись даты, а спорные данные.

Трогает только дорожки, добавленные с прошлого запуска (отметка в общей
базе); каждое изменение пишется в журнал и откатывается из пульта.

  postimport.py [--dry] [--days N] [--names-all]
      --days: взять добавленное за N дней; --names-all: править имена
      старых дорожек без предела MAX_OLD_NAMES
"""
import collections
import difflib
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import discography as disco  # noqa: E402
import janitordb as jdb  # noqa: E402
from retry import artist_ok  # noqa: E402

DRY = "--dry" in sys.argv
NAMES_ALL = "--names-all" in sys.argv   # снять предел MAX_OLD_NAMES (разовая массовая правка)
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

    fixed_name = canonical_names(con, items, fresh, log)
    fixed_album_date = align_dates(items, fresh, log)

    if con is not None:
        jdb.meta_set(con, "postimport_seen_until", max((float(i.added or 0) for i in items), default=time.time()))
    print("свежих дорожек: %d | исполнитель альбома поправлен: %d (сборников оставлено: %d) | исходная дата: %d"
          " | имя исполнителя как в фонотеке: %d | дата как у альбома: %d"
          % (len(fresh), fixed_aa, kept, fixed_date, fixed_name, fixed_album_date))
    stats_out = os.environ.get("JANITOR_STATS_OUT")
    if stats_out and not DRY:
        with open(stats_out, "w", encoding="utf-8") as f:
            json.dump({"fresh": len(fresh), "albumartist": fixed_aa, "original_date": fixed_date,
                       "artist_name": fixed_name, "album_date": fixed_album_date}, f)


def release_artists(con):
    """Чьи релизы скачивали: id Spotify дорожки -> имя исполнителя и
    (альбом, название) -> имя. Имя — как исполнитель заведён в пульте."""
    by_id, by_title = {}, {}
    if con is None:
        con = jdb.connect()
    for r in con.execute("SELECT rt.title, rt.info, rl.title AS album, a.name FROM release_tracks rt "
                         "JOIN releases rl ON rl.id = rt.release_id JOIN artists a ON a.id = rl.artist_id "
                         "WHERE rl.status IN ('queued', 'downloading', 'in_library', 'failed')"):
        info = jdb.loads(r["info"], {}) or {}
        if info.get("spotify_id"):
            by_id[info["spotify_id"]] = r["name"]
        by_title[(disco.norm_title(r["album"]), disco.norm_title(r["title"]))] = r["name"]
    return by_id, by_title


def renamed(names, old, new):
    return [new if disco.name_keys(n) & disco.name_keys(old) else n for n in names]


def translit_ratio(a, b):
    """Насколько два имени похожи в латинице: «agatha christie» и «агата
    кристи» (agata kristi) — 0.85, «vadim samoylov» и «агата кристи» — 0.3."""
    ka, kb = disco.name_keys(a), disco.name_keys(b)
    return max((difflib.SequenceMatcher(None, x, y).ratio() for x in ka for y in kb), default=0.0)


SAME_NAME = 0.7     # от такого сходства в латинице имена — одно и то же имя
SAME_SKELETON = 0.8  # латиница и кириллица, длинные скелеты: «Agatha Christie» = «Агата Кристи»
LONG_SKELETON = 8    # короче — только точное совпадение скелета: Kino ≠ Kasta, Queen ≠ Квин
MAX_OLD_NAMES = 300  # старых дорожек за прогон; больше — подозрительно, только свежие
MBID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
CYR = re.compile("[а-яё]", re.I)
# латинские написания одного звука; после транслита кириллицы и разные
# транслитерации Spotify сходятся: Tsoi/Цой -> coi, Smoky/Смоки -> smoki
SKEL = (("sch", "sh"), ("ts", "c"), ("kh", "h"), ("ck", "k"), ("ph", "f"),
        ("w", "v"), ("x", "ks"), ("q", "k"), ("j", "i"), ("y", "i"))
NAME_FIELDS = ("artist", "albumartist", "artists", "albumartists")


def skeleton(s):
    """«Viktor Tsoi», «Виктор Цой» -> viktorcoi; «ТЯЖЁЛАЯ АТЛЕТИКА» и
    «Тяжелая Атлетика» -> одно и то же."""
    n = disco.norm_name(s).translate(disco.TRANSLIT)
    for a, b in SKEL:
        n = n.replace(a, b)
    n = re.sub(r"[^a-z0-9]", "", n)
    return re.sub(r"(.)\1+", r"\1", n)


def canonical_map(items, con):
    """Имя исполнителя -> каноническое написание. В словаре только имена,
    которые надо менять."""
    count, mbids, panel = collections.Counter(), collections.defaultdict(set), set()
    for i in items:
        for field, idf in (("artist", "mb_artistid"), ("albumartist", "mb_albumartistid")):
            names = [n for n in disco.artist_names(str(i.get(field) or "")) if not is_va(n)]
            for n in names:
                count[n] += 1
            mb = str(i.get(idf) or "").strip()
            if len(names) == 1 and MBID.match(mb):
                mbids[names[0]].add(mb)
    for r in con.execute("SELECT name, deezer_name FROM artists"):
        for n in (r["name"], r["deezer_name"]):
            n = str(n or "").strip()
            if n and not is_va(n):
                panel.add(n)
                count[n] += 0

    groups = collections.defaultdict(set)
    for n in count:
        k = skeleton(n)
        if k:
            groups[k].add(n)
    # латиница <-> кириллица по сходству — только длинные скелеты
    parent = {k: k for k in groups}

    def root(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    def has_cyr(k):
        return any(CYR.search(n) for n in groups[k])

    lat = collections.defaultdict(list)
    for k in groups:
        if len(k) >= LONG_SKELETON and not has_cyr(k):
            lat[k[0]].append(k)
    for k in groups:
        if len(k) < LONG_SKELETON or not has_cyr(k):
            continue
        for other in lat.get(k[0], ()):
            m = difflib.SequenceMatcher(None, k, other)
            if m.real_quick_ratio() >= SAME_SKELETON and m.quick_ratio() >= SAME_SKELETON \
                    and m.ratio() >= SAME_SKELETON:
                parent[root(other)] = root(k)
    merged = collections.defaultdict(set)
    for k, names in groups.items():
        merged[root(k)] |= names

    out = {}
    for names in merged.values():
        if len({n.lower() for n in names}) < 2:
            continue      # только регистр: Navidrome и так считает одним (ё он различает)
        ids = set().union(*(mbids.get(n, set()) for n in names))
        if len(ids) > 1:
            print("   имена %s: разные MusicBrainz ID, не склеиваю" % " / ".join(sorted(names)))
            continue
        # в канон — только чистое кириллическое имя: «. Немига» из чужого
        # тега не годится, а варианты латиницы («skeler.», «L’One») — кто из
        # них верный, не угадать
        clean = [n for n in names if CYR.search(n) and not re.match(r"[\W_]", n)]
        if not clean:
            continue
        best = max(clean, key=lambda n: (bool(mbids.get(n)), n in panel, count[n], n))
        for n in names:
            if n.lower() != best.lower() or \
                    (n != best and CYR.search(n)):
                out[n] = best
    return out


def rename_in(s, cmap):
    """Заменить в строке исполнителей каждое имя из словаря, разделители
    и гостей оставить как есть."""
    s = str(s or "")
    for n in disco.artist_names(s):
        new = cmap.get(n)
        if new:
            s = re.sub(r"(?<!\w)%s(?!\w)" % re.escape(n), lambda _m, v=new: v, s, count=1)
    return s


def known_names(items, con):
    """Имена, которые встречаются сами по себе: целым исполнителем дорожки
    или альбома, отдельным элементом списка, исполнителем пульта."""
    known = set()
    for i in items:
        for f in ("artist", "albumartist"):
            known.add(str(i.get(f) or "").strip())
        for f in ("artists", "albumartists"):
            known.update(str(v).strip() for v in (i.get(f) or []))
    known.update(str(r[0]).strip() for r in con.execute("SELECT name FROM artists"))
    known.discard("")
    return known


JOINED = re.compile(r"\s*[,;]\s*")


def split_joined(values, cmap, known):
    """Многозначный тег, где downtify положил «Баста, Гуф» одним значением:
    Navidrome заводит из него отдельного исполнителя «Баста, Гуф». Значение
    делится, только если каждая часть — известное отдельное имя («Tyler, The
    Creator» не делится). «&» не трогается: «Bob Marley & The Wailers» и
    «Miyagi & Эндшпиль» — сами по себе исполнители."""
    out, split = [], False
    for v in values:
        v = cmap.get(v, v)
        parts = [cmap.get(x, x) for x in JOINED.split(v) if x.strip()]
        if len(parts) > 1 and all(x in known for x in parts):
            out.extend(parts)
            split = True
        else:
            out.append(v)
    return list(dict.fromkeys(out)) if split else out


def canonical_names(con, items, fresh, log):
    if con is None:
        con = jdb.connect()
    cmap = canonical_map(items, con)
    known = known_names(items, con)
    known.update(cmap.values())
    fresh_ids = {i.id for i in fresh}
    plan = []
    for i in items:
        before = {f: i.get(f) for f in NAME_FIELDS}
        after = {"artist": rename_in(i.artist, cmap), "albumartist": rename_in(i.albumartist, cmap)}
        for f in ("artists", "albumartists"):
            vals = list(i.get(f) or [])
            if vals:
                after[f] = split_joined(vals, cmap, known)
            else:
                # списка нет — Navidrome берёт строку целиком, а запятую
                # разделителем не считает: «Баста, Гуф» становится исполнителем
                parts = split_joined([after[f[:-1]]], cmap, known)
                after[f] = parts if len(parts) > 1 else []
        if any(after[f] != (list(before[f] or []) if f.endswith("s") else str(before[f] or "")) for f in NAME_FIELDS):
            plan.append((i, before, after))
    old = [p for p in plan if p[0].id not in fresh_ids]
    if len(old) > MAX_OLD_NAMES and not NAMES_ALL and not DRY:
        print("   !! имя исполнителя: %d старых дорожек к правке — больше %d, правлю только свежие; "
              "посмотрите postimport.py --dry, применить — --names-all" % (len(old), MAX_OLD_NAMES))
        plan = [p for p in plan if p[0].id in fresh_ids]
    seen = collections.Counter()
    for i, before, after in plan:
        f = next(f for f in NAME_FIELDS if after[f] != (list(before[f] or []) if f.endswith("s") else str(before[f] or "")))
        seen["%s: %s -> %s" % (f, before[f] if not f.endswith("s") else list(before[f] or []), after[f])] += 1
        if not DRY:
            for f, v in after.items():
                i[f] = v
            i.store()
            i.try_write()
            log(i, "artist", before, dict(after, why="имя исполнителя как в фонотеке"))
    for k, n in seen.most_common():
        print("   %4d  %s" % (n, k))
    fixed = len(plan)

    # свежее, чьё имя группами не распознано: имя исполнителя скачанного
    # релиза, если оно похоже в латинице (и само приведено к канону)
    by_id, by_title = release_artists(con)
    for i in fresh:
        name = by_id.get(str(i.get("spotify_track_id") or "")) or \
            by_title.get((disco.norm_title(i.album), disco.norm_title(i.title)))
        name = cmap.get(name, name)
        names = disco.artist_names(str(i.artist))
        if not name or not names or any(disco.name_keys(n) & disco.name_keys(name) for n in names):
            continue
        # заменяется только имя, которое и есть исполнитель релиза в другом
        # написании; у «Вадим Самойлов, Agatha Christie» — второе, не первое
        old, ratio = max(((n, translit_ratio(n, name)) for n in names), key=lambda x: x[1])
        if ratio < SAME_NAME:
            continue
        before = {f: i.get(f) for f in NAME_FIELDS}
        after = {"artist": str(i.artist).replace(old, name, 1),
                 "albumartist": name if disco.name_keys(str(i.albumartist)) & disco.name_keys(old) else i.albumartist,
                 "artists": renamed(list(i.get("artists") or []), old, name),
                 "albumartists": renamed(list(i.get("albumartists") or []), old, name)}
        print("   %s — %s: исполнитель %s -> %s" % (str(i.artist)[:30], str(i.title)[:30], old, name))
        fixed += 1
        if DRY:
            continue
        for f, v in after.items():
            i[f] = v
        i.store()
        i.try_write()
        log(i, "artist", before, dict(after, why="имя исполнителя как в пульте"))
    return fixed


def align_dates(items, fresh, log):
    fresh_ids = {i.id for i in fresh}
    dates = collections.defaultdict(collections.Counter)
    for i in items:
        if i.id not in fresh_ids and str(i.album or "").strip():
            key = (str(i.albumartist or "").strip().lower(), str(i.album).strip().lower())
            dates[key][(int(i.year or 0), int(i.month or 0), int(i.day or 0))] += 1
    fixed = 0
    for i in fresh:
        key = (str(i.albumartist or "").strip().lower(), str(i.album or "").strip().lower())
        if not dates.get(key):
            continue
        (y, m, d), n = dates[key].most_common(1)[0]
        mine = (int(i.year or 0), int(i.month or 0), int(i.day or 0))
        # только запись даты внутри того же года («2008» против
        # «2008-01-01»); другой год — не формат, а спорные данные, их не трогаем
        if not y or mine == (y, m, d) or mine[0] != y:
            continue
        print("   %s — %s: дата %s -> %s, как у альбома" % (str(i.artist)[:30], str(i.title)[:30],
                                                         "-".join(map(str, mine)), "-".join(map(str, (y, m, d)))))
        fixed += 1
        if DRY:
            continue
        before = {"year": i.year, "month": i.month, "day": i.day, "original_year": i.original_year,
                  "original_month": i.original_month, "original_day": i.original_day}
        i.year, i.month, i.day = y, m, d
        i.original_year, i.original_month, i.original_day = y, m, d
        i.store()
        i.try_write()
        log(i, "album_date", before, {"year": y, "month": m, "day": d, "why": "дата как у альбома"})
    return fixed


if __name__ == "__main__":
    main()
