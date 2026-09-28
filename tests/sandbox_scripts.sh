#!/bin/sh
# Проверка записи в janitor.db у ночных скриптов: consolidate, dedup,
# albumgroup, covers, retry — и возврата из карантина через apply_actions.
# Всё идёт по-настоящему, не вхолостую, но в песочнице: своя фонотека из
# копий двух файлов, своя база beets, своя общая база. Живая библиотека не
# затрагивается.
#
#   sh tests/sandbox_scripts.sh <папка-со-скриптами> <файл1.mp3> <файл2.mp3> <картинка.jpg>
#
# Файл 1 должен быть с настоящими тегами и идентификатором Spotify/Deezer/MB:
# по нему covers найдёт обложку.
set -e
SCRIPTS=$1; F1=$2; F2=$3; IMG=$4
SB=/tmp/sandbox-scripts
rm -rf "$SB"
mkdir -p "$SB/config" "$SB/residue" \
    "$SB/music/Cons Artist/Cons Album" "$SB/music/Non-Album/Dup Artist" \
    "$SB/music/Group Artist/Real Album" "$SB/music/Non-Album/Cover Artist" \
    "$SB/music/Non-Album/Nobody Known"

cat > "$SB/config/config.yaml" <<EOF
directory: $SB/music
library: $SB/config/library.db
plugins: []
import:
    write: no
    copy: no
    move: no
    quiet: yes
    # иначе beets сам пропустит вторую копию и дедупу нечего будет проверять
    duplicate_action: keep
EOF
cp "$SB/config/config.yaml" "$SB/config/library.yaml"

for n in 1 2 3; do cp "$F2" "$SB/music/Cons Artist/Cons Album/0$n.mp3"; done
cp "$F2" "$SB/music/Non-Album/Dup Artist/same a.mp3"
cp "$F2" "$SB/music/Non-Album/Dup Artist/same b.mp3"
for n in 1 2 3; do cp "$F2" "$SB/music/Group Artist/Real Album/0$n.mp3"; done
cp "$F1" "$SB/music/Non-Album/Cover Artist/real.mp3"
cp "$F2" "$SB/music/Non-Album/Nobody Known/ghost.mp3"

export MUSIC_DIR="$SB/music" RESIDUE_DIR="$SB/residue" CONFIG_DIR="$SB/config" BEETSDIR="$SB/config" INCOMING_DIR="$SB/incoming"
export JANITOR_DB="$SB/config/janitor.db" PYTHONPATH="$SCRIPTS" COVERS_LIMIT=10

python3 - "$SB/music" "$IMG" <<'PYEOF'
import os, sys, mediafile
root, img = sys.argv[1], open(sys.argv[2], "rb").read()

def tag(rel, clear_ids=True, **kw):
    m = mediafile.MediaFile(os.path.join(root, rel))
    if clear_ids:
        m.mb_trackid = m.mb_albumid = m.mb_artistid = m.mb_albumartistid = None
    for k, v in kw.items():
        setattr(m, k, v)
    m.save()

# альбом, где первая дорожка лежит дважды — дело для consolidate
for n, title in enumerate(["Alpha", "Beta", "Alpha"], 1):
    tag("Cons Artist/Cons Album/0%d.mp3" % n, artist="Cons Artist", albumartist="Cons Artist",
        title=title, album="Cons Album", track=1 if title == "Alpha" else 2, images=[])
# две одинаковые копии одного сингла в свалке — дело для dedup
for f in ("same a", "same b"):
    tag("Non-Album/Dup Artist/%s.mp3" % f, artist="Dup Artist", albumartist="Dup Artist",
        title="Same Song", album="", track=0, images=[])
# альбом, развалившийся на синглы: у всех одна обложка, название альбома
# уцелело только у первой дорожки — дело для albumgroup
art = [mediafile.Image(data=img, desc=None, type=mediafile.ImageType.front)]
for n, (title, album) in enumerate([("One", "Real Album"), ("Two", "Two"), ("Three", "Three")], 1):
    tag("Group Artist/Real Album/0%d.mp3" % n, artist="Group Artist", albumartist="Group Artist",
        title=title, album=album, track=1 if album == title else n, images=art)
# настоящий трек со снятой обложкой — covers найдёт её по идентификатору
tag("Non-Album/Cover Artist/real.mp3", clear_ids=False, images=[])
# трек, которого нет ни в одном каталоге — covers спросит в пульте
tag("Non-Album/Nobody Known/ghost.mp3", artist="Zzqx Nobody Known", albumartist="Zzqx Nobody Known",
    title="Qqzv Nothing Here", album="", images=[])
PYEOF

beet -c "$SB/config/config.yaml" import -A -q "$SB/music/Cons Artist/Cons Album" >/dev/null 2>&1
for d in "Non-Album/Dup Artist" "Group Artist/Real Album" "Non-Album/Cover Artist" "Non-Album/Nobody Known"; do
    beet -c "$SB/config/config.yaml" import -A -s -q "$SB/music/$d" >/dev/null 2>&1
done

section() { echo; echo "== $1"; rm -f "$SB/st"; }
db() { python3 -c "import sys, janitordb as j; c=j.connect(); [print('   ', tuple(r)) for r in c.execute(sys.argv[1])]" "$1"; }
Q="SELECT reason, similarity, substr(original_path, length('$SB/music/')+1), album_id, substr(kept_path, length('$SB/music/')+1), status FROM quarantine"

section "consolidate"
JANITOR_STATS_OUT=$SB/st python3 "$SCRIPTS/consolidate.py" > "$SB/consolidate.out" 2>&1 || echo "   !! упал, код $?"
grep -iE "traceback|error|ошибк|!!" "$SB/consolidate.out" | grep -v "ошибок: 0" | head -5 | sed 's/^/   /' || true
echo "   статистика: $(cat $SB/st 2>/dev/null)"
db "$Q WHERE original_path LIKE '%Cons Album%'"
db "SELECT script, op, count(*) FROM events WHERE script='consolidate' GROUP BY op"

section "dedup"
python3 "$SCRIPTS/dedup.py" 2>&1 | tail -1 | sed 's/^/   /'
db "$Q WHERE original_path LIKE '%Dup Artist%'"
db "SELECT script, op, item_id IS NOT NULL FROM events WHERE script='dedup'"

section "возврат из карантина: альбомная дорожка — в свой альбом, одиночка — одиночкой"
python3 - <<'PYEOF'
import os, sys, subprocess
sys.path.insert(0, os.environ["PYTHONPATH"])
import janitordb as jdb, retry
con = jdb.connect()
rows = con.execute("SELECT id, original_path, album_id FROM quarantine ORDER BY id").fetchall()
for r in rows:
    jdb.enqueue(con, "restore", {"quarantine_id": r["id"]})
out = subprocess.run([sys.executable, os.path.join(os.environ["PYTHONPATH"], "apply_actions.py")],
                     capture_output=True, text=True)
for line in (out.stdout + out.stderr).strip().splitlines():
    print("    " + line)
lib = retry.open_library()
for r in rows:
    back = [i for i in lib.items() if i.path.decode() == r["original_path"]]
    where = "нет в базе" if not back else ("одиночка" if back[0].album_id is None else "альбом %d" % back[0].album_id)
    print("    %-34s было album_id=%s -> %s" % (os.path.basename(r["original_path"]), r["album_id"], where))
PYEOF

section "albumgroup"
JANITOR_STATS_OUT=$SB/st python3 "$SCRIPTS/albumgroup.py" 2>&1 | tail -1 | sed 's/^/   /'
echo "   статистика: $(cat $SB/st)"
db "SELECT script, op, json_extract(before, '$.album'), json_extract(after, '$.album'), json_extract(after, '$.why') FROM events WHERE script='albumgroup'"

section "covers"
JANITOR_STATS_OUT=$SB/st python3 "$SCRIPTS/covers.py" 2>&1 | grep -E "\+\+|--|обложек" | sed 's/^/   /'
echo "   статистика: $(cat $SB/st)"
db "SELECT kind, status, key, json_extract(payload, '$.count') FROM reviews WHERE kind='cover'"
db "SELECT script, op, json_extract(after, '$.cover') FROM events WHERE script='covers'"
python3 -c "
import mediafile, sys
print('    картинок в real.mp3:', len(mediafile.MediaFile(sys.argv[1]).images))" "$SB/music/Non-Album/Cover Artist/real.mp3"

section "повторный covers: тот же вопрос не задваивается"
python3 "$SCRIPTS/covers.py" >/dev/null 2>&1
db "SELECT count(*) FROM reviews WHERE kind='cover'"

section "retry: вопрос «как есть» и его снятие при матче"
python3 - <<'PYEOF'
import os, sys, types
sys.path.insert(0, os.environ["PYTHONPATH"])
import janitordb as jdb, retry
lib = retry.open_library()
it = [i for i in lib.items() if str(i.title) == "Qqzv Nothing Here"][0]
info = types.SimpleNamespace(artist="Someone Else", title="Other Song", album="Other", length=201.4,
                             data_source="Spotify", track_id="0123456789abcdefghijkl")
cand = types.SimpleNamespace(info=info, distance=types.SimpleNamespace(distance=0.412))
retry.ask_asis(it, cand, "исполнитель не совпал")
retry.ask_asis(it, cand, "исполнитель не совпал")
con = jdb.connect()
r = con.execute("SELECT count(*) n, status, json_extract(payload, '$.candidate.distance') d FROM reviews WHERE kind='asis'").fetchone()
print("    вопросов: %d, статус: %s, расстояние кандидата: %s" % (r["n"], r["status"], r["d"]))

def apply_metadata():
    it.title = "Other Song"
cand.apply_metadata = apply_metadata
retry.apply_match(cand, it, "проверка")
r = con.execute("SELECT status, decision FROM reviews WHERE kind='asis'").fetchone()
e = con.execute("SELECT json_extract(before, '$.title') b, json_extract(after, '$.title') a FROM events WHERE script='retry'").fetchone()
print("    после матча: %s / %s | журнал: %s -> %s" % (r["status"], r["decision"], e["b"], e["a"]))
PYEOF

section "verify: первый запуск только ставит отметку импортов и сверяет фонотеку"
# подмена: звук «2d девочки», а идентификатор — от трека из файла 1
python3 - <<'PYEOF'
import os, sys
sys.path.insert(0, os.environ["PYTHONPATH"])
import retry
lib = retry.open_library()
real = [i for i in lib.items() if i.path.decode().endswith("real.mp3")][0]
ghost = [i for i in lib.items() if i.path.decode().endswith("ghost.mp3")][0]
ghost.mb_trackid = real.mb_trackid
ghost.store()
PYEOF
JANITOR_STATS_OUT=$SB/st python3 "$SCRIPTS/verify.py" 2>&1 | sed 's/^/   /'
echo "   статистика: $(cat $SB/st)"
db "SELECT substr(path, length('$SB/music/')+1), verdict, similarity FROM checks ORDER BY verdict"
db "SELECT kind, status, json_extract(payload, '$.level'), json_extract(payload, '$.similarity'), json_extract(payload, '$.ref.provider') FROM reviews WHERE key LIKE 'verify:%'"
db "SELECT key, value FROM meta WHERE key='imports_seen_until'"

section "leftovers: что beets оставил в incoming"
mkdir -p "$SB/incoming/Some Album"
cp "$SB/music/Non-Album/Dup Artist/same a.mp3" "$SB/incoming/Some Album/copy.mp3"
cp "$F1" "$SB/incoming/Some Album/version.mp3"
cp "$F2" "$SB/incoming/Some Album/stale.mp3"
cp "$F2" "$SB/incoming/Some Album/fresh.mp3"
cp "$SCRIPTS/../config/import-single-keep.yaml" "$SB/config/"
python3 - "$SB/incoming/Some Album" <<'PYEOF'
import os, sys, time, mediafile
d = sys.argv[1]
def tag(f, **kw):
    m = mediafile.MediaFile(os.path.join(d, f))
    m.mb_trackid = None
    for k, v in kw.items():
        setattr(m, k, v)
    m.save()
tag("copy.mp3", artist="Dup Artist", title="Same Song")           # тот же звук, что в библиотеке
tag("version.mp3", artist="Dup Artist", title="Same Song")        # те же теги, другой звук
tag("stale.mp3", artist="Never Imported", title="Stale")          # в библиотеке нет, лежит двое суток
tag("fresh.mp3", artist="Never Imported", title="Fresh")          # в библиотеке нет, только что
old = time.time() - 2 * 86400
os.utime(os.path.join(d, "stale.mp3"), (old, old))
PYEOF
python3 "$SCRIPTS/leftovers.py" 2>&1 | sed 's/^/   /'
echo "   осталось в incoming: $(find "$SB/incoming" -type f | sed "s|$SB/incoming/||" | tr '\n' ' ')"
db "SELECT reason, similarity, substr(original_path, length('$SB/')+1), substr(kept_path, length('$SB/music/')+1) FROM quarantine WHERE original_path LIKE '%incoming%'"
python3 -c "
import sys; sys.path.insert(0, '$SCRIPTS')
import retry
lib = retry.open_library()
print('    «Same Song» в библиотеке:', len([i for i in lib.items() if str(i.title) == 'Same Song']))"

section "импорт из карантина по кнопке пульта"
python3 - <<'PYEOF'
import os, sys, subprocess
sys.path.insert(0, os.environ["PYTHONPATH"])
import janitordb as jdb, retry
con = jdb.connect()
q = con.execute("SELECT id FROM quarantine WHERE reason='duplicate' AND original_path LIKE '%incoming%'").fetchone()
jdb.enqueue(con, "import", {"quarantine_id": q["id"]})
out = subprocess.run([sys.executable, os.path.join(os.environ["PYTHONPATH"], "apply_actions.py")], capture_output=True, text=True)
print("   ", (out.stdout + out.stderr).strip().splitlines()[0])
lib = retry.open_library()
print("    «Same Song» в библиотеке:", len([i for i in lib.items() if str(i.title) == "Same Song"]))
print("    запись карантина:", con.execute("SELECT status FROM quarantine WHERE id=?", (q["id"],)).fetchone()[0])
PYEOF

section "verify --new: свежие импорты — в журнал и на сверку"
JANITOR_STATS_OUT=$SB/st python3 "$SCRIPTS/verify.py" --new 2>&1 | sed 's/^/   /'
echo "   статистика: $(cat $SB/st)"
db "SELECT script, op, json_extract(after, '$.title'), json_extract(after, '$.singleton') FROM events WHERE script='import'"

section "итог по базе"
db "SELECT 'events', count(*) FROM events UNION ALL SELECT 'reviews', count(*) FROM reviews UNION ALL SELECT 'quarantine', count(*) FROM quarantine"
