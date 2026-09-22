#!/usr/bin/env python3
"""Вернуть из карантина то, что дублем не является.

Дедуп до 16.08 сравнивал копии по названию, длительности и идентификатору, но
не по звуку. Так в карантин уехали цензурированные издания, инструменталы,
a cappella и целые альтернативные прессинги альбомов: у них совпадают теги,
а аудио разное.

Проверка: снимаем акустический отпечаток и ищем в библиотеке дорожку того же
исполнителя с тем же названием. Если совпадение ниже порога либо пары нет
вовсе — файл не дубль, возвращаем на место и заводим в базу без автоматчинга,
чтобы не переписать теги заново.

Возвращённое потом пересмотрит обычный дедуп: теперь он сверяет звук и
настоящие дубли уберёт снова, а эти оставит.
"""
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dedup  # noqa: E402
import env  # noqa: E402

QUAR = env.DUPES_DIR
MUSIC = env.MUSIC_DIR
EXT = (".mp3", ".flac", ".opus", ".m4a", ".ogg", ".wav", ".aac", ".wma")
DRY = "--dry" in sys.argv


def library_index():
    SEP = "\x01"
    fmt = SEP.join(["$artist", "$title", "$path"])
    out = subprocess.run(["beet", "ls", "-f", fmt], capture_output=True, text=True).stdout
    idx = {}
    for line in out.splitlines():
        p = line.split(SEP)
        if len(p) == 3:
            idx.setdefault((dedup.norm_strict(p[0]), dedup.norm_strict(p[1])), []).append(p[2])
    return idx


def main():
    import mediafile
    idx = library_index()
    back, kept, failed = [], 0, 0

    for root, _, files in os.walk(QUAR):
        for f in files:
            if os.path.splitext(f)[1].lower() not in EXT:
                continue
            src = os.path.join(root, f)
            try:
                m = mediafile.MediaFile(src)
                key = (dedup.norm_strict(m.artist or ""), dedup.norm_strict(m.title or ""))
            except Exception:
                failed += 1
                continue
            cands = idx.get(key) or []
            best = 0.0
            for c in cands[:3]:
                s = dedup.same_audio(c, src)
                if s and s > best:
                    best = s
            if best >= dedup.FP_MIN:
                kept += 1
                continue
            back.append((src, best, len(cands)))

    print("возвращаю файлов: %d | подтверждённых дублей оставляю в карантине: %d | "
          "не прочитались: %d" % (len(back), kept, failed))

    dirs = set()
    for src, best, n in back:
        rel = os.path.relpath(src, QUAR)
        dst = os.path.join(MUSIC, rel)
        why = "пары нет в библиотеке" if not n else "звук совпал лишь на %.0f%%" % (best * 100)
        print("  %-64s %s" % (rel[:64], why))
        if DRY:
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.exists(dst):
            print("     на месте уже что-то есть, пропускаю")
            continue
        shutil.move(src, dst)
        dirs.add(os.path.dirname(dst))

    if DRY or not dirs:
        return
    # заводим в базу как есть: теги у файлов уже проставлены, повторный
    # матчинг только испортит их заново
    for d in sorted(dirs):
        r = subprocess.run(["beet", "-c", env.LIBRARY_CONFIG, "import", "-A", "-q", d],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print("  !! импорт %s: код %d" % (d, r.returncode))
    print("папок заведено в базу: %d" % len(dirs))


if __name__ == "__main__":
    main()
