# Nightly library repair for beets

A set of scripts that keeps a [beets](https://beets.io) library in order
without manual triage: imports what arrives, retries what came in
unidentified, reassembles albums that fell apart, removes duplicates, fetches
the artwork beets won't fetch on its own, checks that each file really
contains the recording its tags claim, and watches your artists for new
releases.

Whatever the scripts are not sure about goes to a web panel instead of under
the knife: listen to both versions, pick one, and the decision is remembered.

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
| Right title, wrong song | the downloader grabbed a different recording and beets matched it by name | `verify.py` |
| A re-download of another version never arrives | beets skips anything with the same artist and title, and the file blocks `incoming` | `leftovers.py` |
| You find out about a new album a month late | nothing is watching | `follow.py` |

## How it works

A single watcher (`nightly.sh`) runs in a loop:

* new files land in `incoming` → tag encoding is repaired → they are imported
  (a folder is tried as an album first, then file by file) → whatever beets
  refused is sorted out by sound (`leftovers.py`) → fresh imports are checked
  against a preview of the release they were matched to (`verify.py --new`);
* decisions made in the panel are carried out between cycles (`apply_actions.py`);
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
verify.py        does the audio match the release? (a few hundred tracks a night)
follow.py        new releases from the artists you follow
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
* **Journal.** Every tag edit and every file move is recorded in a shared
  SQLite database (`JANITOR_DIR/janitor.db`) — before and after — and can be
  rolled back from the panel. Older plain-text journals are imported on first
  run (`janitordb.py import-legacy`).
* **Fingerprints decide, not filenames.** Before calling two tracks copies,
  the scripts compare acoustic fingerprints via `fpcalc`. Titles and durations
  lie: "Scary Movies (Yonderboi remix)" and "Scary Movies (Future Type Joint
  remix)" differ by one parenthesis, while one and the same recording drifts
  by seconds between encodings.
* **Doubt favours keeping.** If a fingerprint can't be taken, or similarity
  lands in the grey zone, the file stays where it is and becomes a question
  in the panel. Nothing is quarantined because of a suspected wrong recording
  until you say so.

## The panel

A small web app (Django + htmx) for the part that needs a human:

| Page | What it is for |
|---|---|
| Summary | last night's chain step by step, what is waiting for you, Spotify budget, latest imports and their check results |
| Decisions | suspected wrong recordings, uncertain duplicate pairs, tracks filed as-is — with players for both versions and the similarity scale |
| New releases | what the artists you follow put out; download or skip |
| Artists | who is followed (collected from the library automatically, plus your own), and picking the right one when the name is ambiguous |
| Discography | type an artist, pick the right one, download everything you don't have yet |
| Covers | drop an image onto an album nobody has artwork for |
| Quarantine | listen to what was removed and bring it back |
| Journal | every change with before/after, and a rollback button |
| Settings | thresholds, schedule, retention |

**It never touches your files.** Every decision is written to the shared
database as an action, and the watcher container — the only thing that writes
to the library — carries it out within seconds. The panel mounts the library,
the quarantine and the beets config **read-only**; the only thing it can
write is its own folder (`JANITOR_DIR`). A compromised panel can't delete
your music or write to the beets database — but it can read the beets
config, Spotify keys included, so treat the password seriously.

Put it behind a reverse proxy with HTTPS and set `PANEL_PASSWORD`; without a
password it lets nobody in. Repeated wrong passwords from one address are
throttled.

## How "already have it" is decided

Both the new-release watcher and the discography page answer the same
question — which of these tracks do you already have — and they answer it in
this order (the order matters):

1. **Same ISRC already seen in this discography** → a repeat; a single that is
   also on the album arrives with the album.
2. **Same ISRC in your library** → you have it. The ISRC is the recording's
   passport: one recording keeps one ISRC on a single, an album and a
   compilation.
3. **Version marker** — "Instrumental", "Live", "Remix", "Sped up"… → a
   different version, off by default. The marker wins over the fingerprint:
   a rap instrumental and the vocal version score 86–96% on a chromaprint
   comparison, because the fingerprint follows pitch and the vocal barely
   changes it. If only *your* copy is marked (you have the live version), the
   studio one is offered.
4. **Same title and length seen earlier in the discography** → a repeat
   (deluxe editions, singles from the album).
5. **Same title and length in your library but a different ISRC** → labels
   re-issue the same recording under new numbers, so the Deezer preview is
   compared with your file: 85% or more is the same recording, lower goes to
   "ask".
6. Otherwise → download.

Checked against a real 251-track discography: 110 already present, 39
repeats, 87 other versions, 15 to download, 0 questions.

## How a wrong recording is caught

Downloaders that take audio from YouTube Music sometimes deliver a different
song under perfectly correct tags. No tag check can see that — only the audio
can. Spotify and Deezer publish 30-second previews, so `verify.py` looks for
the preview *inside* the file: correct recordings scored 92–98% in testing,
wrong ones 53–56% (not 50: taking the best of thousands of offsets lifts the
noise floor). Everything below 85% becomes a question in the panel, with both
players side by side.

The whole file is fingerprinted (`fpcalc -length 0`): by default `fpcalc`
only reads the first two minutes, and a preview cut from the third minute
would make a correct file look wrong.

The same limit as above applies: an instrumental delivered instead of the
vocal version is not caught.

## Install

You need Docker and an image with beets, ffmpeg and `fpcalc` from chromaprint
— `lscr.io/linuxserver/beets` will do.

```bash
git clone https://github.com/JinkoSiz/beets-janitor.git
cd beets-janitor
cp .env.example .env
$EDITOR .env                       # Spotify keys and PANEL_PASSWORD
cp docker-compose.example.yml docker-compose.yml
$EDITOR docker-compose.yml         # point the volumes at your library
docker compose up -d --build
docker compose logs -f
```

The panel listens on `127.0.0.1:8090`. Publish it through your reverse proxy
with HTTPS and set `PANEL_ORIGINS` to its public address
(`https://beets.example.com`), otherwise Django rejects form posts as
cross-site.

If you already ran an older version of these scripts, import the old
journals once so the panel's history and quarantine are complete:

```bash
docker compose exec beets python3 /app/scripts/janitordb.py import-legacy
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
| `JANITOR_DIR` | `CONFIG_DIR/janitor` | shared database, panel uploads, watcher heartbeat |
| `LIBRARY_DB` | `CONFIG_DIR/library.db` | the beets database (the panel reads it directly) |
| `BEETSDIR` | `= CONFIG_DIR` | where beets looks for its own config |
| `DOWNTIFY_URL` | `http://downtify:8000` | where downloads are sent |
| `PANEL_PASSWORD` | — | panel login; without it nobody gets in (`PANEL_PASSWORD_HASH` takes a Django hash instead) |
| `PANEL_ORIGINS` | empty | the panel's public URL, for CSRF checks behind a proxy |
| `PANEL_HOSTS` | `*` | allowed host names |
| `PANEL_SECRET_KEY` | generated | signing key; kept in `JANITOR_DIR/panel-secret` if not set |
| `LOOSE_DIRS` | `Non-Album,TelegramMusic,_Unofficial,_Unmatched` | dump folders (see below) |
| `INTERVAL` | `300` | how often to check `incoming`, seconds (panel setting wins) |
| `NIGHTLY_HOUR` | `5` | earliest hour to start the nightly run (panel setting wins) |
| `JUNK_DAYS` / `DUPES_DAYS` | `3` / `14` | quarantine retention (panel setting wins) |
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
| `verify.py` | checks the audio against a Spotify/Deezer preview of the matched release |
| `leftovers.py` | sorts out what beets left in `incoming`: same audio → quarantine, another version → imported alongside |
| `follow.py` | new releases from followed artists |
| `discography.py` | an artist's discography against your library (also used by the panel) |
| `download.py` | Deezer → Spotify by ISRC/UPC, and the downtify queue |
| `janitordb.py` | the shared database: runs, journal, questions, actions, quarantine, settings |
| `apply_actions.py` | carries out decisions made in the panel |
| `unquarantine.py` | restores anything quarantined by mistake |
| `sitecustomize.py` | paces Spotify requests, caches responses, trips a breaker |
| `panel/` | the web panel |
| `tests/` | sandbox runs of the scripts against a throwaway library |

### About `sitecustomize.py`

Python picks this up on its own as long as the scripts directory is on
`PYTHONPATH`. It wraps outbound calls: keeps a daily Spotify request budget,
slows down at the first sign of throttling, caches responses in SQLite and
takes the source offline for a few hours after a run of failures. Without it a
large library walks straight into HTTP 429 on its first night.

## What it does not do

* It does not download music by itself. Downloads are handed to
  [downtify](https://github.com/henriquesebastiao/downtify); without it,
  whatever you put in `incoming` is the input.
* It does not edit what it isn't sure about. Ambiguous cases become questions
  in the panel, not operations.
* It does not replace or patch beets. It sits next to it; beets stays stock.
* It cannot tell an instrumental from the vocal version by sound — only by the
  marker in the title (see above).

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
