#!/usr/bin/env python3
"""Дедуп в два прохода с проверкой по акустическому отпечатку.

Проход 1 — по mb_trackid. Одинаковый идентификатор = источник считает это
одной записью, поэтому разница в написании названия ничего не значит
("My Band" и "My Band (album version)").

Проход 2 — по связке исполнитель + название. Нужен потому, что копии одного
трека приходят из разных источников и получают разные идентификаторы: у
MusicBrainz это UUID, у Spotify свой код. Для первого прохода такая пара
невидима, хотя это очевидный дубль. Сюда же попадают треки без идентификатора.

Перед тем как убрать копию, сверяем **акустический отпечаток**. Названия и
длительности врут: "Scary Movies (Yonderboi remix)" и "Scary Movies (Future
Type Joint remix)" отличаются одной скобкой, а одна и та же запись может
разойтись на три секунды из-за кодирования. Отпечаток отвечает однозначно,
поэтому он и решает, а не эвристика по строкам.

Что не трогаем:
  разные альбомы            -> одна запись на двух релизах
  длительность разошлась    -> признак ложного матча, только в отчёт
  отпечатки не совпали      -> разные версии, обе остаются
  второе издание альбома    -> разбирать по дорожкам нельзя

Выбор копии: битрейт, затем нормальная папка альбома против Non-Album,
затем размер файла.
"""
import collections
import os
import re
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import env  # noqa: E402

DRY = "--dry" in sys.argv
DUPES = env.DUPES_DIR
REPORT = env.DUPES_LOG
SEP = "\x01"
LEN_TOLERANCE = 20
LEN_NAME = 15
FPCALC = env.FPCALC
FP_SECONDS = 120
FP_MIN = 0.90
# Для явно худшей копии, лежащей отдельным файлом вне альбома, планку опускаем:
# сильное сжатие само по себе портит отпечаток, и 128 kbps из свалки рядом с
# 320 kbps из альбома — это почти наверняка тот же трек. Цена ошибки мала,
# копия уезжает в карантин, откуда её можно вернуть.
FP_LOOSE = 0.80
LOOSE_BR_RATIO = 0.6

# папки, где лежат отдельные файлы, а не собранные альбомы
LOOSE = env.LOOSE_DIRS


def norm(s):
    s = (s or "").lower()
    s = re.sub(r"[\[\(].*?[\]\)]", " ", s)
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    return " ".join(s.split())


def norm_strict(s):
    """То же, но со скобками: для группировки по названию они значимы."""
    s = re.sub(r"[^\w\s]", " ", (s or "").lower(), flags=re.UNICODE)
    return " ".join(s.split())


FEAT = re.compile(r"[\(\[]?\s*\b(?:feat|ft|featuring)\b\.?\s+[^\)\]]*[\)\]]?",
                  re.IGNORECASE)


def norm_title(s):
    """Название без указания приглашённых.

    Скобки в названии значимы — они отличают ремикс от оригинала и
    инструментал от вокала. Но "(feat. ...)" версию не меняет, это то же
    исполнение с другой подписью авторства: "Wasted" и
    "Wasted (feat. Lil Uzi Vert)" — один трек, а по разным ключам они
    никогда не встречались и дублями не признавались.
    """
    return norm_strict(FEAT.sub(" ", str(s or "")))


def fake_album(i):
    """Альбом-пустышка: матч на сингловый релиз, где название альбома совпало
    с названием трека.

    Одного совпадения названий мало: на нормальном альбоме есть заглавная
    песня, и `Master Of Puppets` на альбоме `Master of Puppets` — это она, а
    не пустышка. Поэтому дополнительно требуем, чтобы в альбоме была от силы
    пара дорожек. Без этого условия проверка разрешала выдёргивать заглавные
    песни из полноценных изданий.
    """
    a, t = norm_strict(i["album"]), norm_strict(i["title"])
    return bool(a) and a == t and i.get("small", True)


def secs(v):
    try:
        parts = [int(x) for x in v.split(":")]
    except Exception:
        return -1
    out = 0
    for p in parts:
        out = out * 60 + p
    return out


_fp_cache = {}


def fingerprint(path):
    if path in _fp_cache:
        return _fp_cache[path]
    out = None
    try:
        r = subprocess.run([FPCALC, "-raw", "-length", str(FP_SECONDS), path],
                           capture_output=True, text=True, timeout=90)
        for line in r.stdout.splitlines():
            if line.startswith("FINGERPRINT="):
                out = [int(x) for x in line.split("=", 1)[1].split(",")]
                break
    except Exception:
        out = None
    _fp_cache[path] = out
    return out


def same_audio(a, b):
    """Доля совпадающих бит отпечатка. None — сравнить не удалось."""
    fa, fb = fingerprint(a), fingerprint(b)
    if not fa or not fb:
        return None
    best = 0.0
    for off in range(-12, 13):
        bits = pairs = 0
        for i, va in enumerate(fa):
            j = i + off
            if 0 <= j < len(fb):
                bits += bin(va ^ fb[j]).count("1")
                pairs += 1
        if pairs > 40:
            sim = 1.0 - bits / (pairs * 32.0)
            if sim > best:
                best = sim
    return best


def items():
    fmt = SEP.join(["$id", "$mb_trackid", "$album", "$title", "$bitrate",
                    "$length", "$path", "$artist", "$albumartist", "$album_id"])
    out = subprocess.run(["beet", "ls", "-f", fmt], capture_output=True, text=True).stdout
    for line in out.splitlines():
        p = line.split(SEP)
        if len(p) != 10:
            continue
        mbid = "" if (not p[1] or p[1].startswith("$")) else p[1]
        try:
            br = int(p[4].replace("kbps", "").strip())
        except Exception:
            br = 0
        try:
            size = os.path.getsize(p[6])
        except Exception:
            size = 0
        who = p[8] if p[8] and not p[8].startswith("$") else p[7]
        alb = p[9] if p[9] and not p[9].startswith("$") else ""
        yield {"id": p[0], "mbid": mbid, "album": p[2], "title": p[3],
               "br": br, "len": secs(p[5]), "size": size, "path": p[6],
               "who": who, "album_id": alb}


def rank(i):
    # копия из свалки уступает копии из папки альбома
    non_album = 1 if any(j in i["path"] for j in LOOSE) else 0
    return (-i["br"], non_album, -i["size"])


_con = None


def db():
    global _con
    if _con is None:
        import janitordb
        _con = janitordb.connect()
    return _con


def quarantine(it, sim=None, kept=None):
    p = it["path"]
    rel = os.path.relpath(p, env.MUSIC_DIR) if p.startswith(env.MUSIC_DIR + "/") else p.lstrip("/")
    dst = os.path.join(DUPES, rel)
    if DRY:
        return "БЫ УБРАЛ [%s] %s" % (it["br"], p)
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(p, dst)
        # чистка карантина смотрит на дату файла, а перемещение её сохраняет:
        # без этой отметки старая запись удалялась бы почти сразу, а не через
        # положенные две недели
        os.utime(dst, None)
    except Exception as e:
        return "ОШИБКА: %s" % e
    subprocess.run(["beet", "remove", "-f", "id:%s" % it["id"]], capture_output=True, text=True)
    try:
        import janitordb
        janitordb.quarantine_add(db(), dst, p, "duplicate", similarity=round(sim, 3) if sim else None,
                                 kept_path=kept, title=it.get("title"), artist=it.get("who"),
                                 # 0 — одиночка: при возврате альбом ей не подбирать
                                 album_id=int(it["album_id"]) if str(it.get("album_id") or "").isdigit() else 0)
        janitordb.log_event(db(), "dedup", "quarantine", {"path": p},
                            {"path": dst, "why": "дубль %.0f%%" % (sim * 100) if sim else "дубль"},
                            item_id=int(it["id"]) if str(it["id"]).isdigit() else None, path=p)
    except Exception as e:
        # база — удобство для пульта, а не условие работы: файл уже убран,
        # и падать из-за записи в журнал нельзя
        print("   !! не записалось в базу: %s" % str(e)[:80])
    return "убран [%s] %s" % (it["br"], p)


def removable(keep, victim):
    """Можно ли убирать копию.

    Два разных издания одного альбома лежат в разных папках и оба полные:
    выдёргивать из них дорожки по одной нельзя, останутся два покалеченных
    альбома. Убираем, только если копия лежит отдельным файлом вне альбома,
    в той же самой папке, что и остающаяся, либо её альбом — пустышка вида
    "название альбома совпало с названием трека".
    """
    if any(j in victim["path"] for j in LOOSE):
        return True
    if fake_album(victim):
        return True
    return os.path.dirname(keep["path"]) == os.path.dirname(victim["path"])


def resolve(g, tolerance, lines, gone):
    """Разобрать группу. Вернуть (убрано, разные релизы, спорные,
    вторых изданий, разных версий по отпечатку)."""
    g = [i for i in g if i["id"] not in gone]
    if len(g) < 2:
        return 0, 0, 0, 0, 0
    # имена альбомов-пустышек не считаем доказательством другого релиза
    real = {norm(i["album"]) for i in g if not fake_album(i)}
    if len(real) > 1:
        lines.append("РАЗНЫЕ РЕЛИЗЫ  %s" % g[0]["title"])
        for i in g:
            lines.append("    [%s] %s — %s" % (i["br"], i["album"], i["path"]))
        return 0, 1, 0, 0, 0
    lens = [i["len"] for i in g if i["len"] > 0]
    if lens and max(lens) - min(lens) > tolerance:
        lines.append("ПОДОЗРЕНИЕ НА ЛОЖНЫЙ МАТЧ (длительность расходится)  %s"
                     % g[0]["title"])
        for i in g:
            lines.append("    [%s] %ss %s — %s" % (i["br"], i["len"], i["title"], i["path"]))
        return 0, 0, 1, 0, 0
    g.sort(key=rank)
    head = ["ДУБЛЬ  %s — %s" % (g[0]["album"], g[0]["title"]),
            "    остаётся [%s] %s" % (g[0]["br"], g[0]["path"])]
    moved = pressing = versions = 0
    body = []
    for i in g[1:]:
        if not removable(g[0], i):
            body.append("    ВТОРОЕ ИЗДАНИЕ, не трогаю: %s" % i["path"])
            pressing += 1
            continue
        sim = same_audio(g[0]["path"], i["path"])
        if sim is None:
            body.append("    отпечаток не снялся, не трогаю: %s" % i["path"])
            versions += 1
            continue
        bar = FP_MIN
        if (any(j in i["path"] for j in LOOSE) and i["br"] and g[0]["br"]
                and i["br"] <= g[0]["br"] * LOOSE_BR_RATIO):
            bar = FP_LOOSE
        if sim < bar:
            body.append("    РАЗНЫЕ ВЕРСИИ (%.0f%%, порог %.0f%%), не трогаю: %s"
                        % (sim * 100, bar * 100, i["path"]))
            versions += 1
            continue
        body.append("    %s  (отпечаток %.0f%%)" % (quarantine(i, sim, g[0]["path"]), sim * 100))
        gone.add(i["id"])
        moved += 1
    lines.extend(head + body)
    return moved, 0, 0, pressing, versions


def main():
    rows = list(items())
    # сколько дорожек в каждом альбоме: по этому признаку отличаем настоящий
    # альбом с заглавной песней от пустышки, созданной матчем на сингл
    sizes = collections.Counter(r["album_id"] for r in rows if r["album_id"])
    for r in rows:
        r["small"] = sizes.get(r["album_id"], 1) <= 2

    lines, gone = [], set()
    moved = cross = suspect = pressing = versions = 0

    by_mbid = collections.defaultdict(list)
    for it in rows:
        if it["mbid"]:
            by_mbid[it["mbid"]].append(it)
    for g in by_mbid.values():
        m, c, s, p, v = resolve(g, LEN_TOLERANCE, lines, gone)
        moved += m; cross += c; suspect += s; pressing += p; versions += v

    # копии из разных источников получают разные идентификаторы и в первом
    # проходе не встречаются
    by_name = collections.defaultdict(list)
    for it in rows:
        if it["id"] in gone or it["len"] <= 0:
            continue
        k = (norm_strict(it["who"]), norm_title(it["title"]))
        if not k[0] or not k[1]:
            continue
        by_name[k].append(it)
    cross2 = 0
    for g in by_name.values():
        if len(g) < 2:
            continue
        m, c, s, p, v = resolve(g, LEN_NAME, lines, gone)
        moved += m; cross2 += c; suspect += s; pressing += p; versions += v

    head = ("дублей убрано: %d | разные релизы: %d (+%d по имени) | вторых изданий: %d | "
            "разных версий по отпечатку: %d | подозрительных: %d"
            % (moved, cross, cross2, pressing, versions, suspect))
    print(head)
    with open(REPORT + (".dry" if DRY else ""), "w") as f:
        f.write(head + "\n\n" + "\n".join(lines) + "\n")
    stats_out = os.environ.get("JANITOR_STATS_OUT")
    if stats_out and not DRY:
        import json
        with open(stats_out, "w", encoding="utf-8") as f:
            json.dump({"moved": moved, "releases": cross + cross2, "pressings": pressing,
                       "versions": versions, "suspect": suspect}, f)


if __name__ == "__main__":
    main()
