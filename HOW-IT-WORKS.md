# How Disk-Rip works

Details behind the features in the [README](README.md): how discs are identified
(including the TheDiscDb instant match), how episodes are mapped, how thumbnails
and multi-disc handling work, how [library mode](#library-mode) organizes folders
you've already ripped, plus the full configuration and command-line reference.

Paths below use placeholders like `\\SERVER\media\tv` and
`C:\Users\<you>\Disk-Rip` — substitute your own.

---

## The confirmation step

Nothing is ripped or moved until you approve a plan. The web UI shows it as a
drag-and-drop board; the CLI shows it as a table:

```
  t00   23:39   1920x1080  s01e01 The Boy in the Iceberg   2005-02-21 ✓ TMDB 24m
         Aang, a young Airbender, is discovered frozen in an iceberg...
  ...
  Excluded 1 feature-length title(s) (≥ 70m — almost certainly 'Play All'): t09=3:07:12
  Excluded 17 short title(s) (< 15m, likely extras/menus): ...
```

A `⚠ Δ+37m` badge means the title's runtime doesn't match TMDB — on *every* row,
that usually means the wrong show or season was picked (e.g. the 2005 animated
show vs a live-action remake). Fix it before ripping.

### Interactive commands (CLI)

| Screen | Commands |
|--------|----------|
| Movie  | `[Enter]` accept · `title <name>` · `year <yyyy>` · `q`uit |
| TV     | `[Enter]` accept · `season <n>` · `start <ep#>` · `title <name>` · `skip <tid…>` · `keep <tid…>` · `q`uit |

- `skip 12 13` — drop those title ids.
- `keep 9` — re-include a title the heuristics excluded.
- `title <name>` — re-search TMDB and pick a different match.

In the web UI the same corrections are drag-and-drop: drag a title onto an
episode to assign it, drag it back to the pool to skip it.

---

## Instant match via TheDiscDb

Before falling back to label-guessing and TMDB search, Disc-Rip asks
[TheDiscDb](https://thediscdb.com/) — a community database of ~4,400 catalogued
discs — whether it already knows this exact disc. When it does, the whole
identify step is filled in for you: the show, the season, and every title's
episode number, straight from human-curated data.

**How the disc is recognized.** Every pressed disc of a given release has the
same stream-file sizes stamped into it, so Disc-Rip computes TheDiscDb's
*ContentHash* — an MD5 over the sizes of the disc's `.m2ts` (Blu-ray) or
`VIDEO_TS` (DVD) files. File *sizes* live in the unencrypted directory, so this
needs **no decryption, no keys, and no MakeMKV** — just a directory read that
takes milliseconds. That hash is looked up over TheDiscDb's public GraphQL API.

**How their mapping lands on your titles.** Title *numbers* differ between their
scan and yours (different MakeMKV versions, different minimum-length floors), so
the match is joined on what's intrinsic to the mastering and present on both
sides: each title's **source file + segment map**. Titles that match get their
season/episode; anything unmatched (or a disc title they didn't map) just falls
into the normal buckets. Where they mapped an intro/no-intro pair to the same
episode, Disc-Rip keeps its own representative so the board still shows one title
per episode.

**On a hit**, the web UI jumps to a pre-filled board with a *"Matched by
TheDiscDb"* banner; the CLI skips the TMDB search and sets the show, season, and
start episode. **You still confirm and rip** — your click doubles as a check that
the community data is right.

**It's strictly advisory.** Any failure — the disc isn't in the database, you're
offline, the API changed, a burned disc hashes to nothing — silently falls back
to the normal flow. The feature only ever *adds* signal.

**Caching & privacy.** Results are cached under
`%APPDATA%\Disk-Rip\discdb-cache` (macOS:
`~/Library/Application Support/Disk-Rip/discdb-cache`; hits kept indefinitely, misses re-checked
after a week), so re-scanning a disc — common in a multi-disc box-set session —
doesn't re-query. The only thing sent to TheDiscDb is the 16-byte content hash;
no filenames, paths, or personal data. Turn the whole feature off with
`"discdb": false` in the config.

---

## How identification works

*(Used when TheDiscDb has no match — see above.)*

1. **Disc label** — MakeMKV usually reports the real volume name (e.g.
   `Avatar: The Last Airbender Book One: Water Disc 1`). The label is parsed for
   the title plus season/disc hints, including `Season 2`, `S2`, `Book Two`,
   `Disc 1`, `D1`. If a label is generic (`LOGICAL_VOLUME_ID`), you're asked to
   type the title (or pass `--title`).
2. **TMDB search** — returns candidates; you pick the right one (the year
   disambiguates remakes).
3. **Movie vs TV** — auto-detected from title count and durations (one long
   feature ⇒ movie; several ~20–60 min titles ⇒ TV). Override with `--type`.

---

## Episode mapping

Episode-length titles are mapped to sequential episodes, with each title's
runtime cross-checked against TMDB. Two things make this reliable on messy discs:

### Ordering by the Play-All

Disc **title numbers often don't match broadcast order**. The Play-All title
concatenates every episode's video segments in broadcast order, so it's the
disc's own authoritative ordering. Each episode is placed by where its segments
appear in the Play-All's sequence (segments shared by every episode, like a
common intro, are ignored). Discs without a Play-All fall back to title-id order.

### What gets excluded (and how to override)

- **"Play All" titles** — the multi-hour concatenation of every episode.
- **Short titles** — below `min_length_minutes` (extras, menus, recaps).
- **Lower-resolution titles** — an SD title among HD episodes is almost always an
  extra, not an episode.
- **Titles not in the Play-All** — episode-length content the Play-All doesn't
  include is a bonus/alternate feature.
- **Duplicate playlists** — Blu-rays list each episode several times (e.g. one
  *with* the intro/recap, one *without*). These are collapsed by **segment-map
  overlap**: two titles are the same episode when one plays a subset of the
  other's `.m2ts` segments, or they overlap past `segment_overlap_threshold`. The
  version kept is the superset (the one *with* the intro). This catches the
  intro/no-intro case that pure runtime matching cannot.

Anything excluded can be pulled back in with `keep <tid>` (CLI) or by dragging it
onto an episode (web UI).

> **Uniform-runtime shows** (all ~24 min): the runtime check can't tell episodes
> apart, so mapping relies on Play-All order. Glance at the episode names/plots
> (and thumbnails) to confirm before approving.

---

## Multi-disc sets (continuation awareness)

For a season spanning several discs, Disc-Rip looks at what's already in the
season folder and **resumes numbering after the last episode you ripped** — insert
Disc 2 and it starts at e17 instead of colliding with Disc 1's e01. It also:

- marks planned episodes that **already exist on the NAS** so a collision is
  obvious *before* ripping,
- **never overwrites** an existing file, and
- warns if the plan runs **past the season's real episode count** (a sign the disc
  still has duplicate/alternate titles to `skip`).

Override the start any time with `start <n>` (or `--start-episode`).

---

## Thumbnails

Because a uniform-runtime show can't be told apart by duration, the web UI can
show a **thumbnail frame on each title** so you can eyeball which episode it is
and see duplicate groups (each duplicate group also gets a colored dot). Frames
are pulled **straight from the disc** — one per unique episode, no full rip — via
ffmpeg's `bluray:` protocol.

It's optional and off until you set `ffmpeg` in the config; without it the board
works exactly the same, just without pictures.

**How the AACS decryption is set up:** on Windows, libbluray loads a file named
`libaacs.dll` / `libbdplus.dll` from ffmpeg's own folder. MakeMKV's `libmmbd`
provides that decryption, so Disc-Rip **automatically copies `libmmbd64.dll` in
under those names** the first time it runs (re-copying after an ffmpeg update).
No manual DLL work or environment variables. MakeMKV must be installed, and its
app **closed** so the drive is free.

Notes:
- Frames are sampled ~40% into the episode (past the intro) and cached in
  `work_dir/thumbs/`. Duplicates reuse their representative's frame.
- Extraction is serialized (one at a time) so many thumbnails don't thrash the
  drive; the first takes ~15 s (MakeMKV/AACS warm-up), and prefetch starts right
  after the scan.
- If a frame can't be decoded, that title just shows no thumbnail — nothing else
  breaks.

Install a libbluray-enabled ffmpeg with `winget install Gyan.FFmpeg` (its "full"
build includes libbluray) and point `ffmpeg` at the `.exe` (or leave it as
`"ffmpeg"` if it's on your PATH).

**macOS:** nothing is copied. The disc is read from where macOS mounted it
(`/Volumes/<LABEL>`), and libbluray is pointed at MakeMKV's `libmmbd` inside
`MakeMKV.app` through the `LIBAACS_PATH` / `LIBBDPLUS_PATH` environment
variables. Homebrew's regular `ffmpeg` has no `bluray:` protocol; install
`ffmpeg-full` for disc thumbnails. This path has had far less testing than the
Windows one — if a frame can't be read you simply get no picture. (Thumbnails on
the Library page read ordinary files and work with any ffmpeg.)

---

## Library mode

For discs you ripped *before* using Disk-Rip (or ripped by hand in the MakeMKV
app): a folder where every sub-folder is one ripped disc and the files inside are
whatever MakeMKV called them.

```
/Volumes/rips/
  Coco (2017)/Coco (2017).mkv
  Coco - disk 2/Coco_t04.mkv, Dante.mkv, ...
  Coco Bonus Disc/Deleted Scene - To the Bridge.mkv, ...
  IROBOT_US/title_t00.mkv ... title_t22.mkv
```

Open the **Organize a folder** tab (`/library`) or run `src/library.py`. Nothing
is read from an optical drive, so MakeMKV isn't needed — only `ffprobe` (part of
ffmpeg), which reads each file's length, resolution, audio-track count and
chapter count from the file header. That is fast even on a NAS share, and results
are cached in `work_dir`.

### 1. Which folders are the same movie

Each folder name is cleaned into a title: a `(2017)` year is kept as a hint;
`disk 2`, `Disc 2`, `Bonus Disc`, `Special Features`, `BLU-RAY`/`DVD`/`4K`, a raw
label's region suffix (`IROBOT_US`), and everything after ` AKA ` are dropped.
Folders that reduce to the same title are **merged into one movie** — so
`Coco (2017)`, `Coco - disk 2` and `Coco Bonus Disc` become one entry, while
`Shrek` and `Shrek 2` stay separate. (Two folders also land in the same movie
folder if you match them to the same TMDB entry by hand.)

A folder that contains only other folders (an `Output/Movies/...` tree, say) is
not a ripped disc: it's listed under *Folders left alone* and not descended
into. Scan it on its own if you want its contents organized. The configured
`movie_root` and anything in `library_ignore` are skipped too.

### 2. Which movie it is

The title is searched on TMDB and the top results are scored on name, year (if
the folder had one) and — the strong signal — **how closely the film's runtime
matches the longest file in the group**. That's what separates the 2016 *Doctor
Strange* (115 min) from the 2007 animated one (76 min) without asking. If the
plain title finds nothing, a leading studio possessive is dropped
(`Marvel's Doctor Strange`), the title tag MakeMKV wrote into the files is
tried, and a squashed label is tried split in two (`IROBOT` → `I ROBOT`) — a
split is only believed when its runtime matches.

Each movie gets a confidence: **high** (runtime agrees), **medium** (name agrees,
nothing to check the runtime against), or **low**. High and medium are ticked for
moving; low, unmatched, TV-looking and still-being-written folders are left
unticked until you say otherwise. *Change match* searches TMDB again and shows
each candidate's runtime next to your file's.

### 3. Which file is what

| Role | Rule |
|------|------|
| **Main feature** | The file whose length matches TMDB's runtime (±3 min); failing that, the best-resolution, longest file of at least `movie_min_minutes`. A second feature of the same length at a *different* resolution (4K + Blu-ray combo) is kept as a second version; at the same resolution it's a duplicate playlist and is skipped. |
| **Extra** | Everything else that isn't junk. Files already sitting in an extras sub-folder keep that category. Otherwise the category comes from the name (`Deleted…` → Deleted Scenes, `…Trailer`/`Promo` → Trailers, `Making of` → Behind The Scenes, …); named extras default to **Featurettes**, unnamed ones (`title_t07`) to **Other** as `Extra t07`. MakeMKV names are tidied: `F1-SF_02_The_Anatomy_Of_A_Crash_t11` → `The Anatomy Of A Crash`. |
| **Skipped** | No audio track (warning/logo loops); `TMPL` / `Main Menu` / `WRN` / `warning` in the name; shorter than `extras_min_seconds`; same length *and* size as another file (duplicate); a short unnamed 4K clip on a 4K disc (menu background); HD at under 3 Mbit/s (a static screen); an unnamed clip with 5+ audio tracks (a fragment of the feature, common on Disney discs); unreadable files. |

Every skip shows its reason, and every role, category and name can be changed in
the web UI before anything moves. Skipped files are simply left where they are.

### 4. Where things go

```
<movie_root>/Coco (2017) [imdb-tt2380307]/
    Coco (2017) [imdb-tt2380307] - 2160p.mkv
    Deleted Scenes/Deleted Scene - To the Bridge.mkv
    Featurettes/Dante.mkv
    Trailers/Belong - Australia Trailer.mkv
```

The extras folders — `Behind The Scenes`, `Deleted Scenes`, `Featurettes`,
`Interviews`, `Scenes`, `Shorts`, `Trailers`, `Other` — are the ones both Plex
and Jellyfin recognize. Subtitle/`.nfo` files named after a video travel with it.

### 5. Moving, safely

- Files are **moved, not copied**. With `movie_root` on the same volume/share as
  the rips this is an instant rename; across volumes it's copy → verify size →
  delete source, with progress (slow for 50 GB files over a network — keep the
  library on the same share).
- **Nothing is overwritten**: a target that already exists is flagged and left.
- **Nothing is deleted**, unless you tick *Remove emptied source folders*
  (`--prune-empty`), which removes a source folder only when all that's left in
  it is empty sub-folders and `.DS_Store` files.
- Every run writes `work_dir/library-logs/organize-<date>.json`. **Undo** (button
  after a move, or `library.py --undo <log>`) puts every file back.
- A folder with a file modified in the last three minutes is assumed to be a rip
  in progress and is left unticked.

**Limits.** Movies only — a folder that looks like a TV disc (several
episode-length files, no feature) is flagged and left alone; rip TV from the
disc so the Play-All ordering can be used. Unnamed extras stay unnamed
(`Extra t07`) unless you rename them; the per-file thumbnails are there to help.

---

## Command-line reference

```powershell
py src\diskrip.py                          # scan, confirm, rip
py src\diskrip.py --dry-run                # show the plan, rip nothing
py src\diskrip.py --list-drives            # list optical drives
py src\diskrip.py --drive 1                # choose a drive by index
py src\diskrip.py --type tv|movie|auto     # force disc type
py src\diskrip.py --title "The Office"     # override the title guess
py src\diskrip.py --season 1               # force season (TV)
py src\diskrip.py --start-episode 9        # first episode on this disc (TV)
py src\diskrip.py --yes                    # unattended, no confirmation
py src\diskrip.py --config path\to\config.json

py src\webapp.py                           # web UI on http://127.0.0.1:8765
py src\webapp.py --port 9000 --no-browser
py src\webapp.py --library                 # open on the Organize-a-folder page

py src\library.py                          # scan rips_root, show the plan, ask, move
py src\library.py --dry-run                # show the plan, move nothing
py src\library.py --root /Volumes/rips     # another folder (macOS: smb://host/share too)
py src\library.py --only Shrek             # only folders whose name contains "Shrek"
py src\library.py --match "IROBOT_US=I, Robot"     # fix an identification by title
py src\library.py --match "IROBOT_US=tmdb:2048"    # ...or pin the exact TMDB id
py src\library.py --include-low            # also move low-confidence matches
py src\library.py --prune-empty            # remove source folders left empty
py src\library.py --yes                    # no confirmation prompt
py src\library.py --undo path\to\organize-....json   # put a run's files back
```

On macOS / Linux run the same scripts with `python3 src/…`.

---

## Configuration reference

`config.json` (create it by running setup, or copy `config.example.json`):

| Key | Meaning |
|-----|---------|
| `tmdb_api_key` | Your TMDB v3 API key. |
| `makemkvcon` | Path to `makemkvcon64.exe` (macOS: `/Applications/MakeMKV.app/Contents/MacOS/makemkvcon`). Only needed to rip discs. |
| `tv_root` / `movie_root` | Destination library roots (local or a NAS share). |
| `work_dir` | Local scratch folder titles are ripped into before moving to the library. Needs free space ≈ the largest title. |
| `min_length_minutes` | Titles shorter than this are treated as extras (default `15`). |
| `movie_min_minutes` | Titles this long or longer are treated as a feature / Play-All (default `70`). |
| `language` | TMDB metadata language (default `en`). |
| `write_nfo` | If `true`, write a minimal `.nfo` next to each file. Default `false` (Jellyfin/tinyMediaManager scrape by the folder's `[imdb-…]` id anyway). |
| `segment_overlap_threshold` | How much two titles' segment sets must overlap to be treated as the same episode (default `0.6`; the subset/intro rule fires regardless). Raise toward `1.0` if distinct episodes get wrongly merged. |
| `ffmpeg` | Path to `ffmpeg.exe` (with libbluray) to enable thumbnails. Empty ⇒ thumbnails off. |
| `thumb_mid_fraction` / `thumb_tail_seconds` / `thumb_width` | Thumbnail frame offset and size (defaults `0.40`, `120`, `240`). |
| `discdb` | Look up discs in [TheDiscDb](https://thediscdb.com/) to auto-fill identity and episode mapping (default `true`). Set `false` to disable all lookups. |
| `rips_root` | Library mode: the folder of already-ripped discs. A path, or on macOS an `smb://host/share` address (mounted under `/Volumes/` on demand). |
| `ffprobe` | Library mode: path to `ffprobe`. Empty ⇒ looked up next to `ffmpeg`, then on the PATH. |
| `library_ignore` | Library mode: top-level folder names to skip, e.g. `["Test", "Incoming"]`. |
| `extras_min_seconds` | Library mode: files shorter than this are skipped as junk (default `60`). |
| `discdb_endpoint` / `discdb_timeout` | TheDiscDb GraphQL URL and per-lookup timeout in seconds (defaults `https://thediscdb.com/graphql/`, `4`). Lookups are advisory — a timeout just falls back to the normal flow. |

---

## Notes & limitations

- **Artwork / full metadata** — folder names carry the `[imdb-…]` id, so Jellyfin
  and tinyMediaManager scrape posters, fanart, and `.nfo` after the rip. Disc-Rip
  focuses on ripping + correct naming, not artwork.
- **TheDiscDb coverage** — the lookup only helps for discs already in the
  database; TV box sets are the thinnest area. A miss costs you nothing (normal
  flow continues), and the ContentHash is drive-independent, so a disc that
  matches once matches on any drive.
- **Drive contention** — only one program can read the optical drive at a time.
  Keep the MakeMKV app closed while scanning or ripping. Disc-Rip refuses to scan
  if it detects the MakeMKV app running.
- **Stall watchdog** — MakeMKV can wedge on a dirty/scratched disc and hang with
  no output. If a scan or rip produces no output for 120 seconds
  (`MakeMKV.STALL_TIMEOUT`), Disc-Rip kills it and tells you to clean/reseat the
  disc. If MakeMKV is already hung from a previous run, end it in Task Manager (or
  `taskkill /IM makemkvcon64.exe /F`) — it holds the drive open.
- **Disk space & time** — a full Blu-ray title can be several GB and take a while
  to rip.
- **macOS** — library mode is plain Python + ffprobe and behaves the same on
  every OS. The disc-ripping path is ported (MakeMKV detection via `pgrep`, the
  disc's mount point resolved from `/dev/rdiskN` for TheDiscDb hashing and
  thumbnails) but has had much less real-disc testing than on Windows. To end a
  hung `makemkvcon`: `pkill makemkvcon`.

---

## Project layout

| File | Purpose |
|------|---------|
| `src/diskrip.py` | Core engine: MakeMKV control, TMDB, naming, dedup/ordering, CLI. |
| `src/webapp.py` | Local web server + JSON API. |
| `src/thumbs.py` | ffmpeg frame extraction + libmmbd/AACS setup. |
| `src/discdb.py` | TheDiscDb lookup: ContentHash, GraphQL query + cache, title join. Also runnable standalone (`py src\discdb.py <drive>`) to test a disc. |
| `src/library.py` | Library mode: scan a folder of ripped discs, group, identify, classify, move, undo. Also the CLI. |
| `ui/index.html` | The drag-and-drop web UI (single file). |
| `ui/library.html` | The Organize-a-folder page (single file). |
| `install/setup.ps1` / `install/setup.cmd` | One-time Windows setup (winget installs + config). |
| `install/setup.sh` / `install/setup.command` | One-time macOS setup (Homebrew installs), then runs `configure.py`. |
| `install/configure.py` | Prompts for the key and folders, writes `config.json`, verifies. |
| `config.example.json` | Template config with all keys and defaults (repo root). |

The repo is organized into `src/` (Python engine + server), `ui/` (the single-file
web front-end), and `install/` (the Windows and macOS setup scripts); `config.json` and
`config.example.json` live at the repo root.
