#!/bin/sh
# Сторож и ночная работа.
#
# Крутится в цикле: замечает новые файлы в INCOMING и импортирует их, а раз в
# сутки после NIGHTLY_HOUR прогоняет всю библиотеку через цепочку починки.
#
# Все пути берутся из окружения; значения по умолчанию — раскладка образа
# linuxserver/beets. Описание переменных — в README и в scripts/env.py.

SCRIPTS=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

INCOMING=${INCOMING_DIR:-/incoming}
LIBRARY=${MUSIC_DIR:-/music}
RESIDUE=${RESIDUE_DIR:-/residue}
CONFIG=${CONFIG_DIR:-/config}

# beets ищет свой конфиг по BEETSDIR: скрипты зовут просто `beet`, и без этой
# переменной он взял бы конфиг из домашней папки, а не наш
BEETSDIR=${BEETSDIR:-$CONFIG}
export BEETSDIR
PYTHONPATH="$SCRIPTS${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH

INTERVAL=${INTERVAL:-300}            # пауза между проверками incoming, секунд
NIGHTLY_HOUR=${NIGHTLY_HOUR:-5}      # с какого часа пытаться начать ночную работу
OWNER=${OWNER:-}                     # chown фонотеки после работы, вида 1000:1000
JUNK_DAYS=${JUNK_DAYS:-3}            # сколько хранить мусор в карантине
DUPES_DAYS=${DUPES_DAYS:-14}         # сколько хранить убранные копии
TMO_ALBUM=${TMO_ALBUM:-900}          # потолок времени на импорт одной папки
TMO_SINGLE=${TMO_SINGLE:-300}        # то же для одиночного файла
SPOOL_DIR=${SPOOL_DIR:-}             # если задать — сюда лягут сводки для уведомлялки

CFG=${CONFIG}/import-single.yaml
ACFG=${CONFIG}/import-album.yaml
LIBCFG=${CONFIG}/library.yaml
STAMP=${CONFIG}/.last-lib-scan
STATE=${CONFIG}/.incoming-state
LOGS=${CONFIG}/nightly

AUDIO="-iname *.mp3 -o -iname *.flac -o -iname *.opus -o -iname *.m4a -o -iname *.ogg -o -iname *.wav -o -iname *.aac -o -iname *.wma"

count_audio() {
  find "$1" -maxdepth 1 -type f \( $AUDIO \) 2>/dev/null | wc -l
}

has_audio_deep() {
  find "$1" -type f \( $AUDIO \) 2>/dev/null | head -1
}

snapshot() {
  n=$(find "$INCOMING" -type f \( $AUDIO \) 2>/dev/null | wc -l)
  s=$(du -sb "$INCOMING" 2>/dev/null | cut -f1)
  echo "$n|$s"
}

net_ok() {
  # Источник может лежать сам по себе: MusicBrainz так отваливался на часы и
  # блокировал импорт целиком, хотя Spotify с Deezer отвечали. Требуем, чтобы
  # откликнулись хотя бы двое из трёх — одиночный сбой работу не
  # останавливает, а настоящий обрыв канала по-прежнему ловится.
  n=0
  curl -s -o /dev/null --max-time 30 "https://musicbrainz.org/ws/2/release?query=test&fmt=json" && n=$((n+1))
  curl -s -o /dev/null --max-time 30 "https://api.deezer.com/search?q=test" && n=$((n+1))
  curl -s -o /dev/null --max-time 30 "https://api.spotify.com/v1/" && n=$((n+1))
  [ "$n" -ge 2 ]
}

run() {
  t=$1
  shift
  # timeout запускает только программы; shell-функцию (library_scan,
  # cleanup_residue) выполняем как есть — у них свои внутренние таймауты
  if type "$1" 2>/dev/null | grep -q "function"; then
    "$@"
  else
    timeout "$t" "$@"
  fi
  rc=$?
  # молчаливые падения — худший вид: неверный ключ команды однажды сломал
  # ночную работу, а лог об этом ничего не сказал
  if [ "$rc" -eq 124 ]; then
    echo "!! таймаут ${t}s: $1"
  elif [ "$rc" -ne 0 ]; then
    echo "!! ОШИБКА (код $rc): $*"
  fi
  LAST_RC=$rc
  return 0
}

jdb() {
  # общая база набора; её недоступность не должна ронять сторожа
  python3 "$SCRIPTS"/janitordb.py "$@" 2>/dev/null
}

step() {
  # step ИМЯ ТАЙМАУТ команда... — то же, что run, но шаг попадает в базу:
  # пульт строит сводку ночи из этих записей, а не из текстового лога
  name=$1
  shift
  sid=$(jdb step-start "$RUN_ID" "$name")
  JANITOR_STATS_OUT=$(mktemp)
  export JANITOR_STATS_OUT
  run "$@"
  case "$LAST_RC" in
    0) st=ok ;;
    124) st=timeout ;;
    *) st=failed ;;
  esac
  [ -n "$sid" ] && jdb step-finish "$sid" "$st" "$(cat "$JANITOR_STATS_OUT" 2>/dev/null)"
  rm -f "$JANITOR_STATS_OUT"
  unset JANITOR_STATS_OUT
}

BROKEN="$RESIDUE/_broken"

playable() {
  # у оборванного файла ffprobe не определит длительность
  ffprobe -v error -show_entries format=duration -of csv=p=0 "$1" 2>/dev/null | grep -q "^[0-9]"
}

quarantine_broken() {
  # вызывается после попыток импорта: всё, что осталось и не читается,
  # уводим из incoming, иначе цикл встанет намертво
  find "$1" -type f \( $AUDIO \) 2>/dev/null | while IFS= read -r f; do
    if ! playable "$f"; then
      rel=${f#"$INCOMING/"}
      dst="$BROKEN/$rel"
      mkdir -p "$(dirname "$dst")" 2>/dev/null
      mv "$f" "$dst" 2>/dev/null && echo "!! битый файл -> карантин: $rel"
    fi
  done
}

cleanup_one() {
  d=$1
  [ -n "$(has_audio_deep "$d")" ] && return 0
  if [ -z "$(find "$d" -type f 2>/dev/null | head -1)" ]; then
    find "$d" -depth -type d -exec rmdir {} + 2>/dev/null
    echo "-- пусто, удалено: $(basename "$d")"
  else
    mkdir -p "$RESIDUE" 2>/dev/null
    mv "$d" "$RESIDUE/" 2>/dev/null && echo "-- остатки -> residue: $(basename "$d")"
  fi
}

cleanup_residue() {
  # мусор (обложки, cue, логи) живёт JUNK_DAYS, убранные дубли — DUPES_DAYS,
  # чтобы был запас времени на откат, если выбран не тот экземпляр
  find "$RESIDUE" -mindepth 1 -maxdepth 1 ! -name "_dupes" -mtime +$JUNK_DAYS -exec rm -rf {} + 2>/dev/null
  find "$RESIDUE/_dupes" -type f -mtime +$DUPES_DAYS -delete 2>/dev/null
  find "$RESIDUE" -mindepth 1 -depth -type d -exec rmdir {} + 2>/dev/null
  # файлы, удалённые по сроку, помечаем в базе — иначе пульт показывал бы их
  # в карантине с кнопкой «Вернуть», которой нечего возвращать
  jdb sync-quarantine
  echo "-- карантин: $(du -sh "$RESIDUE" 2>/dev/null | cut -f1)"
}

process_incoming() {
  [ -n "$OWNER" ] && chown -R "$OWNER" "$INCOMING" 2>/dev/null

  # чинить кодировку тегов НАДО ДО импорта: иначе beets ищет по абракадабре
  # вида "Ìîÿ øëþõà" и трек гарантированно ложится as-is
  run 1800 python3 "$SCRIPTS"/fixenc.py "$INCOMING"

  find "$INCOMING" -mindepth 1 -maxdepth 1 -type f \( $AUDIO \) | while IFS= read -r f; do
    echo "-- single: $f"
    run "$TMO_SINGLE" beet -c "$CFG" import -q -s "$f"
  done

  find "$INCOMING" -mindepth 1 -maxdepth 1 -type d | sort | while IFS= read -r top; do
    find "$top" -type d | sort | while IFS= read -r d; do
      n=$(count_audio "$d")
      [ "$n" -eq 0 ] && continue
      if [ "$n" -le 60 ]; then
        echo "-- album? ($n) $d"
        run "$TMO_ALBUM" beet -c "$ACFG" import -q "$d"
        n=$(count_audio "$d")
      fi
      if [ "$n" -gt 0 ]; then
        echo "-- singles ($n): $d"
        run "$TMO_ALBUM" beet -c "$CFG" import -q -s "$d"
      fi
    done
    quarantine_broken "$top"
    cleanup_one "$top"
  done

  quarantine_broken "$INCOMING"

  # что beets не взял: дубли по «исполнитель + название» и сорвавшийся
  # импорт. Без разбора такой файл лежал бы в incoming вечно, а ночная
  # работа ждёт пустого incoming — так прошли три ночи в сентябре 2026
  run 1800 python3 "$SCRIPTS"/leftovers.py
  find "$INCOMING" -mindepth 1 -maxdepth 1 -type d | while IFS= read -r top; do
    cleanup_one "$top"
  done

  find "$INCOMING" -mindepth 1 -maxdepth 1 -type f | while IFS= read -r f; do
    case "$f" in
      *.mp3|*.flac|*.opus|*.m4a|*.ogg|*.wav|*.aac|*.wma) ;;
      *) mkdir -p "$RESIDUE" 2>/dev/null; mv "$f" "$RESIDUE/" 2>/dev/null ;;
    esac
  done

  find "$INCOMING" -mindepth 1 -depth -type d -exec rmdir {} + 2>/dev/null
  [ -n "$OWNER" ] && chown -R "$OWNER" "$LIBRARY" 2>/dev/null
  # код возврата функции — код последней команды; без этой строки пустой
  # OWNER превращал бы каждый разбор incoming в «упавший» шаг
  return 0
}

library_scan() {
  find "$LIBRARY" -mindepth 1 -type d -mtime -2 | sort | while IFS= read -r d; do
    [ "$(count_audio "$d")" -eq 0 ] && continue
    [ -n "$(beet ls -f x path:"$d" 2>/dev/null | head -1)" ] && continue
    echo "-- lib: $d"
    run "$TMO_ALBUM" beet -c "$LIBCFG" import -q "$d"
  done
  return 0
}

nightly_body() {
  # прогон в общей базе: по нему пульт показывает сводку ночи
  RUN_ID=$(jdb run-start nightly)
  JANITOR_RUN_ID=$RUN_ID
  export JANITOR_RUN_ID

  echo "=== $(date '+%F %T') начало ночной работы"
  echo "-- было: треков $(beet ls -f x | wc -l), без MBID $(beet ls -f x 'mb_trackid::^$' | wc -l), альбомов $(beet ls -a -f x | wc -l)"

  echo "=== $(date '+%F %T') сканирование библиотеки"
  step library_scan 7200 library_scan
  echo "=== $(date '+%F %T') кодировка тегов"
  step fixenc 3600 python3 "$SCRIPTS"/fixenc.py "$LIBRARY"
  step "beet update" 3600 beet update -M
  step fixnames 1800 python3 "$SCRIPTS"/fixnames.py
  echo "=== $(date '+%F %T') добивание as-is"
  step retry 18000 python3 "$SCRIPTS"/retry.py
  echo "=== $(date '+%F %T') нормализация тегов"
  step normalize 3600 python3 "$SCRIPTS"/normalize.py
  echo "=== $(date '+%F %T') сборка развалившихся альбомов"
  step albumgroup 3600 python3 "$SCRIPTS"/albumgroup.py
  echo "=== $(date '+%F %T') дедуп"
  step dedup 1800 python3 "$SCRIPTS"/dedup.py
  echo "=== $(date '+%F %T') один альбом - один экземпляр"
  step consolidate 7200 python3 "$SCRIPTS"/consolidate.py
  if [ -f "$SCRIPTS"/verify.py ]; then
    echo "=== $(date '+%F %T') проверка по превью Spotify"
    step verify 3600 python3 "$SCRIPTS"/verify.py
  fi
  if [ -f "$SCRIPTS"/follow.py ]; then
    echo "=== $(date '+%F %T') новинки исполнителей"
    step follow 3600 python3 "$SCRIPTS"/follow.py
  fi
  echo "=== $(date '+%F %T') обложки"
  step covers 3600 python3 "$SCRIPTS"/covers.py
  echo "=== $(date '+%F %T') чистка карантина"
  step cleanup 600 cleanup_residue
  [ -n "$OWNER" ] && chown -R "$OWNER" "$LIBRARY" 2>/dev/null

  echo "-- стало: треков $(beet ls -f x | wc -l), без MBID $(beet ls -f x 'mb_trackid::^$' | wc -l), альбомов $(beet ls -a -f x | wc -l)"
  echo "-- Spotify: пауза $(cat "$CONFIG/.spotify-pace" 2>/dev/null), потрачено $(cat "$CONFIG/.spotify-budget" 2>/dev/null)"
  echo "-- отказов 429 за ночь: $(grep -c "^$(date '+%F').*429" "$CONFIG/net.log" 2>/dev/null)"
  echo "=== $(date '+%F %T') ночная работа завершена"
  [ -n "$RUN_ID" ] && jdb run-finish "$RUN_ID" ok
  unset JANITOR_RUN_ID
}

nightly() {
  mkdir -p "$LOGS"
  d=$(date '+%F')
  nightly_body 2>&1 | tee -a "$LOGS/$d.log"
  find "$LOGS" -name "*.log" -mtime +30 -delete 2>/dev/null
  # сводка в Telegram: кладём файл в почтовый ящик уведомлялки, она заберёт
  if [ -n "$SPOOL_DIR" ] && [ -d "$SPOOL_DIR" ]; then
    {
      echo "beets, ночь $d"
      grep "^-- " "$LOGS/$d.log" | tail -20
    } > "$SPOOL_DIR/beets-$d.msg" 2>/dev/null
  fi
}

while true; do
  # решения из пульта: пульт сам файлы не трогает, он кладёт действие в
  # очередь, а выполняем его мы — единственный, кто пишет в библиотеку
  if [ -f "$SCRIPTS"/apply_actions.py ]; then
    run 1800 python3 "$SCRIPTS"/apply_actions.py
  fi

  cur=$(snapshot)
  prev=$(cat "$STATE" 2>/dev/null)
  echo "$cur" > "$STATE"
  n=${cur%%|*}

  if [ "$n" -gt 0 ] && [ "$cur" = "$prev" ]; then
    if net_ok; then
      echo "=== $(date '+%F %T') incoming start, файлов: $n"
      RUN_ID=$(jdb run-start incoming)
      JANITOR_RUN_ID=$RUN_ID
      export JANITOR_RUN_ID
      step import 7200 process_incoming
      # свежие импорты — сразу на сверку с превью: подмену в новой закачке
      # лучше увидеть сегодня, а не когда до неё дойдёт ночная очередь
      if [ -f "$SCRIPTS"/verify.py ]; then
        step verify 1800 python3 "$SCRIPTS"/verify.py --new
      fi
      [ -n "$RUN_ID" ] && jdb run-finish "$RUN_ID" ok
      unset JANITOR_RUN_ID
      echo "=== $(date '+%F %T') incoming done"
      echo "" > "$STATE"
    else
      echo "=== $(date '+%F %T') КАНАЛ НЕДОСТУПЕН, импорт пропущен"
    fi
  elif [ "$n" -gt 0 ]; then
    echo "=== $(date '+%F %T') incoming меняется ($cur), жду"
  fi

  # ночная работа: пробуем начиная с 05:00 и повторяем, пока не отработает.
  # отметка о выполнении ставится ТОЛЬКО после успеха, иначе упавший канал
  # в 05:00 отменял бы всю работу на сутки
  today=$(date '+%F')
  # «Запустить сейчас» из пульта: флаг ставит apply_actions.py
  if [ -f "$CONFIG/.run-now" ] && [ "$n" -eq 0 ]; then
    rm -f "$CONFIG/.run-now"
    echo "=== $(date '+%F %T') ночная работа по кнопке из пульта"
    nightly
  fi
  if [ "$(date '+%H')" -ge "$NIGHTLY_HOUR" ] && [ "$(cat "$STAMP" 2>/dev/null)" != "$today" ]; then
    if [ "$n" -eq 0 ] && net_ok; then
      nightly
      echo "$today" > "$STAMP"
    fi
  fi

  sleep "$INTERVAL"
done
