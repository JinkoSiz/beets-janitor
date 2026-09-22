# Nightly library repair for beets

A set of scripts that keeps a [beets](https://beets.io) library in order
without manual triage: imports what arrives, retries what came in
unidentified, reassembles albums that fell apart, removes duplicates and
fetches the artwork beets won't fetch on its own.

It runs *alongside* stock beets — not a fork, not a patch. beets stays
untouched.

Written against a live 8,000-track library that had been assembled over years
from mismatched sources and had accumulated every kind of mess a music library
can accumulate.

## Why, when beets already exists

beets is excellent at filing music that arrives with decent tags. The trouble
starts after that, and for most of it there is no built-in answer:

| What you see in the player | Why it happens | What fixes it |
|---|---|---|
| One album split into five single-track entries | the release has an empty `albumartist`, so the player falls back to the track artist | `normalize.py` |
| The same album listed two or three times | two pressings in the library, each with its own album object | `consolidate.py` |
| A track appears twice inside one album | copies came from different sources and got different IDs | `dedup.py`, `consolidate.py` |
| "Disc 0" and "Disc 1" on a single-disc album | some tracks carry no disc number, others carry 1 | `consolidate.py` |
| Part of a compilation drifts into Various Artists | collaborations are tagged differently from solo tracks | `normalize.py`, `consolidate.py` |
| Empty square instead of cover art on singletons | `fetchart` only serves albums — it never reaches standalone tracks | `covers.py` |
| Tags that read `Ìîÿ øëþõà` | file written in cp1251, read back as latin-1 | `fixenc.py` |
| A track sits untagged for years | the import fell a fraction short of the match threshold | `retry.py` |
| An album is assembled but its tracks disagree on its name | some tracks matched a single release instead of the album | `albumgroup.py` |

## How it works

A single watcher (`nightly.sh`) runs in a loop:

* new files land in `incoming` → tag encoding is repaired → they are imported
  (a folder is tried as an album first, then file by file);
* once a day after `NIGHTLY_HOUR` the whole library goes through the chain:

```
library_scan     pick up folders that appeared in the library bypassing incoming
fixenc.py        tag encoding
beet update -M   re-read tags from disk
fixnames.py      filenames to the beets template
retry.py         retry tracks that were filed as-is
normalize.py     artist names: collapsed credits, casing, homoglyphs
albumgroup.py    reassemble broken-up albums by embedded cover art
dedup.py         drop duplicates confirmed by acoustic fingerprint
consolidate.py   one album, one copy
covers.py        artwork
cleanup_residue  prune the quarantine
```

The order is deliberate. Encoding is fixed **before** import: otherwise beets
searches for a match against mojibake and the track is guaranteed to land
as-is. Normalisation runs before album reassembly, reassembly before dedup,
dedup before merging pressings — each step relies on the previous one having
already settled the tags.

## Why it is safe to run

These scripts touch your library, so:

* **Dry runs.** Almost every script takes `--dry`: it prints what it would do
  and writes nothing. Start there.
* **Quarantine instead of deletion.** Nothing is deleted. Duplicates move to
  `RESIDUE_DIR/_dupes`, unreadable files to `_broken`, and they stay there for
  `DUPES_DAYS` (two weeks by default). `unquarantine.py` brings them back.
* **Journals.** Every tag edit and every file move is appended as a JSON line
  to `applied.jsonl` and `consolidate.journal` — before and after, so you can
  reverse any of it by hand.
* **Fingerprints decide, not filenames.** Before calling two tracks copies,
  the scripts compare acoustic fingerprints via `fpcalc`. Titles and durations
  lie: "Scary Movies (Yonderboi remix)" and "Scary Movies (Future Type Joint
  remix)" differ by one parenthesis, while one and the same recording drifts
  by seconds between encodings.
* **Doubt favours keeping.** If a fingerprint can't be taken, or similarity
  lands in the grey zone, the file stays where it is and goes into the report.

## Install

You need Docker and an image with beets, ffmpeg and `fpcalc` from chromaprint
— `lscr.io/linuxserver/beets` will do.

```bash
git clone https://github.com/JinkoSiz/beets-janitor.git
cd beets-janitor
cp .env.example .env
$EDITOR .env                       # fill in the Spotify keys
cp docker-compose.example.yml docker-compose.yml
$EDITOR docker-compose.yml         # point the volumes at your library
docker compose up -d
docker compose logs -f
```

On first start the configs are expanded from `config/*.template` into
`CONFIG_DIR` with the environment substituted in. Files that already exist are
left alone, so hand edits survive restarts; to overwrite them anyway, run
`render-config.py --force`.

Spotify credentials come from
[developer.spotify.com/dashboard](https://developer.spotify.com/dashboard) →
Create app. Without them the plugin silently finds nothing.

### Without Docker

```bash
export MUSIC_DIR=/srv/music INCOMING_DIR=/srv/incoming \
       RESIDUE_DIR=/srv/residue CONFIG_DIR=~/.config/beets \
       BEETSDIR=~/.config/beets PYTHONPATH=$PWD/scripts
python3 scripts/render-config.py
sh scripts/nightly.sh
```

Individual scripts can be run by hand — with `--dry` first:

```bash
python3 scripts/consolidate.py --dry
```

## Configuration

Everything except the Spotify keys is optional; the defaults match the
linuxserver image layout.

| Variable | Default | Meaning |
|---|---|---|
| `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` | — | Spotify credentials |
| `MUSIC_DIR` | `/music` | the library |
| `INCOMING_DIR` | `/incoming` | where new files land |
| `RESIDUE_DIR` | `/residue` | quarantine |
| `CONFIG_DIR` | `/config` | configs, database, journals |
| `BEETSDIR` | `= CONFIG_DIR` | where beets looks for its own config |
| `LOOSE_DIRS` | `Non-Album,TelegramMusic,_Unofficial,_Unmatched` | dump folders (see below) |
| `INTERVAL` | `300` | how often to check `incoming`, seconds |
| `NIGHTLY_HOUR` | `5` | earliest hour to start the nightly run |
| `JUNK_DAYS` / `DUPES_DAYS` | `3` / `14` | quarantine retention |
| `TMO_ALBUM` / `TMO_SINGLE` | `900` / `300` | import timeouts, seconds |
| `SPOTIFY_DAILY` | `2500` | daily request ceiling |
| `OWNER` | empty | `chown` the library afterwards, e.g. `1000:1000` |
| `SPOOL_DIR` | empty | if set, nightly summaries are dropped here |
| `FPCALC` | `fpcalc` | path to `fpcalc` if it isn't on `PATH` |

**`LOOSE_DIRS`** are folders holding standalone files rather than assembled
albums. The distinction matters to the dedup: a spare copy sitting in a dump
folder can be removed, but pulling a track out of a complete pressing cannot —
that would leave a crippled release behind.

## What's inside

| File | What it does |
|---|---|
| `nightly.sh` | the watcher and the nightly chain |
| `env.py` | the one place where paths and settings live |
| `render-config.py` | expands configs from templates, substituting the environment |
| `fixenc.py` | repairs tag encoding (cp1251 read as latin-1) |
| `fixnames.py` | brings filenames in line with the beets template |
| `retry.py` | retries tracks that were filed as-is |
| `normalize.py` | artist names: collapsed credits, casing, homoglyphs, empty `albumartist` |
| `albumgroup.py` | reassembles broken-up albums by embedded cover art |
| `dedup.py` | removes duplicates in two passes, confirmed by fingerprint |
| `consolidate.py` | one album, one copy: pressings, discs, compilation fragments |
| `covers.py` | artwork for singletons and for albums `fetchart` gave up on |
| `unquarantine.py` | restores anything quarantined by mistake |
| `sitecustomize.py` | paces Spotify requests, caches responses, trips a breaker |

### About `sitecustomize.py`

Python picks this up on its own as long as the scripts directory is on
`PYTHONPATH`. It wraps outbound calls: keeps a daily Spotify request budget,
slows down at the first sign of throttling, caches responses in SQLite and
takes the source offline for a few hours after a run of failures. Without it a
large library walks straight into HTTP 429 on its first night.

## What it does not do

* It does not download music. Whatever you put in `incoming` is the input.
* It does not edit what it isn't sure about. Ambiguous cases go into the
  report, not under the knife.
* It does not replace or patch beets. It sits next to it; beets stays stock.
* It does not verify that a file contains the recording its tags claim. If a
  source handed you the wrong audio with the right tags, these scripts won't
  notice — they compare copies against each other, not against a reference.

## Things worth knowing about beets

Two or three findings from building this, in case you go digging yourself:

* `strong_rec_thresh` and `medium_rec_thresh` are **distance** thresholds, not
  similarity: lower means stricter. And `rec_gap_thresh` can only **lower** a
  recommendation, when two candidates sit close together — it can never raise
  one. Hence the classic "I tuned the thresholds and nothing changed".
* `max_rec` caps the recommendation regardless of distance. With
  `missing_tracks: medium`, a release with missing tracks will never be a
  strong match, however perfectly everything else lines up.
* `fetchart` only serves albums. A standalone track will never get artwork, no
  matter how often you rescan — which is what `covers.py` exists for.

## A note on language

The README is in English; the comments in the code are in Russian. They
explain *why* each decision was made rather than what a line does, which is
where most of the value sits — translating them is on the list, but a machine
pass would cost more than it gives.

## License

MIT, same as beets itself.
