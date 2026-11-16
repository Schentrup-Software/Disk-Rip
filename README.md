# Disk-Rip

Rip your DVDs and Blu-rays into a clean, Jellyfin/Plex-ready library. Point it at
a disc — it identifies the movie or show via **TMDB**, works out which titles are
the real episodes, and rips them with correct names, with a confirmation step so
nothing lands in the wrong place.

Already ripped a pile of discs? Point it at that **folder** instead: it works out
which movie each folder is, merges multi-disc sets, sorts the feature from the
extras and the menu junk, and moves everything into the same layout.

```
Movies:  Title (Year) [imdb-ttID]/Title (Year) [imdb-ttID] - 1080p.mkv
TV:      Show (Year) [imdb-ttID]/Season N/Show (Year) - s01e01.mkv
Extras:  Title (Year) [imdb-ttID]/Featurettes/Name.mkv   (library mode)
```

> Runs on **Windows** and **macOS**. Ripping is best tested on Windows; on macOS
> the folder-organizing mode is the well-trodden path.

## Features

- **Drag-and-drop web UI** (or a terminal CLI) — you always confirm before
  anything is ripped or moved.
- **Recognizes known discs via [TheDiscDb](https://thediscdb.com/)** — if the
  community already catalogued your exact disc, the show, season, and per-episode
  title mapping are filled in automatically; you just review and rip. Unknown
  discs fall back to the normal flow.
- **TMDB identification** with a match picker (e.g. the 2005 vs 2024 _Avatar_).
- **Handles messy TV Blu-rays** — excludes "Play All" titles, extras, and
  duplicate playlists; orders episodes using the disc's own Play-All; resumes
  across multi-disc sets without clobbering what you already ripped.
- **Optional per-title thumbnails** pulled straight from the disc (no full rip),
  so you can eyeball which title is which.
- **Organize folders you've already ripped** — a second page in the web UI (and
  a CLI) that scans a folder of ripped discs, merges "Movie", "Movie - disk 2"
  and "Movie Bonus Disc" into one movie, picks the feature by matching its
  length to TMDB's runtime, files extras into Plex/Jellyfin extras folders, and
  leaves menus, warning loops and duplicates behind. Needs no MakeMKV.
- **Never overwrites** existing files.

For how each of these works under the hood, see **[HOW-IT-WORKS.md](HOW-IT-WORKS.md)**.

## Demo

A static demo of the app lives [here](https://schentrup-software.github.io/Disk-Rip/demo/). It runs the
**real** UI with mocked responses so you can click through four cases by picking a
drive: a movie found in TheDiscDb, a movie that isn't, a TV disc found in
TheDiscDb (with a double episode), and a TV disc that isn't.

## Requirements

- Windows 10 / 11, or macOS (with [Homebrew](https://brew.sh))
- Python 3.9+ and ffmpeg; MakeMKV if you want to rip discs (organizing existing
  folders doesn't need it) — **all installed for you by the setup script below.**
- A free **TMDB API key** (v3): <https://www.themoviedb.org/settings/api>

## Setup

### macOS

1. Download or clone this repository.
2. Double-click **`install/setup.command`** (or run `bash install/setup.sh`).
   It installs Python and ffmpeg via **Homebrew**, offers to install MakeMKV,
   asks for your TMDB key and folders, writes `config.json`, and verifies
   everything works. Folders can be local paths or a NAS share
   (`smb://nas.example.com/rips` — macOS mounts it under `/Volumes/`).
3. Only if you rip discs: **register MakeMKV** (open the app once and enter your
   license or the free beta key).

Re-run setup any time to change the answers (`--skip-install` skips Homebrew).

### Windows

1. Download or clone this repository.
2. Double-click **`install\setup.cmd`** (or run
   `powershell -ExecutionPolicy Bypass -File install\setup.ps1`).
   It installs the prerequisites via **winget**, asks for your TMDB key and
   library paths, writes `config.json`, and verifies everything works.
3. **Register MakeMKV** — open the MakeMKV app once and enter your license, or the
   free beta key (rotates monthly, posted on the MakeMKV forum). The setup script
   can apply a key you paste, but can't provide one.

To reconfigure later, re-run setup (your current values become the defaults) or
edit `config.json`.

<details>
<summary>Manual setup (without the script)</summary>

1. Install [MakeMKV](https://www.makemkv.com/) and [Python 3.9+](https://python.org).
   For thumbnails, also install an ffmpeg build **with libbluray**:
   `winget install Gyan.FFmpeg` (macOS: `brew install ffmpeg`, or
   `ffmpeg-full` for thumbnails straight from a disc).
2. Copy `config.example.json` to `config.json` and set at least `tmdb_api_key`,
`tv_root`, `movie_root`, and the `makemkvcon` / `ffmpeg` paths (macOS:
`/Applications/MakeMKV.app/Contents/MacOS/makemkvcon`).
See the [config reference](HOW-IT-WORKS.md#configuration-reference).
 </details>

## Usage

Commands below are shown for Windows (`py src\webapp.py`); on macOS use
`python3 src/webapp.py`.

### Organize a folder you've already ripped

```bash
python3 src/webapp.py --library      # web UI, opens http://127.0.0.1:8765/library
python3 src/library.py --dry-run     # or: print the plan in the terminal
```

**Scan folder** → review each movie (match, feature, extras, what's skipped) →
fix anything that's off → **Move files**. Each top-level folder is treated as one
ripped disc; nothing moves until you confirm, nothing is overwritten or deleted,
and every run can be undone. Details: **[HOW-IT-WORKS.md](HOW-IT-WORKS.md#library-mode)**.

### Rip a disc

> Keep the **MakeMKV app closed** while ripping — only one program can read the
> optical drive at a time.

#### Web UI (recommended)

```powershell
py src\webapp.py
```

Opens <http://127.0.0.1:8765>. Pick the drive → **Scan** → confirm the show →
drag any titles that need fixing onto the right episodes → **Rip**. The board
flags duplicates, extras, and episodes you already have on the NAS.

If the disc is already in **TheDiscDb**, Scan jumps straight to a pre-filled
board — a "Matched by TheDiscDb" banner, the right show/season selected, and
every episode already assigned — so you usually just click **Rip**.

#### CLI

```powershell
py src\diskrip.py                # scan the disc, confirm, rip
py src\diskrip.py --dry-run      # show the plan, rip nothing
py src\diskrip.py --list-drives  # list optical drives
py src\diskrip.py --yes          # unattended (no confirmation)
```

The full flag list and the interactive commands are in
**[HOW-IT-WORKS.md](HOW-IT-WORKS.md#command-line-reference)**.

## Configuration

The setup script writes `config.json` for you. The keys you'll usually set are
`tmdb_api_key`, `tv_root`, `movie_root`, and (for library mode) `rips_root`;
everything else has sensible defaults. See `config.example.json` and the
[full reference](HOW-IT-WORKS.md#configuration-reference).

> Your `config.json` contains your TMDB key. It's git-ignored — don't commit it.

## Troubleshooting

- **"The MakeMKV app is open" / SCSI errors** — close the MakeMKV window. Only one
  program can use the optical drive at a time.
- **Scan hangs, then aborts** — the disc is probably dirty or scratched; clean and
  reseat it. (The tool aborts after ~2 minutes rather than hanging forever.)
- **No thumbnails** — set `ffmpeg` in `config.json` to a libbluray build
  (`winget install Gyan.FFmpeg`). Details in
  [HOW-IT-WORKS.md](HOW-IT-WORKS.md#thumbnails).
- **Library page: "Source folder not found"** — the NAS share isn't mounted.
  Connect to it in Finder (Go → Connect to Server) or enter its `smb://` address
  as the source folder and Disk-Rip will mount it.
- **Library page: a movie is matched wrong / not at all** — click **Change
  match**, search the title, and pick the entry whose runtime matches your file.

## Credits

Uses [MakeMKV](https://www.makemkv.com/) for ripping, the
[TMDB](https://www.themoviedb.org/) API for metadata, and
[TheDiscDb](https://thediscdb.com/) (MIT) for community disc-to-episode mappings.
This product uses the TMDB API but is not endorsed or certified by TMDB.
