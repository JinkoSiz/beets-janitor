#!/bin/sh
# Проверка apply_actions.py в песочнице: своя фонотека из двух копий файлов,
# своя база beets, своя общая база. Живая библиотека не затрагивается.
#
#   sh tests/sandbox_apply_actions.sh <папка-со-скриптами> <файл1.mp3> <файл2.mp3> <картинка.jpg>
set -e
SCRIPTS=$1; F1=$2; F2=$3; IMG=$4
SB=/tmp/sandbox-janitor
rm -rf "$SB"
mkdir -p "$SB/config/uploads" "$SB/music/Test Artist/Test Album" "$SB/residue"

cat > "$SB/config/config.yaml" <<EOF
directory: $SB/music
library: $SB/config/library.db
plugins: []
import:
    write: no
    copy: no
    move: no
    quiet: yes
EOF
cp "$SB/config/config.yaml" "$SB/config/library.yaml"

cp "$F1" "$SB/music/Test Artist/Test Album/01 first.mp3"
cp "$F2" "$SB/music/Test Artist/Test Album/02 second.mp3"
cp "$IMG" "$SB/config/uploads/cover.jpg"

export MUSIC_DIR="$SB/music" RESIDUE_DIR="$SB/residue" CONFIG_DIR="$SB/config" BEETSDIR="$SB/config"
export JANITOR_DB="$SB/config/janitor.db" PYTHONPATH="$SCRIPTS"

# у копий свои теги (это разные синглы) — пропишем общий альбом прямо в
# файлы, иначе проверки «один альбом в папке» и «возврат в тот же альбом»
# проверяли бы не то, что нужно
python3 - "$SB/music/Test Artist/Test Album" <<'PYEOF'
import os, sys, mediafile
d = sys.argv[1]
for n, f in enumerate(sorted(os.listdir(d)), 1):
    m = mediafile.MediaFile(os.path.join(d, f))
    m.album, m.albumartist, m.artist = "Test Album", "Test Artist", "Test Artist"
    m.title, m.track, m.images = "Track %d" % n, n, []
    m.save()
PYEOF

# альбом из двух дорожек, без поиска совпадений
beet -c "$SB/config/config.yaml" import -A -q "$SB/music/Test Artist/Test Album" >/dev/null 2>&1

python3 - <<'PYEOF'
import os, sys, subprocess
sys.path.insert(0, os.environ["PYTHONPATH"])
import janitordb as jdb, retry
S = os.environ["PYTHONPATH"]
con = jdb.connect()
lib = retry.open_library()
items = sorted(lib.items(), key=lambda i: i.path)
print("в песочнице дорожек: %d, альбомов: %d" % (len(items), len(list(lib.albums()))))
first, second = items[0], items[1]
album_before = second.album_id

def run(title):
    out = subprocess.run([sys.executable, os.path.join(S, "apply_actions.py")], capture_output=True, text=True)
    print("== %s" % title)
    for line in (out.stdout + out.stderr).strip().splitlines():
        print("   " + line)

def last_action():
    r = con.execute("SELECT status, result FROM actions ORDER BY id DESC LIMIT 1").fetchone()
    return r["status"], r["result"]

# 1. карантин второй дорожки
jdb.enqueue(con, "quarantine", {"item_id": second.id, "reason": "duplicate", "similarity": 0.93, "kept": first.path.decode()})
run("карантин")
q = con.execute("SELECT * FROM quarantine").fetchone()
lib = retry.open_library()
print("   файл в карантине: %s | в базе beets: %s | запись: %s %s"
      % (os.path.exists(q["path"]), lib.get_item(second.id) is not None, q["reason"], q["similarity"]))

# 2. возврат — и попадание обратно в тот же альбом
jdb.enqueue(con, "restore", {"quarantine_id": q["id"]})
run("возврат")
lib = retry.open_library()
back = [i for i in lib.items() if i.path.decode().endswith("02 second.mp3")]
print("   файл на месте: %s | в базе: %d | в том же альбоме: %s"
      % (os.path.exists(q["original_path"]), len(back), bool(back) and back[0].album_id == album_before))

# 3. откат правки тегов
it = lib.get_item(first.id)
old = str(it.title)
it.title = "ИСПОРЧЕНО"; it.store()
eid = jdb.log_event(con, "consolidate", "retitle", {"title": old}, {"title": "ИСПОРЧЕНО"}, item_id=it.id, path=it.path.decode())
jdb.enqueue(con, "rollback", {"event_id": eid})
run("откат тегов")
lib = retry.open_library()
print("   название вернулось: %s" % (str(lib.get_item(first.id).title) == old))
jdb.enqueue(con, "rollback", {"event_id": eid})
run("повторный откат той же записи")
print("   ", last_action())

# 4. обложка
jdb.enqueue(con, "set_cover", {"item_ids": [i.id for i in lib.items()], "image": os.path.join(os.environ["CONFIG_DIR"], "uploads", "cover.jpg")})
run("обложка")
import mediafile
print("   картинок во втором файле: %d | cover.jpg в папке: %s"
      % (len(mediafile.MediaFile(back[0].path.decode()).images), os.path.exists(os.path.join(os.path.dirname(back[0].path.decode()), "cover.jpg"))))

# 5. попытки выйти за пределы
jdb.enqueue(con, "quarantine", {"path": "/etc/passwd"})
jdb.enqueue(con, "set_cover", {"item_ids": [first.id], "image": "/etc/hostname"})
jdb.enqueue(con, "quarantine", {"path": os.environ["MUSIC_DIR"] + "/../config/janitor.db"})
jdb.enqueue(con, "frobnicate", {})
run("попытки выйти за пределы")
for r in con.execute("SELECT kind, status, result FROM actions ORDER BY id DESC LIMIT 4"):
    print("   %-10s %-7s %s" % (r["kind"], r["status"], r["result"]))
PYEOF
