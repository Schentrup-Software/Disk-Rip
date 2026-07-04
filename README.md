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

1. `config.json` is already created with your TMDB key and NAS paths. If you ever
   need to recreate it, copy `config.example.json` to `config.json` and fill in
   `tmdb_api_key`.
2. Check the paths in `config.json` (see **Config** below).

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
4. **Episode mapping** — episode-length titles are mapped in disc order to
   sequential episodes, with each title's runtime cross-checked against the
   TMDB episode runtime. Automatically excluded:
   - **"Play All" titles** (multi-hour concatenations of every episode),
   - **short extras/menus**, and
   - **duplicate playlists** — Blu-rays routinely list each episode as several
     titles that share the same video segments; titles with an identical segment
     map are de-duplicated so a 20-episode season isn't ripped as 40 files.

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
