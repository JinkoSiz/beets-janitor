#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Авторазбор пульта: вопросы, на которые ответ однозначен, закрываются сами.

К ноябрю 2026 в «Решениях» копилось больше 400 открытых вопросов, а руками
разобрано около 55. Большая часть очереди — не спорные случаи, а слабые
сигналы одного метода, которые второй источник подтверждает или опровергает.
Здесь — правила, которые закрывают вопрос только при согласии двух
независимых проверок. Всё остальное остаётся человеку.

Второй источник для звука — AcoustID: отпечаток всего файла (fpcalc, как в
verify.py) ищется в открытой базе, ответ — записи с названием и
исполнителями. На проверке 2026-11-14 он подтвердил верный трек там, где
превью Spotify дало 0.51, и поймал настоящие подмены: под «Stimulate» лежал
«Cleanin' Out My Closet», под «Real Situation» — «Top Rankin'».

Правила:

  подмена (verify.py)   AcoustID узнал в файле эту же песню (название и
                        исполнитель сходятся, уверенность от ACO_OK) —
                        «правильный трек». Если AcoustID уверенно (от
                        ACO_SURE) называет другую песню, вопрос остаётся,
                        а в карточке появляется его ответ
  две копии (consolidate.py)
                        одинаковое название, длина ±PAIR_LEN с и один
                        отпечаток AcoustID — остаётся лучшая копия, вторая
                        уходит в карантин, как по кнопке. Отпечатки у копий
                        разные — «разные версии», обе остаются
  как есть (retry.py)   вопрос открыт дольше asis_days дней — «оставить как
                        есть»: теги не трогаются, retry.py больше не спрашивает
  цензура (uncensor.py) от трёх эталонов, и каждое место показал только
                        один из них — остальные с ним не согласны, это шум

Каждое решение пишется в журнал (script=autoresolve, op=decide) и
откатывается оттуда: вопрос снова открывается. Копия, ушедшая в карантин,
возвращается из карантина, как обычно.

  autoresolve.py          разобрать очередь (не больше autoresolve_limit
                          запросов к AcoustID за прогон)
  autoresolve.py --dry    ничего не записывать, только показать решения
"""
import collections
import difflib
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests  # noqa: E402

import discography as disco  # noqa: E402
import janitordb as jdb  # noqa: E402

DRY = "--dry" in sys.argv

ACO_URL = "https://api.acoustid.org/v2/lookup"
ACO_PAUSE = 0.4         # AcoustID просит не больше трёх запросов в секунду
ACO_OK = 0.80           # от такой уверенности совпадение названия — подтверждение
ACO_SURE = 0.90         # от такой — «это другая песня» показывается в карточке
ACO_FRESH_DAYS = 30     # ответ AcoustID в карточке живёт столько, потом спрашиваем снова
PAIR_LEN = 3.0          # копии одной записи расходятся по длине не больше чем на столько
CENSOR_REFS = 3         # столько эталонов нужно, чтобы одиночные места считать шумом
LOSSLESS = {"FLAC", "ALAC", "WAV", "AIFF", "APE", "WAVPACK"}


# ---------------------------------------------------------------- AcoustID
class Aco:
    """Запросы к AcoustID с лимитом на прогон и паузой между ними."""

    def __init__(self, key, limit):
        self.key, self.left, self.last, self.errors = key, limit, 0.0, 0

    def lookup(self, path):
        """Записи, которые AcoustID видит в файле: [(score, rec_id, title, [artists], aid)].
        aid — номер отпечатка в AcoustID: одинаковый звук попадает в один
        отпечаток, а записей MusicBrainz на нём бывает много (по одной на
        каждый релиз, где трек вышел).
        None — спросить не удалось (лимит, сбой, нет файла)."""
        if self.left <= 0 or self.errors >= 5 or not os.path.isfile(path):
            return None
        try:
            out = subprocess.run(["fpcalc", "-json", path], capture_output=True, text=True, timeout=120).stdout
            fp = json.loads(out or "{}")
        except Exception:
            return None
        if not fp.get("fingerprint"):
            return None
        wait = ACO_PAUSE - (time.time() - self.last)
        if wait > 0:
            time.sleep(wait)
        self.left -= 1
        self.last = time.time()
        try:
            r = requests.post(ACO_URL, data={"client": self.key, "duration": int(fp.get("duration") or 0),
                                             "fingerprint": fp["fingerprint"], "meta": "recordings"}, timeout=30)
            data = r.json()
        except Exception:
            self.errors += 1
            return None
        if data.get("status") != "ok":
            self.errors += 1
            return None
        self.errors = 0
        recs = []
        for res in data.get("results") or []:
            for rec in res.get("recordings") or []:
                if rec.get("title"):
                    recs.append((round(float(res.get("score") or 0), 3), rec.get("id"), rec["title"],
                                 [a.get("name", "") for a in rec.get("artists") or []], res.get("id")))
        return recs


def title_keys(s):
    """Название без пометок в скобках и без хвоста через тире («… - Skeler Vision»)."""
    t = disco.norm_title(s)
    keys = {t} if t else set()
    tail = disco.norm_title(disco.DASH_TAIL.sub("", str(s or "")))
    if tail:
        keys.add(tail)
    return keys


# пометки версий: «Snow in Sweden (stripped version)» — не то же, что «Snow In Sweden»
VERSION = re.compile(r"\b(remix|rmx|mix|edit|live|acoustic|stripped|instrumental|karaoke|demo|sped|speed|slowed|"
                     r"reverb|cover|version|remaster|remastered|unplugged|radio|extended|clean|explicit|"
                     r"cappella|capella|acapella|session|sessions|rehearsal|take|"
                     r"ремикс|микс|версия|live|концерт|акустика|инструментал|демо)\b", re.I)


def versions(s):
    return {m.group(1).lower() for m in VERSION.finditer(str(s or ""))} - {"explicit", "remaster", "remastered"}


def same_title(a, b):
    if versions(a) != versions(b):
        return False
    for x in title_keys(a):
        for y in title_keys(b):
            if x == y or (min(len(x), len(y)) >= 4 and (x.startswith(y) or y.startswith(x))):
                return True
            if difflib.SequenceMatcher(None, x, y).ratio() >= 0.85:
                return True
    return False


def artist_keys(s):
    return {k for n in disco.artist_names(s) for k in disco.name_keys(n)}


def same_artist(item_artist, rec_artists):
    mine = artist_keys(item_artist)
    theirs = {k for a in rec_artists for k in artist_keys(a)}
    return bool(mine & theirs)


def judge(recs, artist, title):
    """('ok', rec) — AcoustID узнал эту песню; ('other', rec) — уверенно другую;
    (None, None) — ничего определённого."""
    if not recs:
        return None, None
    for score, rid, rtitle, rartists, *_ in recs:
        if score >= ACO_OK and same_title(rtitle, title) and same_artist(artist, rartists):
            return "ok", (score, rid, rtitle, rartists)
    top = max(recs, key=lambda r: r[0])
    if top[0] >= ACO_SURE and not any(same_title(r[2], title) for r in recs if r[0] >= ACO_OK):
        return "other", top
    return None, None


def brief(rec):
    score, rid, title, artists = rec[:4]
    return {"score": score, "id": rid, "title": title, "artists": artists,
            "url": "https://musicbrainz.org/recording/%s" % rid if rid else None}


# ---------------------------------------------------------------- решения
class Run:
    def __init__(self, con, aco):
        self.con, self.aco = con, aco
        self.stats = collections.Counter()

    def decide(self, r, decision, why, item_id=None, path=None, action=None):
        """Закрыть вопрос r так же, как это сделала бы кнопка в пульте."""
        print("   %-13s %s: %s (%s)" % (decision, r["kind"], r["title"], why))
        self.stats[r["kind"] + ":" + decision] += 1
        if DRY:
            return
        if action is not None:
            jdb.enqueue(self.con, action[0], action[1], r["id"])
        self.con.execute("UPDATE reviews SET status='resolved', decision=?, decided_at=? WHERE id=?",
                         (decision, jdb.now(), r["id"]))
        jdb.log_event(self.con, "autoresolve", "decide", {"review": r["id"], "status": "open"},
                      {"review": r["id"], "kind": r["kind"], "decision": decision, "why": why,
                       "title": r["title"]}, item_id=item_id, path=path)

    def note(self, r, p, key, value):
        """Дописать в карточку то, что пульт покажет человеку."""
        if DRY:
            return
        p[key] = value
        self.con.execute("UPDATE reviews SET payload=? WHERE id=?", (json.dumps(p, ensure_ascii=False), r["id"]))

    def aco_for(self, r, p, path, slot="acoustid"):
        """Ответ AcoustID по файлу: из карточки, если свежий, иначе запросом."""
        cached = p.get(slot)
        if isinstance(cached, dict) and cached.get("path") == path and cached.get("v") == 2 and \
                days_since(cached.get("at")) < ACO_FRESH_DAYS:
            return [tuple(x) for x in cached.get("recs") or []]
        recs = self.aco.lookup(path)
        if recs is None:
            self.stats["acoustid_skipped"] += 1
            return None
        self.stats["acoustid"] += 1
        self.note(r, p, slot, {"path": path, "at": jdb.now(), "v": 2, "recs": recs[:12]})
        return recs

    # -- подмена по превью
    def verify_card(self, r, p):
        path = p.get("path") or ""
        if not os.path.isfile(path):
            return
        recs = self.aco_for(r, p, path)
        verdict, rec = judge(recs, p.get("artist"), p.get("title"))
        if verdict == "ok":
            self.decide(r, "keep", "AcoustID узнал «%s» (%.0f%%)" % (rec[2], rec[0] * 100),
                        item_id=p.get("item_id"), path=path)
        elif verdict == "other":
            self.stats["substitution:acoustid_other"] += 1
            self.note(r, p, "acoustid_says", brief(rec))
            print("   ?             substitution: %s — AcoustID слышит «%s — %s» (%.0f%%)"
                  % (r["title"], ", ".join(rec[3]), rec[2], rec[0] * 100))

    # -- две копии
    def pair_card(self, r, p):
        files = p.get("files") or []
        if len(files) != 2 or not all(os.path.isfile(f.get("path") or "") for f in files):
            return
        a, b = files
        if not same_title(a.get("title"), b.get("title")):
            return
        if abs(float(a.get("length") or 0) - float(b.get("length") or 0)) > PAIR_LEN:
            return
        ra = self.aco_for(r, p, a["path"], "acoustid_a")
        rb = self.aco_for(r, p, b["path"], "acoustid_b")
        # сравниваем отпечатки AcoustID, а не записи MusicBrainz: одна и та же
        # запись на двух релизах — две записи MB, но один отпечаток
        # у одной записи в AcoustID бывает несколько отпечатков, а у одного
        # отпечатка — записи MusicBrainz с разных релизов. Копии одинаковые,
        # если совпал отпечаток или запись; разные — только если ни то, ни другое
        aid_a = {x[4] for x in ra or [] if len(x) > 4 and x[0] >= ACO_OK}
        aid_b = {x[4] for x in rb or [] if len(x) > 4 and x[0] >= ACO_OK}
        rec_a = {x[1] for x in ra or [] if x[0] >= ACO_OK}
        rec_b = {x[1] for x in rb or [] if x[0] >= ACO_OK}
        same = bool(aid_a & aid_b or rec_a & rec_b)
        if aid_a and aid_b and not same:
            # звук разный (другая версия или мастер): ничего не удаляем
            self.decide(r, "different", "AcoustID: у копий разный звук")
            return
        if not same:
            return
        keep, drop = sorted(files, key=quality, reverse=True)
        decision = "keep_a" if keep is a else "keep_b"
        why = "та же запись по AcoustID, оставлена %s %s" % (keep.get("format"), keep.get("bitrate"))
        self.decide(r, decision, why, item_id=drop.get("item_id"), path=drop.get("path"),
                    action=("quarantine", {"item_id": drop.get("item_id"), "similarity": p.get("similarity"),
                                           "reason": "substitution" if r["kind"] == "substitution" else "duplicate",
                                           "kept": keep.get("path")}))

    # -- как есть
    def asis_card(self, r, p, days):
        if days_since(r["created_at"]) >= days:
            self.decide(r, "asis", "открыт дольше %d дней" % days, item_id=p.get("item_id"), path=p.get("path"))

    # -- цензура
    def censor_card(self, r, p):
        refs, spots = p.get("refs") or [], p.get("spots") or []
        if len(refs) >= CENSOR_REFS and spots and not any(s.get("sure") for s in spots):
            self.decide(r, "dismiss", "каждое место показал один эталон из %d" % len(refs),
                        item_id=p.get("item_id"), path=p.get("path"))
            if not DRY:
                self.con.execute("UPDATE censor SET verdict='clean', checked_at=? WHERE item_id=?",
                                 (jdb.now(), p.get("item_id")))


def quality(f):
    """Чем лучше копия: без потерь, битрейт, разложена по альбому, без «.1» в имени."""
    path = f.get("path") or ""
    return (str(f.get("format") or "").upper() in LOSSLESS, int(f.get("bitrate") or 0),
            "/Non-Album/" not in path, not re.search(r"\.\d+\.[^./]+$", path), -len(path))


def days_since(ts):
    import datetime
    try:
        return (datetime.datetime.now() - datetime.datetime.fromisoformat(str(ts))).days
    except Exception:
        return 10 ** 6


def main():
    con = jdb.connect()
    if jdb.setting(con, "auto_resolve") != "1":
        print("авторазбор выключен в настройках")
        return
    aco = Aco(jdb.setting(con, "acoustid_key"), int(float(jdb.setting(con, "autoresolve_limit"))))
    run = Run(con, aco)
    asis_days = int(float(jdb.setting(con, "asis_days")))
    rows = con.execute("SELECT * FROM reviews WHERE status='open' ORDER BY created_at").fetchall()
    print("открытых вопросов: %d" % len(rows))
    for r in rows:
        p = jdb.loads(r["payload"], {}) or {}
        try:
            if r["kind"] == "substitution" and r["key"].startswith("verify:"):
                run.verify_card(r, p)
            elif r["kind"] in ("substitution", "duplicate"):
                run.pair_card(r, p)
            elif r["kind"] == "asis":
                run.asis_card(r, p, asis_days)
            elif r["kind"] == "censored":
                run.censor_card(r, p)
        except Exception as e:
            print("!! %s %s: %s" % (r["kind"], r["title"], str(e)[:160]))
            run.stats["failed"] += 1
        if not DRY:
            con.commit()
    left = con.execute("SELECT count(*) FROM reviews WHERE status='open'").fetchone()[0]
    print("итог: %s | открыто осталось: %d" % (dict(run.stats), left))
    stats_out = os.environ.get("JANITOR_STATS_OUT")
    if stats_out and not DRY:
        with open(stats_out, "w", encoding="utf-8") as f:
            json.dump(dict(run.stats, left=left), f, ensure_ascii=False)


if __name__ == "__main__":
    main()
