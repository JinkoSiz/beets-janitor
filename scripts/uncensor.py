#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Цензура стримингов: заглушённые строчки возвращаются из оригинальных загрузок.

Русские лейблы перезаливают на стриминги мастеры, где заглушены упоминания
наркотиков — «грязь», «таблы», «кодеин», «косяк»; мат при этом остаётся.
Мастер общий для Spotify, Deezer и YouTube Music, поэтому downtify приносит
уже заглушённый звук, пометка explicit ничего не значит, а сверка по превью
(verify.py) цензуру не видит: превью заглушено так же. На двух альбомах
OG Buda так оказалось 11 треков из 41 (2026-10-22).

Оригинал почти всегда остаётся на YouTube в загрузках времени релиза: у
лейбла (S&P Records выложил оба альбома через неделю после выхода) и на
каналах премьер (RUS TRACKS). Их и берём в эталоны:

1. Ищем на YouTube «исполнитель название». Отбрасываем speed up, slowed,
   инструменталы, реакции и разборы, а также автоматические арт-треки
   (канал «… - Topic» или канал исполнителя с голым названием трека) — это
   тот же стриминговый мастер. Длина — от файла минус 3 с до плюс 25 с: у
   премьер заставка в 3 с и свой хвост.
2. Скачиваем звук в низком качестве (m4a 49 кбит/с, около 0.8 МБ): для
   сравнения хватает, а трафика через VPN вчетверо меньше.
3. Сравниваем с файлом: сдвиг по отпечатку, затем шагами по 0.1 с громкость
   и отпечаток окнами по 0.37 с. Загрузка, совпадающая с файлом меньше чем
   на 87%, — не та запись (другой мастеринг даёт 70–85%), она не эталон.
     заглушено   файл тише эталона вдвое и больше, подряд от 0.3 с, — или
                 тише только голос (300–3400 Гц), от 0.2 с, а бит играет
     заменено    отпечаток расходится подряд без провала громкости
                 (писк или реверс вместо слова)
     громче      наоборот, файл громче эталона: оригинал у нас, а заглушён
                 эталон
   Первые 5 с не сравниваются ни у кого: в начале у загрузок бывают метки
   каналов и теги продюсеров. У загрузок другой длины (премьер) не
   сравниваются и последние 10 с: хвост там свой, на шести премьерах из
   шести он расходился с файлом.

Решение:
  - место нашлось у двух эталонов — цензура подтверждена. Если один из них
    идёт без сдвига и той же длины, при censor_auto=1 его звук встаёт на
    место файла сразу — действием replace: старый файл в карантин, теги и
    обложка остаются, откат — из журнала. Формат — как у заменяемого файла:
    mp3 перекодируется в 320 кбит/с, opus и m4a кладутся как есть (иначе
    Navidrome делит альбом надвое, см. codec_for). Иначе — вопрос в пульт;
  - место нашлось только у одного эталона — вопрос в пульт с обеими
    версиями: сам скрипт по одному источнику не меняет;
  - файл громче эталонов и нигде не тише — это и есть оригинал (original):
    сверка по превью перестаёт по нему спрашивать;
  - эталонов нет — no_ref, повтор через 30 дней.
Lossless (FLAC и подобное) сам не меняется никогда, только вопросом:
звук с YouTube сжат, и решать, что важнее, должен человек.

YouTube после сотни-другой запросов подряд просит подтвердить, что мы не
бот, — прогон тогда сразу останавливается: downtify ходит через тот же
выход, и его закачки важнее. Отсюда и лимит censor_limit дорожек за ночь.

yt-dlp живёт в CONFIG_DIR/pylib и обновляется раз в неделю: YouTube ломает
старые версии за пару месяцев. Запускается отдельным процессом без нашего
sitecustomize — через перехватчик сети его поиск получал пустой ответ.

  uncensor.py                 свежие импорты, затем остальная фонотека, не
                              больше censor_limit дорожек
  uncensor.py --item N        проверить одну дорожку и подробно показать
  uncensor.py --item N --file ПУТЬ
                              то же для другого файла с тегами дорожки N —
                              проверка метода на старых файлах из карантина
  uncensor.py --mark N ...    отметить дорожки, уже исправленные вручную
  uncensor.py --dry           ничего не заменять и не записывать
"""
import array
import collections
import difflib
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests  # noqa: E402

import discography as disco  # noqa: E402
import env  # noqa: E402
import janitordb as jdb  # noqa: E402
import verify  # noqa: E402

DRY = "--dry" in sys.argv or "--file" in sys.argv

PYLIB = os.path.join(env.CONFIG_DIR, "pylib")
PYLIB_STAMP = os.path.join(PYLIB, ".updated")
REFRESH_DAYS = 7

SEARCH_N = 8              # результатов поиска на дорожку
CANDIDATES = 3            # загрузок скачиваем и сравниваем, не больше
SHORTER, LONGER = 3, 25   # насколько загрузка может быть короче / длиннее файла, с
# общее сходство: та же запись. Премьеры с заставкой дают 89–93%, разные
# песни — около 55%, другой мастеринг той же песни — 70–85%
SAME_REC = 0.87
# звуком загрузки заменяем только при таком сходстве. Сравнение идёт по m4a
# 49 кбит/с, и загрузки лейбла дают на нём 92.8–95.5% (на opus — 93–98%)
SOURCE_SIM = 0.92
SAME_SHIFT = 0.3          # загрузка той же формы: сдвиг не больше, с
SAME_LEN = 1.5            # и разница длины не больше, с
TAIL = 10.0               # у загрузок другой формы хвост не сравниваем, с
# начало и конец у загрузок бывают свои — метка канала, тег продюсера, —
# поэтому первые и последние секунды не сравниваем и у загрузок той же формы
HEAD, SAME_TAIL = 5.0, 3.0
# места двух эталонов совпадают с таким допуском, с. На всех подтверждённых
# случаях места перекрывались; при 0.6 склеивались соседние строчки
AGREE = 0.2
MAX_CUT = 0.15            # «вырезано» больше этой доли трека — не цензура, спросить
PAUSE = 2.0               # между обращениями к YouTube, с
DEADLINE = 170 * 60       # прогон не дольше, с: ночью есть и другая работа
RECHECK = {"failed": 1, "no_ref": 30, "replacing": 2}
LOSSLESS = {"FLAC", "ALAC", "WAV", "AIFF", "APE", "WAVPACK"}

FR = 0.1238               # секунд на значение отпечатка fpcalc
STEP = 0.1                # шаг огибающей громкости, с
SR = 8000
QUIET = 0.45              # тише эталона в столько раз и меньше — заглушено
LOUD_MIN = 800            # тишину эталона с тишиной файла не сравниваем
MIN_RUN = 3               # подряд шагов, чтобы место засчиталось
FP_LOW = 0.65             # отпечаток окном в 3 значения ниже этого — заменено
# голос отдельно от бита: полоса речи и порог относительно обычного для
# трека отношения голоса файла к голосу эталона. Вместе с проверкой всей
# полосы 0.5 на 10 заглушённых оригиналах OG Buda из карантина нашёл ещё
# полтора десятка мест («lean», «опиаты», «курю паровозы»), а на заменённых
# файлах и 7 чистых треках других исполнителей — ни одного; 0.6 даёт лишние.
# Вставки вроде «(грязь)» в «Крит» звучат 0.2 с, поэтому голосу хватает двух
# шагов подряд; с 0.55 так нашлись ещё «pour up», «гикаю на заднем»,
# «дилеры», а на 19 заменённых файлах по-прежнему ни одного места (2026-10-25)
VOICE = "highpass=f=300,lowpass=f=3400"
VQUIET = 0.55
VMIN_RUN = 2
VFLOOR = 0.3              # голос эталона тише медианы в столько раз — там не поют

LOW_FMT = "139/233/worstaudio"
HIGH_FMT = "251/140/bestaudio"

# «Sign in to confirm your age» — не блок, а возрастное ограничение одной
# загрузки: её пропускаем. Раньше оно останавливало прогон каждую ночь на
# одной и той же дорожке, и очередь не двигалась (2026-10-23, 2026-10-24)
BOT = re.compile(r"not a bot|sign in to confirm(?! your age)|HTTP Error 429|too many requests", re.I)
JUNK = re.compile(
    r"sped ?up|speed ?up|slowed|reverb|nightcore|\b8d\b|bass ?boost|instrumental|инструментал|минус|"
    r"karaoke|караоке|acapella|a ?cappella|\bcover\b|кавер|remix|ремикс|reaction|реакция|разбор|обзор|"
    r"type ?beat|snippet|сниппет|\blive\b|лайв|концерт|tutorial|mashup|мэшап|\b1 ?hour\b|\b1 ?час\b|"
    r"без мата|\bclean\b|перевод|translation|tik ?tok|тик ?ток", re.I)
CYR = re.compile("[А-Яа-яЁё]")
VA = {"various artists", "various", "va", "сборник", "разные исполнители"}


class Blocked(Exception):
    """YouTube просит подтвердить, что мы не бот, — на сегодня хватит."""


# ---------------------------------------------------------------- yt-dlp
def ensure_ytdlp():
    """yt-dlp в своей папке: есть и не старше недели — иначе ставим заново."""
    have = os.path.isdir(os.path.join(PYLIB, "yt_dlp"))
    try:
        fresh = time.time() - os.path.getmtime(PYLIB_STAMP) < REFRESH_DAYS * 86400
    except OSError:
        fresh = False
    if have and fresh:
        return True
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--no-cache-dir",
                        "--disable-pip-version-check", "--upgrade", "--target", PYLIB, "yt-dlp[default]"],
                       capture_output=True, text=True, timeout=900)
    if r.returncode == 0 and os.path.isdir(os.path.join(PYLIB, "yt_dlp")):
        open(PYLIB_STAMP, "w").close()
        print("-- yt-dlp обновлён: %s" % ytdlp(["--version"]).stdout.strip())
        return True
    print("!! yt-dlp не обновился: %s%s" % ((r.stderr or r.stdout).strip()[-200:],
                                             " — работаю на прежнем" if have else ""))
    return have


def ytdlp(args, timeout=240):
    """yt-dlp отдельным процессом: свои библиотеки, без нашего sitecustomize."""
    e = dict(os.environ, PYTHONPATH=PYLIB)
    cmd = [sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-warnings", "--no-progress",
           "--socket-timeout", "30"]
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    if proxy:
        cmd += ["--proxy", proxy]
    r = subprocess.run(cmd + args, capture_output=True, text=True, env=e, timeout=timeout)
    if r.returncode != 0 and BOT.search(r.stderr or ""):
        raise Blocked((r.stderr or "").strip().splitlines()[-1][:160])
    return r


def search(query):
    r = ytdlp(["--flat-playlist", "-J", "ytsearch%d:%s" % (SEARCH_N, query)], timeout=120)
    try:
        return (json.loads(r.stdout or "{}") or {}).get("entries") or []
    except ValueError:
        return []


def download(vid, fmt, folder, tag):
    """Звук загрузки в folder, файл называется tag.<расширение>. None — не вышло."""
    ytdlp(["-f", fmt, "--no-playlist", "-o", os.path.join(folder, tag + ".%(ext)s"), "--", vid])
    for f in os.listdir(folder):
        p = os.path.join(folder, f)
        if f.startswith(tag + ".") and not f.endswith(".part") and os.path.getsize(p) > 10000:
            return p
    return None


# ---------------------------------------------------------------- что проверять
def names_of(it):
    return [n for n in disco.artist_names(it.artist) + disco.artist_names(it.albumartist)
            if disco.norm_name(n) not in VA]


def russian(items):
    """Русскоязычные дорожки и всё у русских исполнителей: у OG Buda есть и
    STARWE, и YTPO, цензура у них та же.

    Русский исполнитель — у кого кириллицей заметная доля треков: от
    четверти или хотя бы три и от 4% (Big Baby Tape пишет названия латиницей,
    кириллических у него 6 из 113). Пара русских фитов зарубежного артиста не
    делает: у BONES их 9 из 1019, у Eminem 3 из 676. И судим по главному
    исполнителю трека, а не по соавторам."""
    total, cyr = collections.Counter(), collections.Counter()
    for it in items:
        ru = bool(CYR.search("%s %s %s" % (it.artist, it.title, it.album)))
        for k in {k for n in names_of(it) for k in disco.name_keys(n)}:
            total[k] += 1
            cyr[k] += ru
    keys = {k for k, n in cyr.items() if n / total[k] >= 0.25 or (n >= 3 and n / total[k] >= 0.04)}

    def lead(it):
        n = disco.primary_artist(it.albumartist)
        if not n or disco.norm_name(n) in VA:
            n = disco.primary_artist(it.artist)
        return disco.name_keys(n)

    return [it for it in items if CYR.search("%s %s %s" % (it.artist, it.title, it.album)) or lead(it) & keys]


def due(row, size):
    """Проверять ли: не проверяли, файл заменили или пора повторить."""
    if row is None or row["size"] != size:
        return True
    days = RECHECK.get(row["verdict"])
    return days is not None and verify.days_since(row["checked_at"]) >= days


def query_of(it):
    artist = disco.primary_artist(it.artist) or disco.primary_artist(it.albumartist)
    title = re.sub(r"\s*[\(\[]\s*(?:feat|ft)\.?[^\)\]]*[\)\]]", "", str(it.title), flags=re.I)
    # «Каждый День (▰˘◡˘▰)»: со значками поиск YouTube не находил ничего
    title = re.sub(r"[\(\[][^\w\)\]]*[\)\]]", " ", title)
    title = re.sub(r"[^\w\s'&.,!?-]", " ", title)
    return re.sub(r"\s+", " ", "%s %s" % (artist, title)).strip()


def mentions(title, want):
    """Название трека есть в названии загрузки — с поправкой на написание:
    лейбл выложил «b4nditsky», а в тегах «b4nditskiy»."""
    if not want:
        return True
    for t, w in ((disco.norm_name(title), want),
                 (disco.norm_name(title).translate(disco.TRANSLIT), want.translate(disco.TRANSLIT))):
        if w in t:
            return True
        tw, n = t.split(), len(w.split())
        if any(difflib.SequenceMatcher(None, " ".join(tw[i:i + n]), w).ratio() >= 0.85
               for i in range(max(1, len(tw) - n + 1))):
            return True
    return False


def candidates(it, entries, length):
    """Загрузки, годные в эталоны, в порядке выдачи YouTube."""
    own = "%s %s" % (it.title, it.album)
    names = set()
    for n in names_of(it):
        names |= disco.name_keys(n)
    want = disco.norm_title(it.title)
    out = []
    for e in entries:
        vid, title = e.get("id"), str(e.get("title") or "")
        channel = str(e.get("channel") or e.get("uploader") or "")
        dur = e.get("duration")
        if not vid or not dur or not length - SHORTER <= float(dur) <= length + LONGER:
            continue
        if not mentions(title, want):
            continue
        # пометка версии считается, только если её нет в самом треке
        if any(not re.search(re.escape(m), own, re.I) for m in JUNK.findall(title)):
            continue
        # арт-трек: тот же стриминговый мастер, эталоном быть не может
        if channel.endswith(" - Topic") or (
                disco.name_keys(channel) & names and disco.title_keys(title) & disco.title_keys(it.title)):
            continue
        out.append({"id": vid, "title": title, "channel": channel, "duration": float(dur)})
    return out


# ---------------------------------------------------------------- сравнение
def pcm(path, af=None):
    raw = subprocess.run(["ffmpeg", "-v", "quiet", "-i", path] + (["-af", af] if af else []) +
                         ["-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
                         capture_output=True, timeout=300).stdout
    return array.array("h", raw)


def envelope(path, af=None):
    a = pcm(path, af)
    n = int(STEP * SR)
    return [(sum(x * x for x in a[i:i + n]) / n) ** 0.5 for i in range(0, len(a) - n + 1, n)]


def align(a, b):
    """Лучший сдвиг b относительно a (в значениях отпечатка) и сходство при нём."""
    best = (-1.0, 0)
    for off in range(-int(SHORTER / FR) - 8, int(LONGER / FR) + 8):
        n = err = 0
        for i in range(0, len(a), 4):
            j = i + off
            if 0 <= j < len(b):
                err += verify._bits(a[i] ^ b[j])
                n += 1
        if n > 50:
            best = max(best, (1 - err / (32.0 * n), off))
    return best


def runs(idx, gap=1, min_len=MIN_RUN):
    """Подряд идущие номера (с пропуском до gap) длиной от min_len."""
    out, cur = [], []
    for i in idx:
        if cur and i - cur[-1] > gap + 1:
            if len(cur) >= min_len:
                out.append((cur[0], cur[-1]))
            cur = []
        cur.append(i)
    if len(cur) >= min_len:
        out.append((cur[0], cur[-1]))
    return out


def merged(spans):
    """Пересекающиеся и соседние отрезки — в один."""
    out = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def touches(span, others):
    return any(o[0] - 0.5 <= span[0] <= o[1] + 0.5 or o[0] - 0.5 <= span[1] <= o[1] + 0.5 for o in others)


def voice_quiet(lv, cv, eo, lo, hi):
    """Шаги, где голос файла заметно тише голоса эталона, хотя бит играет.

    По всей полосе такое место тише меньше чем вдвое и прежде не ловилось:
    «на грязном» в «Гикаю, Мажет» выключено, а громкость упала до 0.6–0.7
    (2026-10-24). Сравниваем полосу речи и не напрямую, а против обычного
    для трека отношения: загрузки бывают громче файла на весь трек."""
    pairs = [(i, a, cv[i + eo]) for i, a in enumerate(lv) if lo <= i * STEP <= hi and 0 <= i + eo < len(cv)]
    if not pairs:
        return set()
    floor = VFLOOR * statistics.median(b for _, _, b in pairs)
    ratios = [(i, a / b) for i, a, b in pairs if b > floor]
    if not ratios:
        return set()
    med = statistics.median(r for _, r in ratios)
    return {i for i, r in ratios if r < VQUIET * med}


def compare(lib, cand_path):
    """Сравнить файл (lib: отпечаток, огибающая, голос, длина) с загрузкой."""
    cf = verify.fingerprint(cand_path)
    if len(cf) < 40:
        return None
    sim, off = align(lib["fp"], cf)
    ce = envelope(cand_path)
    shift = off * FR
    cand_len = len(ce) * STEP
    same = abs(shift) <= SAME_SHIFT and abs(cand_len - lib["len"]) <= SAME_LEN
    # общая часть без начала; у загрузок другой формы — и без длинного хвоста
    lo = max(0.0, -shift) + HEAD
    hi = min(lib["len"], cand_len - shift) - (SAME_TAIL if same else TAIL)
    eo = int(round(shift / STEP))
    le = lib["env"]
    quiet, loud = [], []
    for i, a in enumerate(le):
        j = i + eo
        if not lo <= i * STEP <= hi or not 0 <= j < len(ce):
            continue
        b = ce[j]
        if b > LOUD_MIN and a < QUIET * b:
            quiet.append(i)
        elif a > LOUD_MIN and b < QUIET * a:
            loud.append(i)
    vq = voice_quiet(lib["voice"], envelope(cand_path, VOICE), eo, lo, hi)
    quiet = sorted(set(quiet) | vq)
    lf = lib["fp"]
    low = []
    for i in range(len(lf) - 3):
        if not lo <= i * FR <= hi - 3 * FR or not 0 <= i + off <= len(cf) - 3:
            continue
        if 1 - sum(verify._bits(lf[k] ^ cf[k + off]) for k in range(i, i + 3)) / 96.0 < FP_LOW:
            low.append(i)
    quiet_s = [(a * STEP, (b + 1) * STEP)
               for a, b in merged(runs(quiet) + runs(sorted(vq), gap=0, min_len=VMIN_RUN))]
    loud_s = [(a * STEP, (b + 1) * STEP) for a, b in runs(loud)]
    other_s = [s for s in ((a * FR, (b + 3) * FR) for a, b in runs(low)) if not touches(s, quiet_s + loud_s)]
    return {"sim": sim, "shift": shift, "len": cand_len, "same": same,
            "quiet": quiet_s, "loud": loud_s, "other": other_s}


def decide(refs):
    """По сравнениям с эталонами: вердикт, подтверждённые места, одиночные.

    Место — (начало, конец, вид, {номера эталонов}). Подтверждено, если его
    показали хотя бы два эталона. Одиночное место поднимает вопрос, только
    если его показал эталон, годный в источник: загрузки похуже (видео с
    текстом, премьеры) шумят и могут лишь подтвердить чужую находку.
    """
    marks = []
    for i, c in enumerate(refs):
        marks += [(a, b, "заглушено", {i}) for a, b in c["quiet"]]
        marks += [(a, b, "заменено", {i}) for a, b in c["other"]]
    spots = []
    for s in sorted(marks, key=lambda m: m[0]):
        if spots and s[0] - spots[-1][1] <= AGREE:
            a, b, kind, who = spots[-1]
            spots[-1] = (a, max(b, s[1]), kind if kind == "заглушено" else s[2], who | s[3])
        else:
            spots.append(s)
    sure = [s for s in spots if len(s[3]) >= 2]
    single = [s for s in spots if len(s[3]) < 2 and any(refs[i]["sim"] >= SOURCE_SIM for i in s[3])]
    if sure:
        verdict = "censored"
    elif single:
        verdict = "suspect"
    elif any(c["loud"] for c in refs):
        verdict = "original"
    else:
        verdict = "clean"
    return verdict, sure, single


def pick_source(refs, spots):
    """Эталон, чьим звуком заменять или предложить заменить: показавший
    больше всего мест, при равенстве — той же формы и ближе к файлу.
    Возвращает (эталон, показал ли он все места) или (None, False)."""
    scored = sorted(((sum(1 for s in spots if i in s[3]), c["same"] and c["sim"] >= SOURCE_SIM, c["sim"], i)
                     for i, c in enumerate(refs)), reverse=True)
    if not scored or not scored[0][0]:
        return None, False
    return refs[scored[0][3]], scored[0][0] == len(spots)


def fits(src, covers_all):
    """Звуком этого эталона можно заменить без вопроса."""
    return src is not None and covers_all and src["same"] and src["sim"] >= SOURCE_SIM


# ---------------------------------------------------------------- текст
def lyric_lines(it, spots):
    """Строчки синхронного текста lrclib, на которые пришлись места: для
    пульта и журнала, чтобы было видно, что вырезано. Не нашлось — пусто."""
    j = None
    for _ in range(2):
        try:
            r = requests.get("https://lrclib.net/api/search", timeout=20, params={
                "artist_name": disco.primary_artist(it.artist), "track_name": str(it.title)})
            j = r.json() if r.ok else None
        except Exception:
            j = None
        if isinstance(j, list):
            break
        time.sleep(3)
    synced = next((x.get("syncedLyrics") for x in j or [] if x.get("syncedLyrics")), "") if isinstance(j, list) else ""
    lines = []
    for line in synced.splitlines():
        m = re.match(r"\[(\d+):(\d+(?:\.\d+)?)\]\s*(.*)", line)
        if m and m.group(3).strip():
            lines.append((int(m.group(1)) * 60 + float(m.group(2)), m.group(3).strip()))
    out = []
    for a, b, _, _ in spots:
        # строчка, что звучит в начале места, и следующая, если место на неё заходит
        cur = [t for t in lines if t[0] <= a + 0.3][-1:]
        nxt = [t for t in lines if a + 0.3 < t[0] <= b + 0.3][:1]
        out.append(" / ".join(t[1] for t in cur + nxt))
    return out


# ---------------------------------------------------------------- файлы
def decode_errors(path):
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "null", "-"],
                       capture_output=True, text=True, timeout=300)
    return len((r.stderr or "").strip().splitlines()) if r.returncode == 0 else -1


def duration(path):
    r = subprocess.run(["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                       capture_output=True, text=True, timeout=60)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


def codec_for(f, like):
    """Расширение и кодек замены — в формате заменяемого файла.

    Альбом из mp3 с одной дорожкой в opus Navidrome показывает двумя
    альбомами: beets пишет в Vorbis-теги ещё и YEAR, Navidrome берёт его как
    дату релиза («2023» против «2023-12-08» у mp3), а дата релиза входит в
    признак альбома. Так «POX VAWË» раскололся надвое 2026-10-22. Поэтому
    mp3 заменяется mp3 320 кбит/с — тем же, во что downtify перекодирует
    звук YouTube, — а opus и m4a перекладываются без перекодирования."""
    src = ".opus" if f.endswith(".webm") else os.path.splitext(f)[1].lower()
    want = os.path.splitext(like)[1].lower()
    if want == ".mp3":
        return ".mp3", ["-c:a", "libmp3lame", "-b:a", "320k"]
    if want == ".m4a" and src != ".m4a":
        return ".m4a", ["-c:a", "aac", "-b:a", "256k"]
    return src, ["-c:a", "copy"]


def prepare(src, lib, tmp, like):
    """Звук эталона в хорошем качестве — в папку загрузок пульта, откуда его
    берёт действие replace, в формате заменяемого файла like (см. codec_for).
    У загрузки другой формы отрезаются заставка и хвост."""
    length = lib["len"]
    f = download(src["id"], HIGH_FMT, tmp, "hi")
    if f is None:
        return None
    ext, codec = codec_for(f, like)
    os.makedirs(env.UPLOADS_DIR, exist_ok=True)
    dst = os.path.join(env.UPLOADS_DIR, "uncensor-%s-%d%s" % (src["id"], int(time.time()), ext))
    cmd = ["ffmpeg", "-v", "error", "-y"]
    if not src["same"] and src["shift"] > 0.05:
        cmd += ["-ss", "%.3f" % src["shift"]]
    cmd += ["-i", f, "-vn", "-map_metadata", "-1"] + codec
    if not src["same"]:
        cmd += ["-t", "%.3f" % length]
    subprocess.run(cmd + [dst], capture_output=True, timeout=300)
    # в исходнике с YouTube бывает битый пакет; перекладка его отбрасывает,
    # и готовый файл обязан читаться без единой ошибки
    dur = duration(dst) if os.path.isfile(dst) else None
    ok = dur is not None and abs(dur - length) <= 2.0 and decode_errors(dst) == 0
    if ok:
        # хорошее качество — та же запись, что сравнивалась в плохом
        c = compare(lib, dst)
        ok = c is not None and c["sim"] >= SAME_REC and abs(c["shift"]) <= SAME_SHIFT
    if not ok:
        if os.path.exists(dst):
            os.remove(dst)
        return None
    return dst


def cleanup_uploads(con, days=7):
    """Звук эталонов, который больше не нужен: вопрос решён, замена сделана."""
    if not os.path.isdir(env.UPLOADS_DIR):
        return
    used = set()
    for r in con.execute("SELECT payload FROM reviews WHERE kind='censored' AND status='open'"):
        used.add((jdb.loads(r["payload"], {}) or {}).get("file"))
    for r in con.execute("SELECT payload FROM actions WHERE kind='replace' AND status IN ('pending','running')"):
        used.add((jdb.loads(r["payload"], {}) or {}).get("file"))
    for f in os.listdir(env.UPLOADS_DIR):
        p = os.path.join(env.UPLOADS_DIR, f)
        if f.startswith("uncensor-") and p not in used and time.time() - os.path.getmtime(p) > days * 86400:
            os.remove(p)


# ---------------------------------------------------------------- проверка
def mmss(t):
    return "%d:%04.1f" % (t // 60, t % 60)


def brief(c):
    return {"id": c["id"], "channel": c["channel"], "title": c["title"],
            "url": "https://www.youtube.com/watch?v=" + c["id"], "sim": round(c["sim"], 3),
            "shift": round(c["shift"], 2), "same": c["same"]}


def check(con, it, auto, path=None, verbose=False):
    """Проверить одну дорожку. Вернуть вердикт."""
    path = path or it.path.decode("utf-8", "replace")
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    name = "%s — %s" % (it.artist, it.title)
    lib = {"fp": verify.fingerprint(path), "env": envelope(path), "voice": envelope(path, VOICE)}
    lib["len"] = len(lib["env"]) * STEP
    if len(lib["fp"]) < 40 or lib["len"] < 20:
        if not DRY:
            jdb.censor_set(con, it.id, path, size, "failed")
        return "failed"

    entries = search(query_of(it))
    time.sleep(PAUSE)
    cands = candidates(it, entries, lib["len"])
    if verbose:
        print("   поиск «%s»: %d результатов, годных в эталоны %d" % (query_of(it), len(entries), len(cands)))
    tmp = tempfile.mkdtemp(prefix="uncensor-")
    refs, tried = [], []
    try:
        for e in cands[:CANDIDATES]:
            f = download(e["id"], LOW_FMT, tmp, "%d" % len(tried))
            time.sleep(PAUSE)
            tried.append(e["id"])
            c = compare(lib, f) if f else None
            if c is None:
                continue
            c.update(e)
            if verbose:
                print("   %-11s %5.1f%% сдвиг %+5.1f с %-13s | %-18s %s" % (
                    e["id"], c["sim"] * 100, c["shift"], "той же формы" if c["same"] else "другой формы",
                    e["channel"][:18], e["title"][:50]))
                for key, word in (("quiet", "файл тише"), ("other", "звук другой"), ("loud", "файл громче")):
                    if c[key]:
                        print("               %s: %s" % (word, ", ".join("%s–%s" % (mmss(a), mmss(b)) for a, b in c[key])))
            if c["sim"] < SAME_REC:
                continue
            refs.append(c)
            # двух согласных эталонов хватает — третий качать незачем
            if len(refs) >= 2:
                verdict, sure, _ = decide(refs)
                if verdict in ("clean", "original") or (verdict == "censored" and fits(*pick_source(refs, sure))):
                    break

        if not refs:
            if not DRY:
                jdb.censor_set(con, it.id, path, size, "no_ref", refs=tried)
            if verbose:
                print("   эталонов нет")
            return "no_ref"

        verdict, sure, single = decide(refs)
        if verdict in ("clean", "original"):
            if not DRY:
                jdb.censor_set(con, it.id, path, size, verdict, refs=[brief(c) for c in refs])
            if verbose:
                print("   %s (эталонов %d)" % ("чисто" if verdict == "clean" else "у нас оригинал", len(refs)))
            return verdict

        src, covers_all = pick_source(refs, sure or single)
        # одиночные места при подтверждённой цензуре — только те, что показал
        # и эталон-источник: эталон похуже (видео с текстом) шумит местами,
        # которых нет больше ни у кого, и в карточке они только мешают
        keep = single if verdict == "suspect" else \
            [s for s in single if src is not None and refs.index(src) in s[3]]
        spots_raw = sorted(sure + keep, key=lambda s: s[0])
        lines = lyric_lines(it, spots_raw)
        spots = [{"t0": round(a, 1), "t1": round(b, 1), "kind": kind, "sure": len(who) >= 2, "line": line}
                 for (a, b, kind, who), line in zip(spots_raw, lines)]
        cut = sum(b - a for a, b, _, _ in sure)
        lossless = str(it.format).upper() in LOSSLESS
        can_auto = (auto and verdict == "censored" and fits(src, covers_all)
                    and cut <= MAX_CUT * lib["len"] and not lossless)
        where = "; ".join("%s–%s%s" % (mmss(s["t0"]), mmss(s["t1"]), " «%s»" % s["line"] if s["line"] else "")
                          for s in spots)
        if DRY:
            print("%s %s: %s -> %s" % ("!!" if verdict == "censored" else "? ", name, where,
                                        "заменил бы звуком %s (%s)" % (src["id"], src["channel"]) if can_auto
                                        else "спросил бы в пульте"))
            return verdict

        file = prepare(src, lib, tmp, path) if src else None
        source = brief(src) if src else None
        refs_b = [brief(c) for c in refs]
        if can_auto and file:
            jdb.enqueue(con, "replace", {"item_id": it.id, "file": file, "by": "uncensor",
                                         "censor": {"spots": spots, "source": source}})
            jdb.censor_set(con, it.id, path, size, "replacing", spots, refs_b, source)
            print("!! цензура  %s: %s -> заменю звуком %s (%s)" % (name, where, src["id"], src["channel"]))
            return "replacing"

        why = ("lossless сам не меняю" if lossless else
               "место показал только один эталон" if verdict == "suspect" else
               "нет загрузки той же длины без сдвига" if not fits(src, covers_all) else
               "вырезано подозрительно много" if cut > MAX_CUT * lib["len"] else
               "звук эталона не скачался" if not file else "автозамена выключена")
        payload = {"item_id": it.id, "path": path, "artist": str(it.artist), "title": str(it.title),
                   "album": str(it.album), "length": round(lib["len"], 1), "format": str(it.format),
                   "spots": spots, "source": source, "file": file, "refs": refs_b, "why": why,
                   "sure": verdict == "censored"}
        status, decision = jdb.ask(con, "censored", "censor:%d:%d" % (it.id, size), name, payload)
        if status in ("resolved", "dismissed") and decision != "replace":
            # об этом файле уже спрашивали, и решили оставить
            jdb.censor_set(con, it.id, path, size, "kept", spots, refs_b, source)
            return "kept"
        jdb.censor_set(con, it.id, path, size, verdict, spots, refs_b, source)
        print("?  цензура  %s: %s -> вопрос в пульт (%s)" % (name, where, why))
        return verdict
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- запуск
def mark(con, lib, ids):
    """Отметить дорожки, звук которых уже заменён оригиналом вручную."""
    for iid in ids:
        it = lib.get_item(iid)
        if it is None:
            print("   нет дорожки %d" % iid)
            continue
        path = it.path.decode("utf-8", "replace")
        jdb.censor_set(con, it.id, path, os.path.getsize(path), "replaced", source={"note": "заменено вручную"})
        # вопросы сверки по превью к этим файлам — следствие той же цензуры
        con.execute("UPDATE reviews SET status='resolved', decision='uncensored', decided_at=? "
                    "WHERE key LIKE ? AND status='open'", (jdb.now(), "verify:%d:%%" % it.id))
        print("   %d %s — %s: заменено вручную" % (it.id, it.artist, it.title))


def main():
    import retry
    lib = retry.open_library()
    con = jdb.connect()

    if "--mark" in sys.argv:
        mark(con, lib, [int(a) for a in sys.argv[sys.argv.index("--mark") + 1:] if a.isdigit()])
        return

    if not ensure_ytdlp():
        sys.exit("!! yt-dlp недоступен — проверка цензуры пропущена")

    auto = jdb.setting(con, "censor_auto") == "1"
    if "--item" in sys.argv:
        iid = int(sys.argv[sys.argv.index("--item") + 1])
        it = lib.get_item(iid)
        if it is None:
            sys.exit("нет дорожки %d" % iid)
        path = sys.argv[sys.argv.index("--file") + 1] if "--file" in sys.argv else None
        print("%s — %s  [%s]" % (it.artist, it.title, path or it.path.decode("utf-8", "replace")))
        print("   итог: %s" % check(con, it, auto, path=path, verbose=True))
        return

    if jdb.setting(con, "censor_enabled") != "1":
        print("проверка цензуры выключена в настройках")
        return
    if not DRY:
        cleanup_uploads(con)

    items = [it for it in lib.items("") if os.path.isfile(it.path.decode("utf-8", "replace"))]
    ru = russian(items)
    rows = {r["item_id"]: r for r in con.execute("SELECT * FROM censor")}

    def size(it):
        try:
            return os.path.getsize(it.path.decode("utf-8", "replace"))
        except OSError:
            return None

    # свежие импорты первыми: цензура в новой закачке важнее старой фонотеки
    queue = sorted((it for it in ru if due(rows.get(it.id), size(it))), key=lambda i: -float(i.added or 0))
    limit = int(float(jdb.setting(con, "censor_limit")))
    take = queue[:limit]
    print("русских дорожек: %d из %d, к проверке: %d, останется на следующие ночи: %d"
          % (len(ru), len(items), len(take), len(queue) - len(take)))

    stats = collections.Counter()
    started = time.time()
    try:
        for it in take:
            if time.time() - started > DEADLINE:
                print("-- время прогона вышло, остальное в следующую ночь")
                break
            try:
                v = check(con, it, auto)
            except Blocked:
                raise
            except Exception as e:
                print("!! %s — %s: %s" % (it.artist, it.title, str(e)[:160]))
                v = "failed"
                if not DRY:
                    jdb.censor_set(con, it.id, it.path.decode("utf-8", "replace"), size(it), "failed")
            if v:
                stats[v] += 1
                stats["checked"] += 1
    except Blocked as e:
        print("!! YouTube просит подтвердить, что мы не бот (%s) — остальное в следующую ночь" % e)
        stats["blocked"] = 1

    stats["backlog"] = len(queue) - stats["checked"]
    print("проверено: %d | чисто %d, оригинал у нас %d | цензура: заменено %d, спрошено %d | "
          "без эталона %d, сбоев %d" % (stats["checked"], stats["clean"], stats["original"], stats["replacing"],
                                        stats["censored"] + stats["suspect"], stats["no_ref"], stats["failed"]))
    stats_out = os.environ.get("JANITOR_STATS_OUT")
    if stats_out and not DRY:
        with open(stats_out, "w", encoding="utf-8") as f:
            json.dump(dict(stats), f)


if __name__ == "__main__":
    main()
