#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Новинки исполнителей.

Раз в сутки у каждого отслеживаемого исполнителя смотрим список релизов в
Deezer. Что появилось с прошлого раза и вышло недавно, разбираем против
фонотеки (discography.py) и кладём в «Новинки» пульта. При auto_download=1
то, что решено скачать, сразу уходит в очередь на скачивание.

Список исполнителей собирается сам — из фонотеки, у кого не меньше
follow_min_tracks дорожек, — плюс добавленные в пульте вручную.
Исключённые в пульте пропускаются.

Кто есть кто в Deezer, решает пересечение с фонотекой: поиск по имени там
путается. Если однозначного кандидата нет, исполнитель помечается
«уточнить», и выбор делается в пульте.

При первой проверке исполнителя его прежние релизы только запоминаются, а
не объявляются новинками: иначе в «Новинки» разом высыпалась бы вся
дискография. Недостающее из старого — это страница «Дискография».

  follow.py [--dry] [--artist ИМЯ]
  follow.py --arrivals     только отметить скачанные релизы, доехавшие до фонотеки
"""
import datetime
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import discography as disco  # noqa: E402
import janitordb as jdb  # noqa: E402

DRY = "--dry" in sys.argv
RESOLVE_PER_RUN = 40      # столько новых исполнителей опознаём в Deezer за ночь
NEW_DAYS = 60             # релиз старше — не новинка, а поздно добавленный старый
VA = {"various artists", "various", "va", "сборник", "разные исполнители", "v a"}


def owner(it):
    """Чей это трек: исполнитель альбома, если он не «сборник», иначе исполнитель трека."""
    aa = it["albumartist"]
    if aa and disco.norm_name(aa) not in VA:
        return disco.primary_artist(aa)
    return disco.primary_artist(it["artist"])


def sync_artists(con, lib, min_tracks):
    """Исполнители из фонотеки: новых заводим, у известных обновляем счётчик."""
    count, spelled = Counter(), defaultdict(Counter)
    for it in lib.items:
        name = owner(it)
        k = disco.norm_name(name)
        if not k or k in VA:
            continue
        count[k] += 1
        spelled[k][name] += 1
    existing = {}
    for r in con.execute("SELECT id, name FROM artists"):
        existing.setdefault(disco.norm_name(r["name"]), r["id"])
    added = 0
    for k, n in count.items():
        if k in existing:
            if not DRY:
                con.execute("UPDATE artists SET library_tracks=? WHERE id=?", (n, existing[k]))
        elif n >= min_tracks:
            added += 1
            if not DRY:
                con.execute("INSERT INTO artists(name, source, library_tracks, created_at) VALUES (?, 'auto', ?, ?)",
                            (spelled[k].most_common(1)[0][0], n, jdb.now()))
    return added


def resolve_ids(con, lib, only=None):
    """Опознать исполнителей в Deezer. Неоднозначных — пометить для пульта."""
    q = ("SELECT * FROM artists WHERE deezer_id IS NULL AND excluded=0 AND follow=1 AND attention IS NULL "
         "ORDER BY library_tracks DESC")
    rows = con.execute(q).fetchall()
    if only:
        rows = [r for r in rows if disco.name_keys(r["name"]) & disco.name_keys(only)]
    rows = rows[:RESOLVE_PER_RUN]
    done = unclear = 0
    for a in rows:
        cands = disco.search_artists(a["name"], lib)
        best = disco.pick_artist(cands)
        if best is not None:
            taken = con.execute("SELECT name FROM artists WHERE deezer_id=?", (str(best["id"]),)).fetchone()
            if taken is not None:
                # «ЛСП» и «LSP» — один исполнитель, записанный в фонотеке по-разному
                attention, info = "тот же исполнитель, что и «%s»" % taken["name"], None
            else:
                print("   = %-28s -> Deezer %s «%s» (пересечение %d)"
                      % (a["name"][:28], best["id"], best["name"], best["overlap"]))
                if not DRY:
                    con.execute("UPDATE artists SET deezer_id=?, deezer_name=?, picture=?, info=NULL WHERE id=?",
                                (str(best["id"]), best["name"], best["picture"], a["id"]))
                done += 1
                continue
        elif not cands:
            attention, info = "нет в Deezer", None
        else:
            exact = [c for c in cands if c["exact"]]
            if not exact:
                attention = "в Deezer не нашёлся, есть похожие"
            elif not any(c["overlap"] for c in exact):
                attention = "в Deezer тёзка без общих треков"
            else:
                attention = "несколько кандидатов"
            info = json.dumps(cands[:5], ensure_ascii=False)
        print("   ? %-28s %s" % (a["name"][:28], attention))
        unclear += 1
        if not DRY:
            con.execute("UPDATE artists SET attention=?, info=? WHERE id=?", (attention, info, a["id"]))
    return done, unclear


def check(con, a, lib, ok_t, auto):
    """Релизы одного исполнителя. Вернуть (новинок, в очередь)."""
    rels = disco.releases_of(a["deezer_id"])
    if not rels:
        return 0, 0
    known = {r["provider_id"] for r in con.execute(
        "SELECT provider_id FROM releases WHERE provider='deezer' AND artist_id=?", (a["id"],))}
    fresh = [r for r in rels if str(r["id"]) not in known]
    latest = max((r.get("release_date") or "") for r in rels)
    days = int(jdb.setting(con, "follow_new_days", NEW_DAYS))
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    first = a["last_checked"] is None
    new = [] if first else [r for r in fresh if (r.get("release_date") or "") >= cutoff]
    old = [r for r in fresh if r not in new]

    queued = 0
    if not DRY:
        disco.store(con, a["id"], [disco.release_row(r) for r in old], "known")
    if new:
        resolved = disco.resolve(a["deezer_id"], a["deezer_name"] or a["name"], lib, ok_t=ok_t, releases=new)
        for r in resolved:
            c = r["counts"]
            print("   + %-24s %-10s %-34s есть %d, версий %d, спросить %d, скачать %d"
                  % (a["name"][:24], r["release_date"], str(r["title"])[:34], c.get("have", 0) + c.get("dup", 0),
                     c.get("version", 0), c.get("ask", 0), c.get("get", 0)))
        if not DRY:
            ids = disco.store(con, a["id"], resolved,
                              lambda r: "in_library" if not r["counts"].get("get") and not r["counts"].get("ask")
                              else "new")
            if auto:
                for r in resolved:
                    if r["counts"].get("get"):
                        jdb.enqueue(con, "download", {"release_id": ids[r["provider_id"]]})
                        con.execute("UPDATE releases SET status='queued' WHERE id=?", (ids[r["provider_id"]],))
                        queued += 1
    if not DRY:
        con.execute("UPDATE artists SET last_checked=?, last_release_date=? WHERE id=?",
                    (jdb.now(), latest or None, a["id"]))
    return len(new), queued


def recheck_downloads(con, lib):
    """Скачанное доехало до фонотеки? Тогда релиз переходит в «в фонотеке».

    Ищем по ISRC, а если его нет в тегах — по названию и длине у того же
    исполнителя: downtify не всегда пишет ISRC.
    """
    moved = 0
    for rel in con.execute("SELECT r.*, a.name AS artist FROM releases r LEFT JOIN artists a ON a.id=r.artist_id "
                           "WHERE r.status IN ('queued', 'downloading')").fetchall():
        rows = [t for t in con.execute("SELECT * FROM release_tracks WHERE release_id=?", (rel["id"],))
                if disco.wanted(t)]
        mine = defaultdict(list)
        for it in lib.of_artist(rel["artist"] or ""):
            mine[disco.norm_title(it["title"])].append(it["length"])

        def arrived(t):
            if t["isrc"] and t["isrc"] in lib.by_isrc:
                return True
            return any(abs(L - (t["duration"] or 0)) <= disco.LEN_SAME for L in mine.get(disco.norm_title(t["title"]), []))

        if rows and all(arrived(t) for t in rows):
            moved += 1
            if not DRY:
                con.execute("UPDATE releases SET status='in_library' WHERE id=?", (rel["id"],))
    return moved


def main():
    only = None
    if "--artist" in sys.argv:
        only = sys.argv[sys.argv.index("--artist") + 1]
    con = jdb.connect()
    if "--arrivals" in sys.argv:
        # только отметить доехавшее: сторож зовёт это сразу после разбора
        # incoming, чтобы скачанный релиз не висел «качается» до ночи
        arrived = recheck_downloads(con, disco.Library())
        print("доехало до фонотеки релизов: %d" % arrived)
        return
    if jdb.setting(con, "follow_enabled") != "1" and not only:
        print("слежение выключено в настройках")
        return
    lib = disco.Library()
    ok_t = float(jdb.setting(con, "ref_ok"))
    auto = jdb.setting(con, "auto_download") == "1"

    added = sync_artists(con, lib, int(jdb.setting(con, "follow_min_tracks")))
    print("исполнителей из фонотеки добавлено: %d" % added)
    done, unclear = resolve_ids(con, lib, only)
    print("опознано в Deezer: %d, требуют выбора в пульте: %d" % (done, unclear))

    rows = con.execute("SELECT * FROM artists WHERE deezer_id IS NOT NULL AND follow=1 AND excluded=0 "
                       "ORDER BY last_checked IS NOT NULL, last_checked").fetchall()
    if only:
        rows = [r for r in rows if disco.name_keys(r["name"]) & disco.name_keys(only)]
    new = queued = firsts = 0
    for a in rows:
        if a["last_checked"] is None:
            firsts += 1
        try:
            n, q = check(con, a, lib, ok_t, auto)
        except Exception as e:
            print("   !! %s: %s" % (a["name"], str(e)[:80]))
            continue
        new += n
        queued += q
    arrived = recheck_downloads(con, lib)
    print("проверено исполнителей: %d (впервые, только запомнить релизы: %d) | новинок: %d | "
          "в очередь на скачивание: %d | доехало до фонотеки: %d" % (len(rows), firsts, new, queued, arrived))

    stats_out = os.environ.get("JANITOR_STATS_OUT")
    if stats_out and not DRY:
        with open(stats_out, "w", encoding="utf-8") as f:
            json.dump({"artists": len(rows), "added": added, "resolved": done, "unclear": unclear,
                       "new": new, "queued": queued, "arrived": arrived}, f)


if __name__ == "__main__":
    main()
