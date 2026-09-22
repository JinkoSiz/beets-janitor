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
  timeout "$t" "$@"
  rc=$?
  # молчаливые падения — худший вид: неверный ключ команды однажды сломал
  # ночную работу, а лог об этом ничего не сказал
  if [ "$rc" -eq 124 ]; then
    echo "!! таймаут ${t}s: $1"
  elif [ "$rc" -ne 0 ]; then
    echo "!! ОШИБКА (код $rc): $*"
  fi
  return 0
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
  find "$INCOMING" -mindepth 1 -maxdepth 1 -type f | while IFS= read -r f; do
    case "$f" in
      *.mp3|*.flac|*.opus|*.m4a|*.ogg|*.wav|*.aac|*.wma) ;;
      *) mkdir -p "$RESIDUE" 2>/dev/null; mv "$f" "$RESIDUE/" 2>/dev/null ;;
    esac
  done

  find "$INCOMING" -mindepth 1 -depth -type d -exec rmdir {} + 2>/dev/null
  [ -n "$OWNER" ] && chown -R "$OWNER" "$LIBRARY" 2>/dev/null
}

library_scan() {
  find "$LIBRARY" -mindepth 1 -type d -mtime -2 | sort | while IFS= read -r d; do
    [ "$(count_audio "$d")" -eq 0 ] && continue
    [ -n "$(beet ls -f x path:"$d" 2>/dev/null | head -1)" ] && continue
    echo "-- lib: $d"
    run "$TMO_ALBUM" beet -c "$LIBCFG" import -q "$d"
  done
}

nightly_body() {
  echo "=== $(date '+%F %T') начало ночной работы"
  echo "-- было: треков $(beet ls -f x | wc -l), без MBID $(beet ls -f x 'mb_trackid::^$' | wc -l), альбомов $(beet ls -a -f x | wc -l)"

  echo "=== $(date '+%F %T') сканирование библиотеки"
  library_scan
  echo "=== $(date '+%F %T') кодировка тегов"
  run 3600 python3 "$SCRIPTS"/fixenc.py "$LIBRARY"
  run 3600 beet update -M
  run 1800 python3 "$SCRIPTS"/fixnames.py
  echo "=== $(date '+%F %T') добивание as-is"
  run 18000 python3 "$SCRIPTS"/retry.py
  echo "=== $(date '+%F %T') нормализация тегов"
  run 3600 python3 "$SCRIPTS"/normalize.py
  echo "=== $(date '+%F %T') сборка развалившихся альбомов"
  run 3600 python3 "$SCRIPTS"/albumgroup.py
  echo "=== $(date '+%F %T') дедуп"
  run 1800 python3 "$SCRIPTS"/dedup.py
  echo "=== $(date '+%F %T') один альбом - один экземпляр"
  run 7200 python3 "$SCRIPTS"/consolidate.py
  echo "=== $(date '+%F %T') обложки"
  run 3600 python3 "$SCRIPTS"/covers.py
  echo "=== $(date '+%F %T') чистка карантина"
  cleanup_residue
  [ -n "$OWNER" ] && chown -R "$OWNER" "$LIBRARY" 2>/dev/null

  echo "-- стало: треков $(beet ls -f x | wc -l), без MBID $(beet ls -f x 'mb_trackid::^$' | wc -l), альбомов $(beet ls -a -f x | wc -l)"
  echo "-- Spotify: пауза $(cat "$CONFIG/.spotify-pace" 2>/dev/null), потрачено $(cat "$CONFIG/.spotify-budget" 2>/dev/null)"
  echo "-- отказов 429 за ночь: $(grep -c "^$(date '+%F').*429" "$CONFIG/net.log" 2>/dev/null)"
  echo "=== $(date '+%F %T') ночная работа завершена"
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
  cur=$(snapshot)
  prev=$(cat "$STATE" 2>/dev/null)
  echo "$cur" > "$STATE"
  n=${cur%%|*}

  if [ "$n" -gt 0 ] && [ "$cur" = "$prev" ]; then
    if net_ok; then
      echo "=== $(date '+%F %T') incoming start, файлов: $n"
      process_incoming
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
  if [ "$(date '+%H')" -ge "$NIGHTLY_HOUR" ] && [ "$(cat "$STAMP" 2>/dev/null)" != "$today" ]; then
    if [ "$n" -eq 0 ] && net_ok; then
      nightly
      echo "$today" > "$STAMP"
    fi
  fi

  sleep "$INTERVAL"
done
