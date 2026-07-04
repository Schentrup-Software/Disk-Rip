# Disk-Rip

Automated DVD/Blu-ray ripping with **MakeMKV** + **TMDB** naming. Point it at a
disc and it rips each title to your NAS with the exact Jellyfin /
tinyMediaManager naming your library already uses:

```
Movies:  \\NAS\public\media\movies\Title (Year) [imdb-ttID]\Title (Year) [imdb-ttID] - 1080p.mkv
TV:      \\NAS\public\media\tv\Show (Year) [imdb-ttID]\Season N\Show (Year) - s01e01.mkv
```

It scans the disc first (no ripping), identifies the movie/show via TMDB, and
shows a **rich confirmation screen** — episode titles, air dates, plots, and a
runtime cross-check (`✓` / `⚠`) against TMDB — so you can approve or correct the
plan **without opening any video files**. Nothing is ripped or moved until you
confirm.

## Two ways to use it

- **Web UI (recommended for TV):** `py webapp.py` — opens a browser page where you
  pick a drive, scan, and **drag disc titles onto episodes** in a table, then rip
  with live progress. Best for messy discs (duplicate playlists, multi-disc sets).
- **CLI:** `py diskrip.py` — the same engine as a terminal confirm-screen. Good for
  quick movie rips and unattended (`--yes`) runs.

Both share `diskrip.py`, `config.json`, and identical naming/dedup logic.

## Web UI

```powershell
py webapp.py            # opens http://127.0.0.1:8765 in your browser
py webapp.py --port 9000 --no-browser
```

Flow: **Disc** (pick drive → Scan) → **Identify** (search TMDB, pick the right
match — e.g. 2005 vs 2024) → **Match** → **Rip**.

On the Match board, disc titles are draggable cards on the left (badged
`dup` / `play-all` / `extra` so you can see what's real), and the season's
episodes are drop targets on the right. It auto-maps to the first unripped
episode; drag any title onto a different episode to correct it, drag it back to
the pool to skip it. Episodes already on the NAS are badged and never
overwritten. Runtime mismatches show a `Δ` badge per row. Then **Review & rip**
shows the exact target files and rips with a progress bar each.

The server is local-only (`127.0.0.1`) and single-user. Keep the terminal open
while using it; Ctrl+C stops it.

## Requirements

- **Windows** with **MakeMKV** installed (uses `makemkvcon64.exe`).
- **Python 3.9+** (tested on 3.14). No pip packages — standard library only.
- A free **TMDB API key** (v3) from <https://www.themoviedb.org/settings/api>.

## Setup

### Quick setup (recommended)

Double-click **`setup.cmd`** (or run `powershell -ExecutionPolicy Bypass -File setup.ps1`).
It uses **winget** to install the prerequisites (Python, MakeMKV, and the
libbluray-enabled ffmpeg), finds their executables, asks for your TMDB key and
library paths, writes `config.json`, and verifies everything works. Re-running is
safe — your existing values become the defaults.

The one step it can't do for you: **register MakeMKV.** MakeMKV needs a paid
license or the free beta key (rotates monthly, posted on the MakeMKV forum). The
script offers to apply a key you paste; otherwise open the MakeMKV app once and
enter it there.

### Manual setup

1. Install [MakeMKV](https://www.makemkv.com/), [Python 3.9+](https://python.org),
   and (for thumbnails) an ffmpeg build with libbluray (`winget install Gyan.FFmpeg`).
2. Copy `config.example.json` to `config.json`, fill in `tmdb_api_key`, and set the
   `makemkvcon` / `ffmpeg` paths and your `tv_root` / `movie_root` (see **Config**).

## Usage

```powershell
# List optical drives
py diskrip.py --list-drives

# Rip the disc in the (only/auto-detected) drive — auto-detects movie vs TV,
# then shows the confirmation screen before ripping:
py diskrip.py

# See the plan without ripping anything (great first run for any new disc):
py diskrip.py --dry-run

# Force type / identity / season when the disc label is unhelpful:
py diskrip.py --type tv  --title "The Office" --season 1 --start-episode 1
py diskrip.py --type movie --title "Collateral"

# Fully unattended (no confirmation) — uses best-guess matching:
py diskrip.py --yes
```

### The confirmation screen

For TV it lists every episode-length title mapped to an episode, with a runtime
check so a wrong match is obvious at a glance:

```
  t00   23:39   1920x1080  s01e01 The Boy in the Iceberg   2005-02-21 ✓ TMDB 24m
         Aang, a young Airbender, is discovered frozen in an iceberg...
  ...
  Excluded 1 feature-length title(s) (≥ 70m — almost certainly 'Play All'): t09=3:07:12
  Excluded 17 short title(s) (< 15m, likely extras/menus): ...
```

`⚠ Δ+37m` on a row (or on **every** row) means the runtime doesn't match TMDB —
usually a sign the wrong show/season was picked (e.g. the 2005 animated *Avatar*
vs the 2024 live-action remake). Fix it right there.

Interactive commands:

| Screen | Commands |
|--------|----------|
| Movie  | `[Enter]` accept · `title <name>` · `year <yyyy>` · `q`uit |
| TV     | `[Enter]` accept · `season <n>` · `start <ep#>` · `title <name>` · `skip <tid…>` · `keep <tid…>` · `q`uit |

- `skip 12 13` drops those title ids (e.g. duplicate playlists).
- `keep 9` re-includes an excluded title (e.g. a long finale wrongly treated as Play All).
- `title <name>` re-searches TMDB and lets you pick the right match (2005 vs 2024, etc.).

## How identification works

1. **Disc label** — MakeMKV usually reports the real volume name
   (e.g. `Avatar: The Last Airbender Book One: Water Disc 1`). The script parses
   the show/movie title plus season/disc hints, including `Season 2`, `S2`,
   `Book Two`, `Disc 1`, `D1`.
2. **TMDB search** — returns candidates; you pick the right one (year
   disambiguates remakes).
3. **Movie vs TV** — auto-detected from title count and durations (one long
   feature ⇒ movie; several ~20–60 min titles ⇒ TV). Override with `--type`.
4. **Episode mapping** — episode-length titles are mapped to sequential
   episodes. **Order comes from the Play-All title**, not the raw title numbers:
   the Play-All lists every episode's segments in broadcast order, so each
   episode is placed by where its segments appear in that sequence (disc title
   order often does *not* match broadcast order). Falls back to title-id order on
   discs without a Play-All. Each title's runtime is still cross-checked against
   the TMDB episode runtime. Automatically excluded:
   - **"Play All" titles** (multi-hour concatenations of every episode),
   - **short extras/menus**,
   - **lower-resolution titles** — an SD title among HD episodes is almost always
     an extra, not an episode, so it's excluded,
   - **titles not in the Play-All** — episode-length content the disc's Play-All
     doesn't include is a bonus/alternate feature, not an episode, and
   - **duplicate playlists** — Blu-rays routinely list each episode as several
     titles (e.g. one *with* the intro/recap, one *without*). These are collapsed
     by **segment-map overlap**: two titles are the same episode when one plays a
     subset of the other's `.m2ts` segments (or they overlap past
     `segment_overlap_threshold`), so a 20-episode season isn't ripped as 40
     files. The kept version is the superset (the one *with* the intro). This
     catches the intro/no-intro case that pure runtime matching cannot.

   Any of these can be pulled back in with `keep <tid>` (CLI) or by dragging the
   title onto an episode (web UI) if the heuristic guessed wrong.

### Visual matching (thumbnails)

Because a uniform-runtime show (all ~24 min) can't be told apart by duration, the
web UI can show a **thumbnail frame on each title** so you can eyeball which
episode it is and see duplicate groups at a glance (each duplicate group also
gets a colored dot). Frames are pulled **straight from the disc** — one frame per
title, no full rip — via ffmpeg's `bluray:` protocol. It's optional and off until
you set `ffmpeg` in the config (see **ffmpeg setup** below); without it the board
works exactly as before, just without pictures. The thumbnail is sampled ~40% in
(past the intro); duplicate versions of an episode share an identical *ending*, so
that's used as a secondary confirmation frame.

#### ffmpeg setup (optional, enables thumbnails)

1. Install a Windows ffmpeg build **with libbluray** — easiest is
   `winget install Gyan.FFmpeg` (the "full" build, which includes libbluray).
   Set `"ffmpeg"` in `config.json` to the full path of `ffmpeg.exe` (or just
   `"ffmpeg"` if it's on your PATH).
2. AACS-protected discs are decrypted on the fly by MakeMKV's **libmmbd**. On
   Windows, libbluray loads a file literally named `libaacs.dll`/`libbdplus.dll`
   from ffmpeg's own folder — so the app **automatically copies MakeMKV's
   `libmmbd64.dll` in under those names** the first time it runs (re-copying
   after an ffmpeg update). No manual DLL juggling or env vars needed. MakeMKV
   must be installed, and (as always) its GUI **closed** so the drive is free.
3. That's it — restart `webapp.py` and re-scan. If a specific frame can't be
   decoded, that title just shows no thumbnail; nothing else breaks.

> Frames extract lazily and are **cached** in `work_dir/thumbs/`; the first one
> takes ~15 s (MakeMKV/AACS warm-up), the rest are quicker, and extraction is
> serialized so 16 thumbnails don't thrash the drive at once.

### Multi-disc sets (continuation awareness)

For a season spanning several discs, the script looks at what's already in the
season folder on the NAS and **resumes numbering after the last episode you
ripped** — insert Disc 2 and it starts at e17 instead of colliding with Disc 1's
e01. It also:

- marks every planned episode that **already exists on the NAS** (`● already on
  NAS (will skip)`) so a collision is obvious *before* ripping,
- never overwrites an existing file, and
- warns if the plan maps **past the season's real episode count** (a sign the
  disc still has duplicate/alternate titles to `skip`).

Override the start any time with `start <n>` at the prompt (or `--start-episode`).

> **Uniform-runtime shows** (e.g. all ~24 min): the runtime `✓` check can't tell
> episodes apart, so mapping relies on disc order + the start episode. Glance at
> the episode names/plots and the "already on NAS" markers to confirm the disc's
> position in the season before approving.

If a disc label is generic (`LOGICAL_VOLUME_ID`), the script asks you to type
the title, or pass `--title`.

## Config (`config.json`)

| Key | Meaning |
|-----|---------|
| `tmdb_api_key` | Your TMDB v3 API key. |
| `makemkvcon` | Path to `makemkvcon64.exe`. |
| `tv_root` / `movie_root` | NAS destination roots. |
| `work_dir` | Local scratch folder titles are ripped into before moving to the NAS. |
| `min_length_minutes` | Titles shorter than this are treated as extras (default 15). |
| `movie_min_minutes` | Titles this long or longer are treated as a feature / Play-All (default 70). |
| `language` | TMDB metadata language (default `en`). |
| `write_nfo` | If `true`, also write a minimal `.nfo` next to each file (Jellyfin/TMM will otherwise scrape by the folder's `[imdb-…]` id). Default `false`. |
| `segment_overlap_threshold` | How much two titles' segment sets must overlap to be treated as the same episode (default `0.6`; the subset/intro rule fires regardless). Raise toward `1.0` if distinct episodes get wrongly merged. |
| `ffmpeg` | Path to `ffmpeg.exe` (with libbluray) to enable per-title thumbnails. Empty ⇒ thumbnails off. |
| `thumb_mid_fraction` / `thumb_tail_seconds` / `thumb_width` | Thumbnail frame offsets and size (defaults `0.40`, `120`, `240`). |

## Notes & limitations

- **Artwork / full metadata**: the folder names carry the `[imdb-…]` id, so
  Jellyfin and tinyMediaManager can scrape posters, fanart, and `.nfo` after the
  rip. This script focuses on ripping + correct naming, not artwork.
- **Episode ordering** relies on disc title order matching broadcast order
  (almost always true) plus the runtime cross-check. The confirmation screen is
  there precisely because a minority of discs order titles oddly — check the
  `✓`/`⚠` column before approving.
- Ripping a full Blu-ray disc can take a while and needs free space in
  `work_dir` equal to the largest title.
- **Stall watchdog**: MakeMKV can wedge on a dirty/scratched disc and hang
  forever with no output. If a scan or rip produces no output for 120 seconds
  (`MakeMKV.STALL_TIMEOUT`), the script kills it and tells you to clean/reseat
  the disc, instead of hanging silently. The scan now also streams MakeMKV's own
  status text so you can see it working. If MakeMKV itself is already hung from a
  previous run, end it in Task Manager (or `taskkill /IM makemkvcon64.exe /F`)
  before retrying — it holds the drive open.
