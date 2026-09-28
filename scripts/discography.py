#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Дискография исполнителя против фонотеки: что уже есть, что скачать.

Источник — Deezer. Открытый API без ключа, и главное: ISRC у каждой дорожки
приходит прямо в списке треков альбома, одним запросом. У Spotify ISRC
отдаётся только поштучно, по запросу на трек и из суточного лимита, а поиск
по имени у Deezer ненадёжен («Eminem» находил «Emine'm»), поэтому
исполнителя подтверждаем пересечением с фонотекой.

Разбор каждой дорожки, по порядку (порядок важен). Решение, принятое в
пульте, хранится рядом с вердиктом (release_tracks.decision) и при повторном
разборе не затирается — вопрос второй раз не задаётся.

  1. ISRC уже встречался     -> повтор внутри дискографии: сингл, вошедший в
     в этой дискографии         альбом, приходит вместе с альбомом
  2. тот же ISRC в фонотеке  -> есть
  3. пометка версии          -> другая версия: инструментал, ремикс, live…
                                По умолчанию не качаем. Пометка побеждает
                                отпечаток: инструментал и вокал рэп-трека дают
                                86–96%, читка в отпечатке почти не видна.
                                Если помечена только копия в фонотеке (live),
                                студийную версию, наоборот, надо скачать
  4. то же название и длина  -> повтор внутри дискографии: Deluxe-издание,
     уже встречались здесь      сингл с альбома
  5. в фонотеке то же        -> превью Deezer против файла: от ref_ok — та же
     название и длина,          запись (переиздание), иначе спросить
     но ISRC другой
  6. иначе                   -> скачать

Релизы разбираются от альбомов к синглам, внутри — обычное издание раньше
Deluxe и раньше помеченного (инструментального): повтором считается сингл
или дорожка Deluxe, а не альбомная.

Проверено на Кровостоке (251 дорожка, 27 секунд): есть 110, повторов 39
(дорожки Deluxe-изданий и синглы с альбомов), других версий 87
(инструменталы), спросить 0, скачать 15 — новый альбом «Пиры и раны»,
бонусы «Науки (Deluxe)» и студийная «Голова», которой в фонотеке нет, есть
только live.

Работает без beets: фонотеку читает из library.db напрямую и только на
чтение. Поэтому модуль годится и для пульта, и для ночного follow.py.

  discography.py "Кровосток"        кандидаты и разбор лучшего из них
  discography.py --id 12345         разбор по идентификатору Deezer
"""
import json
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests  # noqa: E402

import env  # noqa: E402

API = "https://api.deezer.com/"
PAUSE = 0.15            # у Deezer потолок 50 запросов за 5 секунд
LEN_SAME = 3            # секунд: одна и та же запись на разных релизах
TYPE_ORDER = {"album": 0, "ep": 1, "single": 2, "compile": 3}

# Пометки версии. Ремастер сюда не входит нарочно: это та же запись, и её
# проверит отпечаток на шаге 5.
MARK = re.compile(
    r"instrumental|инструментал|минус|acapella|a cappella|karaoke|караоке|"
    r"\blive\b|лайв|концерт|remix|ремикс|\brmx\b|sped ?up|slowed|reverb|"
    r"\bclean\b|acoustic|акустик|\bdemo\b|демо|radio edit|extended|nightcore",
    re.I)

TRANSLIT = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya"})

SPLIT = re.compile(r"\s*(?:,|;|&|\+|/|\bfeat\.?\b|\bft\.?\b|\bfeaturing\b|\bvs\.?\b|\bx\b)\s*", re.I)


# ---------------------------------------------------------------- имена
def norm_name(s):
    s = str(s or "").lower().replace("ё", "е")
    return re.sub(r"[^a-zа-я0-9]+", " ", s).strip()


def name_keys(s):
    """Ключи сравнения имени исполнителя: как есть и в латинице."""
    n = norm_name(s)
    return {n, n.translate(TRANSLIT)} if n else set()


def artist_names(s):
    """«Xcho (Feat. Gor)», «Лигалайз + П-13» -> отдельные имена."""
    s = re.sub(r"[\(\[]\s*(?:feat|ft)\.?[^\)\]]*[\)\]]", ",", str(s or ""), flags=re.I)
    return [p.strip() for p in SPLIT.split(s) if p and p.strip()]


def primary_artist(s):
    names = artist_names(s)
    return names[0] if names else ""


def norm_title(s):
    s = str(s or "").lower().replace("ё", "е")
    s = re.sub(r"[\(\[].*?[\)\]]", " ", s)
    s = re.sub(r"\b(feat|ft)\.?\b.*$", " ", s)
    return re.sub(r"[^a-zа-я0-9]+", " ", s).strip()


def marked(*texts):
    return any(MARK.search(str(t or "")) for t in texts)


# ---------------------------------------------------------------- Deezer
def deezer(path, **params):
    """Запрос к Deezer с повтором при исчерпании квоты. None — не вышло."""
    for attempt in range(4):
        try:
            r = requests.get(API + path.lstrip("/"), params=params, timeout=30)
            j = r.json()
        except Exception:
            time.sleep(2 * (attempt + 1))
            continue
        err = (j or {}).get("error") if isinstance(j, dict) else None
        if err:
            # code 4 — квота: подождать и повторить; остальное — окончательный отказ
            if isinstance(err, dict) and err.get("code") == 4:
                time.sleep(5)
                continue
            return None
        time.sleep(PAUSE)
        return j
    return None


def deezer_all(path, **params):
    params.setdefault("limit", 100)
    out, index = [], 0
    while True:
        j = deezer(path, index=index, **params)
        if not j:
            break
        data = j.get("data") or []
        out.extend(data)
        if not j.get("next") or not data:
            break
        index += len(data)
    return out


# ---------------------------------------------------------------- фонотека
class Library:
    """Фонотека из library.db, только на чтение.

    beets не нужен: пульт живёт в своём контейнере, и держать открытой
    чужую базу через её библиотеку значило бы рисковать блокировками.
    """

    def __init__(self, path=None):
        path = path or env.LIBRARY_DB
        con = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=30)
        con.row_factory = sqlite3.Row
        cols = {r[1] for r in con.execute("PRAGMA table_info(items)")}
        isrc = "isrc" if "isrc" in cols else "'' AS isrc"
        self.items = []
        for r in con.execute("SELECT id, path, artist, albumartist, title, album, length, %s FROM items" % isrc):
            p = r["path"]
            p = p.decode("utf-8", "replace") if isinstance(p, bytes) else str(p)
            # beets 2 хранит пути относительно папки фонотеки
            if not os.path.isabs(p):
                p = os.path.join(env.MUSIC_DIR, p)
            self.items.append({
                "id": r["id"], "path": p,
                "artist": r["artist"] or "", "albumartist": r["albumartist"] or "",
                "title": r["title"] or "", "album": r["album"] or "",
                "length": float(r["length"] or 0), "isrc": str(r["isrc"] or "").strip().upper()})
        con.close()
        self.by_isrc = {}
        for it in self.items:
            if it["isrc"]:
                self.by_isrc.setdefault(it["isrc"], it)

    def of_artist(self, *names):
        """Дорожки, где среди исполнителей есть любое из имён."""
        keys = set()
        for n in names:
            keys |= name_keys(n)
        out = []
        for it in self.items:
            for n in artist_names(it["artist"]) + artist_names(it["albumartist"]):
                if name_keys(n) & keys:
                    out.append(it)
                    break
        return out


# ---------------------------------------------------------------- исполнитель
def search_artists(name, lib, limit=6):
    """Кандидаты в Deezer с пересечением с фонотекой.

    Поиск по имени у Deezer путается («Hikiraze» -> «Niko Haze»), поэтому
    для каждого кандидата смотрим его популярные треки и считаем, сколько
    из них уже лежит в фонотеке у исполнителя с таким именем. Сортировка:
    совпало имя, потом пересечение, потом число поклонников.
    """
    j = deezer("search/artist", q=name, limit=limit) or {}
    mine = defaultdict(set)
    for it in lib.of_artist(name):
        mine[norm_title(it["title"])].add(it["id"])
    want = name_keys(name)
    out = []
    for a in j.get("data") or []:
        top = (deezer("artist/%s/top" % a["id"], limit=50) or {}).get("data") or []
        overlap = sum(1 for t in top if norm_title(t.get("title")) in mine)
        out.append({"id": a["id"], "name": a.get("name"), "picture": a.get("picture_medium"),
                    "albums": a.get("nb_album"), "fans": a.get("nb_fan"), "link": a.get("link"),
                    "exact": bool(name_keys(a.get("name")) & want), "overlap": overlap,
                    "library_tracks": sum(len(v) for v in mine.values())})
    out.sort(key=lambda c: (not c["exact"], -c["overlap"], -(c["fans"] or 0)))
    return out


def pick_artist(cands):
    """Однозначный выбор для автоматики или None — тогда решает человек.

    Берём кандидата, только если имя совпало, с фонотекой есть пересечение
    и второго такого же нет.
    """
    good = [c for c in cands if c["exact"] and c["overlap"] > 0]
    if not good:
        return None
    if len(good) > 1 and good[1]["overlap"] >= good[0]["overlap"]:
        return None
    return good[0]


EXPANDED = re.compile(r"deluxe|expanded|anniversary|bonus|special edition|делюкс|переиздан", re.I)


def releases_of(artist_id):
    """Релизы в порядке разбора: альбомы, EP, синглы, сборники; внутри —
    по дате, и обычное издание раньше расширенного. Тогда повтором
    считается дорожка Deluxe-издания, а не альбомная."""
    rels = deezer_all("artist/%s/albums" % artist_id)
    rels.sort(key=lambda r: (TYPE_ORDER.get(r.get("record_type"), 9), r.get("release_date") or "",
                             marked(r.get("title")), bool(EXPANDED.search(r.get("title") or ""))))
    return rels


def tracks_of(album_id):
    return deezer_all("album/%s/tracks" % album_id, limit=500)


# ---------------------------------------------------------------- разбор
class Resolver:
    """Разбор дорожек против фонотеки с общей памятью на всю дискографию."""

    def __init__(self, lib, artist_names_, ok_t=0.85, compare=True):
        self.lib = lib
        self.mine = lib.of_artist(*artist_names_)
        self.by_title = defaultdict(list)
        for it in self.mine:
            self.by_title[norm_title(it["title"])].append(it)
        self.ok_t = ok_t
        self.compare = compare
        self.seen_isrc = {}
        self.seen_name = {}
        self._fp = {}

    def _file_fp(self, path):
        if path not in self._fp:
            import verify
            self._fp[path] = verify.fingerprint(path) if os.path.isfile(path) else []
        return self._fp[path]

    def _same_recording(self, preview_url, cands):
        """Лучшее сходство превью с файлами-кандидатами. None — не сравнилось."""
        if not preview_url or not self.compare:
            return None, None
        import verify
        pf = verify.preview_fingerprint(preview_url)
        if not pf:
            return None, None
        best, best_it = None, None
        for c in cands:
            s = verify.best_match(pf, self._file_fp(c["path"]))
            if s is not None and (best is None or s > best):
                best, best_it = s, c
        return best, best_it

    def track(self, t, release):
        """Вердикт одной дорожки Deezer. Вернуть dict для release_tracks."""
        tid = str(t.get("id"))
        isrc = str(t.get("isrc") or "").strip().upper()
        dur = int(t.get("duration") or 0)
        title = str(t.get("title") or "")
        key = norm_title(title)
        out = {"provider_id": tid, "isrc": isrc or None, "title": title, "duration": dur,
               "verdict": None, "library_item": None, "similarity": None,
               "info": {"preview": t.get("preview"), "version": t.get("title_version") or None}}

        is_marked = marked(title, t.get("title_version"), release.get("title"))

        def done(verdict, why, item=None, sim=None):
            out["verdict"], out["library_item"], out["similarity"] = verdict, item and item["id"], sim
            out["info"]["why"] = why
            if item is not None:
                out["info"]["library_path"] = item["path"]
            if isrc:
                self.seen_isrc.setdefault(isrc, release.get("title"))
            # повтором по названию бывает только обычная версия: иначе вокальный
            # трек посчитался бы повтором инструментала той же длины
            if not is_marked:
                self.seen_name.setdefault(key, []).append((dur, release.get("title")))
            return out

        if isrc and isrc in self.seen_isrc:
            return done("dup", "уже есть в релизе «%s»" % self.seen_isrc[isrc])
        if isrc and isrc in self.lib.by_isrc:
            return done("have_isrc", "тот же ISRC", self.lib.by_isrc[isrc])

        same_len = [c for c in self.by_title.get(key, []) if abs(c["length"] - dur) <= LEN_SAME]
        if is_marked:
            twins = [c for c in same_len if marked(c["title"], c["album"])]
            if not twins:
                return done("version", "другая версия: пометка в названии")
            sim, it = self._same_recording(t.get("preview"), twins)
            if sim is not None and sim >= self.ok_t:
                return done("have_same", "та же запись под другим ISRC", it, sim)
            return done("version", "другая версия: пометка в названии")

        # повтор внутри дискографии раньше сравнения с фонотекой: дорожка
        # Deluxe-издания или сингл с альбома решается вместе с тем релизом,
        # где она уже встретилась, — есть он в фонотеке или скачивается
        for d, where in self.seen_name.get(key, []):
            if abs(d - dur) <= LEN_SAME:
                return done("dup", "то же название и длина в релизе «%s»" % where)

        plain = [c for c in same_len if not marked(c["title"], c["album"])]
        if plain:
            sim, it = self._same_recording(t.get("preview"), plain)
            if sim is not None and sim >= self.ok_t:
                return done("have_same", "та же запись под другим ISRC", it, sim)
            why = ("похожее название и длина, звук %.0f%%" % (sim * 100) if sim is not None
                   else "похожее название и длина, сравнить звук не вышло")
            return done("ask", why, it or plain[0], sim)
        return done("get", "нет в фонотеке")


VERDICT_GROUP = {"have_isrc": "have", "have_same": "have", "dup": "dup", "version": "version",
                 "ask": "ask", "get": "get"}


def counts(tracks):
    c = defaultdict(int)
    for t in tracks:
        c[VERDICT_GROUP.get(t["verdict"], t["verdict"])] += 1
    return dict(c)


def wanted(track):
    """Качать ли дорожку: решение из пульта перекрывает вердикт разбора."""
    decision = track["decision"] if "decision" in track.keys() else None
    if decision:
        return decision == "get"
    return track["verdict"] == "get"


def resolve(artist_id, artist_name, lib, ok_t=0.85, releases=None, progress=None):
    """Вся дискография (или переданные релизы) с вердиктом по каждой дорожке."""
    rels = releases if releases is not None else releases_of(artist_id)
    res = Resolver(lib, [artist_name], ok_t)
    out = []
    for n, r in enumerate(rels, 1):
        if progress:
            progress(n, len(rels), r.get("title"))
        tracks = [res.track(t, r) for t in tracks_of(r["id"])]
        out.append({"provider": "deezer", "provider_id": str(r["id"]), "title": r.get("title"),
                    "type": r.get("record_type"), "release_date": r.get("release_date"),
                    "tracks_total": len(tracks), "cover": r.get("cover_medium"), "link": r.get("link"),
                    "tracks": tracks, "counts": counts(tracks)})
    return out


# ---------------------------------------------------------------- хранение
def store(con, artist_id, rels, status):
    """Сохранить разбор в releases / release_tracks общей базы.

    status — статус для релизов, которых в базе ещё нет: строка или функция
    от релиза. У известных статус не трогается: его ведут пульт и follow.py.
    Решения из пульта (release_tracks.decision) не затираются. Вернуть
    {provider_id релиза: id строки}.
    """
    import janitordb as jdb
    ids = {}
    for r in rels:
        c = json.dumps(r["counts"], ensure_ascii=False)
        row = con.execute("SELECT id FROM releases WHERE provider=? AND provider_id=?",
                          (r["provider"], r["provider_id"])).fetchone()
        if row is None:
            st = status(r) if callable(status) else status
            rid = con.execute(
                "INSERT INTO releases(artist_id, provider, provider_id, title, type, release_date, tracks_total, "
                "status, counts, found_at, cover, link) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (artist_id, r["provider"], r["provider_id"], r["title"], r["type"], r["release_date"],
                 r["tracks_total"], st, c, jdb.now(), r.get("cover"), r.get("link"))).lastrowid
        else:
            rid = row["id"]
            con.execute("UPDATE releases SET title=?, type=?, release_date=?, cover=?, link=?, "
                        "artist_id=COALESCE(artist_id, ?) WHERE id=?",
                        (r["title"], r["type"], r["release_date"], r.get("cover"), r.get("link"), artist_id, rid))
            # релиз без разбора дорожек (release_row) прежний разбор не затирает
            if r.get("tracks"):
                con.execute("UPDATE releases SET tracks_total=?, counts=? WHERE id=?", (r["tracks_total"], c, rid))
        for t in r.get("tracks") or []:
            con.execute(
                "INSERT INTO release_tracks(release_id, provider_id, isrc, title, duration, verdict, library_item, "
                "similarity, info) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(release_id, provider_id) DO UPDATE SET "
                "isrc=excluded.isrc, title=excluded.title, duration=excluded.duration, verdict=excluded.verdict, "
                "library_item=excluded.library_item, similarity=excluded.similarity, info=excluded.info",
                (rid, t["provider_id"], t["isrc"], t["title"], t["duration"], t["verdict"], t["library_item"],
                 None if t["similarity"] is None else round(t["similarity"], 3),
                 json.dumps(t["info"], ensure_ascii=False)))
        ids[r["provider_id"]] = rid
    return ids


def release_row(r):
    """Релиз Deezer без разбора дорожек — для отметки «уже известен»."""
    return {"provider": "deezer", "provider_id": str(r["id"]), "title": r.get("title"),
            "type": r.get("record_type"), "release_date": r.get("release_date"), "tracks_total": None,
            "cover": r.get("cover_medium"), "link": r.get("link"), "tracks": [], "counts": {}}


# ---------------------------------------------------------------- CLI
def _print(rels):
    total = defaultdict(int)
    print("%-4s %-7s %-40s %4s  %s" % ("год", "тип", "релиз", "дор", "есть/повтор/версии/спросить/скачать"))
    for r in rels:
        c = r["counts"]
        for k, v in c.items():
            total[k] += v
        print("%-4s %-7s %-40s %4d  %d/%d/%d/%d/%d" % (
            (r["release_date"] or "")[:4], r["type"], str(r["title"])[:40], r["tracks_total"],
            c.get("have", 0), c.get("dup", 0), c.get("version", 0), c.get("ask", 0), c.get("get", 0)))
    print()
    print("итого дорожек %d: есть %d, повторов %d, других версий %d, спросить %d, скачать %d"
          % (sum(total.values()), total["have"], total["dup"], total["version"], total["ask"], total["get"]))
    for r in rels:
        for t in r["tracks"]:
            if t["verdict"] == "ask":
                print("   ? %-30s %-30s %s" % (str(r["title"])[:30], t["title"][:30], t["info"]["why"]))


def main():
    lib = Library()
    if "--id" in sys.argv:
        aid = sys.argv[sys.argv.index("--id") + 1]
        a = deezer("artist/%s" % aid) or {}
        name = a.get("name") or aid
    else:
        name = " ".join(a for a in sys.argv[1:] if not a.startswith("--"))
        cands = search_artists(name, lib)
        for c in cands:
            print("%-10s %-28s альбомов %-4s поклонников %-8s имя %s, пересечение %d"
                  % (c["id"], str(c["name"])[:28], c["albums"], c["fans"],
                     "совпало" if c["exact"] else "другое", c["overlap"]))
        best = pick_artist(cands)
        if best is None:
            sys.exit("однозначного кандидата нет — нужно выбрать руками (--id)")
        aid, name = best["id"], best["name"]
        print("беру: %s (%s)\n" % (name, aid))
    t0 = time.time()
    rels = resolve(aid, name, lib)
    _print(rels)
    print("\n%.0f с" % (time.time() - t0))
    if "--json" in sys.argv:
        print(json.dumps(rels, ensure_ascii=False)[:2000])


if __name__ == "__main__":
    main()
