# -*- coding: utf-8 -*-
"""Единственное место, где живут пути и настройки.

Раньше каждый скрипт носил свои «/music» и «/config» прямо в коде, и перенести
набор на другую машину значило пройтись правкой по девяти файлам. Теперь всё
читается из окружения, а значения по умолчанию совпадают с раскладкой
образа linuxserver/beets — если вы работаете в нём, задавать не нужно ничего.

Переменные (все необязательные):

  MUSIC_DIR       фонотека, куда beets складывает разобранное      /music
  INCOMING_DIR    папка, куда падают новые файлы                   /incoming
  RESIDUE_DIR     карантин: убранные копии и битые файлы           /residue
  CONFIG_DIR      конфиги beets, база, журналы и логи              /config
  JANITOR_DIR     общая база набора и загрузки пульта      CONFIG_DIR/janitor
  LIBRARY_DB      база beets (пульт читает её напрямую)   CONFIG_DIR/library.db
  DOWNTIFY_URL    куда отправлять скачивание           http://downtify:8000
  LOOSE_DIRS      папки-свалки, где лежат одиночные файлы,
                  а не собранные альбомы (через запятую)
  FPCALC          путь к fpcalc из chromaprint                     fpcalc
  SPOTIFY_DAILY   дневной потолок запросов к Spotify               2500
"""
import os


def _dir(name, default):
    return os.environ.get(name, default).rstrip("/") or "/"


MUSIC_DIR = _dir("MUSIC_DIR", "/music")
INCOMING_DIR = _dir("INCOMING_DIR", "/incoming")
RESIDUE_DIR = _dir("RESIDUE_DIR", "/residue")
CONFIG_DIR = _dir("CONFIG_DIR", "/config")

# Карантин делится надвое: копии, которые можно вернуть, и то, что не читается.
DUPES_DIR = os.path.join(RESIDUE_DIR, "_dupes")
BROKEN_DIR = os.path.join(RESIDUE_DIR, "_broken")

# Конфиг beets, которым скрипты открывают библиотеку. Отдельный от основного:
# в нём нет ни плагинов, ни импортных правил — только путь к базе и к файлам,
# чтобы открытие библиотеки не тянуло за собой сеть и плагины.
LIBRARY_CONFIG = os.path.join(CONFIG_DIR, "library.yaml")

# Папка набора: общая база (прогоны, журнал, очередь решений и действий),
# загрузки из пульта, пульс сторожа. Отдельная от конфига beets нарочно:
# пульту она монтируется на запись, а весь остальной CONFIG_DIR — только на
# чтение, так что из пульта не испортить ни базу beets, ни ключи.
JANITOR_DIR = _dir("JANITOR_DIR", os.path.join(CONFIG_DIR, "janitor"))
JANITOR_DB = os.environ.get("JANITOR_DB", os.path.join(JANITOR_DIR, "janitor.db"))
UPLOADS_DIR = os.path.join(JANITOR_DIR, "uploads")
# сторож пишет сюда, чем занят; пульт показывает это в подвале меню
HEARTBEAT = os.path.join(JANITOR_DIR, "heartbeat")
# пульт касается этого файла, положив действие: сторож не ждёт конца
# пятиминутной паузы и выполняет его сразу
WAKE = os.path.join(JANITOR_DIR, "wake")
RUN_FLAG = os.path.join(JANITOR_DIR, "run-now")

# База beets. Скрипты внутри beets-watch открывают её через beets, а пульт и
# разбор дискографии читают напрямую, только на чтение.
LIBRARY_DB = os.environ.get("LIBRARY_DB", os.path.join(CONFIG_DIR, "library.db"))

# downtify: куда отправлять скачивание. Файлы он кладёт в incoming сам.
DOWNTIFY_URL = os.environ.get("DOWNTIFY_URL", "http://downtify:8000").rstrip("/")

# Служебные файлы. Держим рядом с базой: их содержимое бессмысленно без неё.
JOURNAL = os.path.join(CONFIG_DIR, "applied.jsonl")
RETRY_STATE = os.path.join(CONFIG_DIR, ".retried")
CONSOLIDATE_LOG = os.path.join(CONFIG_DIR, "consolidate.log")
CONSOLIDATE_JOURNAL = os.path.join(CONFIG_DIR, "consolidate.journal")
DUPES_LOG = os.path.join(CONFIG_DIR, "dupes.log")
NET_LOG = os.path.join(CONFIG_DIR, "net.log")

# Счётчики обращений к Spotify: бюджет на сутки, темп и предохранитель.
# Их пишет sitecustomize.py, а читает retry.py — поэтому имена общие.
SPOTIFY_BUDGET_FILE = os.path.join(CONFIG_DIR, ".spotify-budget")
SPOTIFY_PACE_FILE = os.path.join(CONFIG_DIR, ".spotify-pace")
SPOTIFY_BREAKER_FILE = os.path.join(CONFIG_DIR, ".spotify-breaker")
SPOTIFY_CACHE_DB = os.path.join(CONFIG_DIR, "spotify-cache.db")
SPOTIFY_DAILY = int(os.environ.get("SPOTIFY_DAILY", "2500"))

# Свалки одиночных файлов. Дедуп обращается с ними иначе, чем с альбомами:
# лишнюю копию из свалки убрать можно, а выдернуть дорожку из собранного
# альбома нельзя — останется покалеченный релиз.
LOOSE_DIRS = tuple(
    "/%s/" % p.strip("/ ")
    for p in os.environ.get(
        "LOOSE_DIRS", "Non-Album,TelegramMusic,_Unofficial,_Unmatched"
    ).split(",")
    if p.strip("/ ")
)

# fpcalc из chromaprint: по умолчанию ищем в PATH, а не по жёсткому пути —
# в разных образах он лежит то в /usr/bin, то в /usr/local/bin.
FPCALC = os.environ.get("FPCALC", "fpcalc")

AUDIO_EXT = (".mp3", ".flac", ".opus", ".m4a", ".ogg", ".wav", ".aac", ".wma")


def in_music(path):
    """Путь относительно фонотеки — для читаемых сообщений в логах."""
    prefix = MUSIC_DIR + "/"
    return path[len(prefix):] if path.startswith(prefix) else path
