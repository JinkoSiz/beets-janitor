#!/usr/bin/env python3
"""Ночное добивание as-is: перематчить то, что осталось без MBID.

Повторы с нарастающим интервалом, а не одна попытка на трек: трек мог не
сматчиться из-за того, что источник был недоступен, и списывать его навсегда
неправильно. После MAX_ATTEMPTS неудач считаем безнадёжным (ютуб-эксклюзивы,
эдиты, чего нет ни в одном каталоге).

Попытка НЕ засчитывается, если Spotify в этот момент отключён предохранителем:
без него шансы на совпадение сильно ниже, и жечь попытку впустую не стоит.

Синглтоны матчатся внутри процесса, а не через отдельный beet import на каждый
трек: так не перезапускается загрузка плагинов на каждом файле и, что важнее,
видна сама оценка совпадения — по ней работает мягкое правило ниже.

Безопасность: никаких удалений, beet remove не используется. Каждое
применённое совпадение пишется в JOURNAL, по нему можно откатиться.
"""
import datetime
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import env  # noqa: E402

STATE = env.RETRY_STATE
JOURNAL = env.JOURNAL
BUDGET_FILE = env.SPOTIFY_BUDGET_FILE
BREAKER_FILE = env.SPOTIFY_BREAKER_FILE
LIBCFG = env.LIBRARY_CONFIG
SPOTIFY_DAILY = env.SPOTIFY_DAILY
REQ_PER_TRACK = 3
MAX_ALBUM = 40
# интервалы между попытками в днях: первые две подряд, дальше с разбегом.
# Попытка может пропасть впустую по причинам, которые скрипт не видит
# (источник ответил из кэша, был на паузе), поэтому в начале даём чаще.
INTERVALS = [1, 1, 2, 4, 8, 16]
MAX_ATTEMPTS = len(INTERVALS)
EXT = (".mp3", ".flac", ".opus", ".m4a", ".ogg", ".wav", ".aac", ".wma")
SEP = "\x01"
DRY = "--dry" in sys.argv

# Мягкое правило приёмки.
# beets принимает автоматически только оценку strong, то есть дистанцию
# меньше 0.15. Замер по библиотеке показал, что этот порог отсекает верные
# совпадения, где единственное расхождение — порядок и разделитель в списке
# исполнителей: вариант через амперсанд против варианта через запятую в
# обратном порядке даёт 0.153 при совпадающих названии и длительности.
# Поднимать сам порог нельзя: в полосе 0.15-0.25 попадаются и ложные
# кандидаты. Поэтому судим не по одной дистанции, а по составу расхождений:
# ненулевой штраф должен быть ровно один и именно за исполнителя, а
# длительность обязана сойтись. Нулевые штрафы beets в keys() не отдаёт,
# поэтому длительность проверяем отдельно и требуем, чтобы она у кандидата
# вообще была — иначе несовпадение по длине осталось бы незамеченным.
SOFT_MAX = 0.25
SOFT_PENALTIES = {"track_artist"}
LEN_TOL = 2
# Одного штрафа за исполнителя мало: ровно такой же одиночный штраф даёт и
# полностью чужой артист с тем же названием трека. На типовых названиях
# ("Outro", "Intro") этого хватает, чтобы притянуть чужую запись. Поэтому
# дополнительно требуем пересечение по словам: у "A & B" и "B, A" оно есть,
# у "BONES" и "Bill Nelson" — нет.
ARTIST_STOP = {"feat", "ft", "featuring", "prod", "the", "vs", "и"}
# кириллица и латиница пишут одни и те же имена по-разному: ЛСП и LSP — это
# один исполнитель. Сравниваем в общей раскладке, иначе теряем верные матчи.
TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def words(s):
    ws = re.split(r"[^0-9a-zA-Zа-яА-ЯёЁ]+", str(s or "").lower())
    return {w for w in ws if len(w) > 1 and w not in ARTIST_STOP}


def translit(w):
    return "".join(TRANSLIT.get(ch, ch) for ch in w)


def artist_ok(a, b):
    """Хотя бы одно общее слово в именах, с поправкой на раскладку."""
    wa, wb = words(a), words(b)
    if not wa or not wb:
        return False
    if wa & wb:
        return True
    return bool({translit(w) for w in wa} & {translit(w) for w in wb})


def title_ok(a, b):
    """Общее слово в названиях. Нужно только для strong: там beets прощает
    расхождение в названии, если исполнитель и длительность сошлись, и так
    "Реакция" однажды стала "П-ровокацией". На мягком пути штраф за название
    обязан быть нулевым, поэтому проверка там не требуется."""
    wa, wb = words(a), words(b)
    if not wa or not wb:
        return True
    if wa & wb:
        return True
    return bool({translit(w) for w in wa} & {translit(w) for w in wb})


def beet(*a):
    return subprocess.run(["beet", *a], capture_output=True, text=True).stdout


def spotify_left():
    today = datetime.date.today().isoformat()
    try:
        day, used = open(BUDGET_FILE).read().split()
        if day != today:
            return SPOTIFY_DAILY
        return max(0, SPOTIFY_DAILY - int(used))
    except Exception:
        return SPOTIFY_DAILY


def spotify_blocked():
    try:
        return time.time() < float(open(BREAKER_FILE).read().split()[0])
    except Exception:
        return False


def load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


def save_state(st):
    if DRY:
        return
    try:
        tmp = STATE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(st, f, ensure_ascii=False)
        os.replace(tmp, STATE)
    except Exception:
        pass


def due(st, path):
    """Пора ли пробовать: интервал растёт с каждой неудачей."""
    rec = st.get(path)
    if not rec:
        return True
    attempts, last = rec.get("n", 0), rec.get("t", "")
    if attempts >= MAX_ATTEMPTS:
        return False
    try:
        days = (datetime.date.today() - datetime.date.fromisoformat(last)).days
    except Exception:
        return True
    return days >= INTERVALS[attempts]


def audio_count(d):
    try:
        return sum(1 for f in os.listdir(d) if f.lower().endswith(EXT))
    except Exception:
        return 0


def open_library():
    from beets import config as bconf
    from beets import library, plugins
    bconf.read()
    bconf.set_file(LIBCFG)
    try:
        plugins.load_plugins()
    except TypeError:
        plugins.load_plugins(names=bconf["plugins"].as_str_seq())
    plugins.find_plugins()
    # каталог обязателен: в базе пути хранятся относительными и достраиваются
    # им. Без него beets подставит своё умолчание ~/Music, и ни один файл
    # не найдётся.
    return library.Library(bconf["library"].as_path(),
                           bconf["directory"].as_filename())


def below_reason(c, d, it):
    """Человеческое объяснение, почему кандидат не прошёл, — для пульта."""
    if d > SOFT_MAX:
        return "расстояние %.3f при пороге %.2f" % (d, SOFT_MAX)
    extra = sorted(set(c.distance.keys()) - SOFT_PENALTIES)
    if extra:
        return "мешает: %s" % ", ".join(extra)
    if c.info.length is None or not it.length:
        return "у кандидата не указана длина"
    return "длина разошлась на %d с" % abs(float(c.info.length) - float(it.length))


def judge(it):
    """Оценить лучшего кандидата.

    Вернуть (кандидат, как принят, почему отклонён). Кандидат возвращается и
    тогда, когда не прошёл: пульт показывает его в очереди «Как есть», и
    принять его можно одной кнопкой.
    """
    from beets.autotag import match as amatch
    from beets.autotag.match import Recommendation
    try:
        prop = amatch.tag_item(it)
    except Exception as e:
        print("   !! ошибка поиска: %s" % str(e)[:90])
        return None, None, None
    if not prop.candidates:
        return None, None, "ни одного кандидата ни в одном каталоге"
    c = prop.candidates[0]
    d = float(c.distance.distance)
    strong = prop.recommendation == Recommendation.strong
    soft = (d <= SOFT_MAX
            and set(c.distance.keys()) <= SOFT_PENALTIES
            and c.info.length is not None
            and it.length
            and abs(float(c.info.length) - float(it.length)) <= LEN_TOL)
    if not (strong or soft):
        return c, None, below_reason(c, d, it)
    # проверку на чужого исполнителя применяем и к strong: beets там опирается
    # на одну дистанцию, а она на типовых названиях бывает обманчиво низкой
    if not artist_ok(it.artist, c.info.artist):
        return c, None, "чужой исполнитель"
    if strong and not title_ok(it.title, c.info.title):
        return c, None, "другое название"
    return c, "strong" if strong else "имя", None


# снимок полей для журнала: пишем всё, что может переписать apply_metadata,
# иначе откатить ошибочный матч нечем
SNAP_FIELDS = ("artist", "title", "album", "albumartist", "track", "year",
               "mb_trackid", "mb_albumid", "mb_artistid", "data_source",
               "label", "disc", "comp")


def snapshot(it):
    return {f: it.get(f) for f in SNAP_FIELDS}


_con = None


def db():
    global _con
    if _con is None:
        import janitordb
        _con = janitordb.connect()
    return _con


def cand_info(c):
    """Что пульт покажет о кандидате и чем потом его применит."""
    i = c.info
    return {"artist": str(i.artist), "title": str(i.title), "album": str(getattr(i, "album", "") or ""),
            "length": round(float(i.length), 1) if i.length else None,
            "source": str(getattr(i, "data_source", "") or ""),
            "track_id": str(getattr(i, "track_id", "") or ""),
            "distance": round(float(c.distance.distance), 3)}


def ask_asis(it, c, bad):
    """Трек остался «как есть» — вопрос в пульт: принять кандидата или нет."""
    if DRY:
        return
    import janitordb
    path = it.path.decode("utf-8", "replace")
    payload = {"path": path, "item_id": it.id, "artist": str(it.artist), "title": str(it.title),
               "album": str(it.album), "length": round(float(it.length or 0), 1),
               "why": bad, "candidate": cand_info(c) if c is not None else None}
    janitordb.ask(db(), "asis", "asis:" + path, "%s — %s" % (it.artist, it.title), payload)


def apply_match(c, it, why, journal=None):
    d = float(c.distance.distance)
    before = snapshot(it)
    print("   + %.3f [%s] %s - %s  ->  %s - %s (%s)"
          % (d, why, str(it.artist)[:18], str(it.title)[:22],
             str(c.info.artist)[:22], str(c.info.title)[:22],
             getattr(c.info, "data_source", "?")))
    if DRY:
        return
    c.apply_metadata()
    it.store()
    it.try_write()
    import janitordb
    after = snapshot(it)
    after.update({"distance": round(d, 3), "why": why})
    janitordb.log_event(db(), "retry", "match", before, after, item_id=it.id,
                        path=it.path.decode("utf-8", "replace"))
    # если трек стоял в очереди «Как есть» — вопрос снят: он сматчился сам
    db().execute("UPDATE reviews SET status='resolved', decision='matched', decided_at=? "
                 "WHERE key=? AND status='open'",
                 (janitordb.now(), "asis:" + it.path.decode("utf-8", "replace")))


def main():
    budget = spotify_left() // REQ_PER_TRACK
    print("бюджет Spotify позволяет добить примерно %d треков" % budget)
    if budget < 5:
        print("бюджет исчерпан, пропускаю")
        return

    st = load_state()
    today = datetime.date.today().isoformat()

    albums, skipped_dump = [], 0
    for line in beet("ls", "-a", "-f", "$id" + SEP + "$path", "mb_albumid::^$").splitlines():
        p = line.split(SEP)
        if len(p) != 2 or not os.path.isdir(p[1]) or not due(st, p[1]):
            continue
        if audio_count(p[1]) > MAX_ALBUM:
            skipped_dump += 1
            continue
        albums.append(p)

    lib = open_library()
    # «Больше не искать» из пульта: такие треки не тратят бюджет Spotify
    never = set()
    if not DRY or os.path.exists(env.JANITOR_DB):
        never = {r["key"][len("asis:"):] for r in db().execute(
            "SELECT key FROM reviews WHERE kind='asis' AND status='resolved' AND decision='never'")}
    singles, skipped_empty = [], 0
    for it in lib.items("mb_trackid::^$ singleton:true"):
        path = it.path.decode("utf-8", "replace")
        if not os.path.isfile(path) or not due(st, path) or path in never:
            continue
        if not str(it.artist).strip() and not str(it.title).strip():
            skipped_empty += 1
            continue
        singles.append((it, path))

    waiting = sum(1 for v in st.values() if 0 < v.get("n", 0) < MAX_ATTEMPTS)
    dead = sum(1 for v in st.values() if v.get("n", 0) >= MAX_ATTEMPTS)
    print("к попытке сейчас: альбомов %d, синглтонов %d | ждут следующего срока: %d | "
          "признаны безнадёжными: %d (помоек %d, пустых тегов %d)"
          % (len(albums), len(singles), waiting, dead, skipped_dump, skipped_empty))

    def attempt(path):
        """Засчитываем попытку только если Spotify был доступен."""
        if DRY or spotify_blocked():
            return
        rec = st.setdefault(path, {"n": 0, "t": today})
        rec["n"] = rec.get("n", 0) + 1
        rec["t"] = today
        save_state(st)

    used = done = skipped_blocked = matched = rejected = 0

    for aid, path in albums:
        if used >= budget:
            break
        print("-- альбом:", os.path.basename(path))
        if not DRY:
            subprocess.run(["timeout", "900", "beet", "-c", LIBCFG,
                            "import", "-q", "-L", "id:" + aid])
        if spotify_blocked():
            skipped_blocked += 1
        attempt(path)
        done += 1
        used += 5

    asked = 0
    for it, path in singles:
        if used >= budget:
            print("-- бюджет на сегодня исчерпан, остальное завтра")
            break
        c, why, bad = judge(it)
        if c is not None and why:
            apply_match(c, it, why)
            matched += 1
        else:
            if c is not None:
                # кандидат был близок, но не прошёл проверку — показываем,
                # что именно упустили и почему
                print("   ? %s: %s - %s  ->  %s - %s"
                      % (bad, str(it.artist)[:18], str(it.title)[:22],
                         str(c.info.artist)[:22], str(c.info.title)[:22]))
                rejected += 1
            if bad and not spotify_blocked():
                ask_asis(it, c, bad)
                asked += 1
            if spotify_blocked():
                skipped_blocked += 1
            attempt(path)
        done += 1
        used += 1

    print("обработано: %d, сматчено: %d, отклонено: %d, в очередь «как есть»: %d "
          "(без Spotify, попытка не засчитана: %d)"
          % (done, matched, rejected, asked, skipped_blocked))
    stats_out = os.environ.get("JANITOR_STATS_OUT")
    if stats_out and not DRY:
        with open(stats_out, "w", encoding="utf-8") as f:
            json.dump({"processed": done, "matched": matched, "rejected": rejected,
                       "asked": asked, "albums": len(albums)}, f)


if __name__ == "__main__":
    main()
