# Disk-Rip

Rip your DVDs and Blu-rays into a clean, Jellyfin/Plex-ready library. Point it at
a disc — it identifies the movie or show via **TMDB**, works out which titles are
the real episodes, and rips them with correct names, with a confirmation step so
nothing lands in the wrong place.

```
Movies:  Title (Year) [imdb-ttID]/Title (Year) [imdb-ttID] - 1080p.mkv
TV:      Show (Year) [imdb-ttID]/Season N/Show (Year) - s01e01.mkv
```

> **Windows only** for now.

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
- **Never overwrites** existing files.

For how each of these works under the hood, see **[HOW-IT-WORKS.md](HOW-IT-WORKS.md)**.

## Demo

A static demo of the app lives [here](https://schentrup-software.github.io/Disk-Rip/demo/). It runs the
**real** UI with mocked responses so you can click through four cases by picking a
drive: a movie found in TheDiscDb, a movie that isn't, a TV disc found in
TheDiscDb (with a double episode), and a TV disc that isn't.

## Requirements

- Windows 10 / 11
- MakeMKV, Python 3.9+, and (optional, for thumbnails) ffmpeg with libbluray —
  **all installed for you by the setup script below.**
- A free **TMDB API key** (v3): <https://www.themoviedb.org/settings/api>

## Setup

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
   `winget install Gyan.FFmpeg`.
2. Copy `config.example.json` to `config.json` and set at least `tmdb_api_key`,
`tv_root`, `movie_root`, and the `makemkvcon` / `ffmpeg` paths.
See the [config reference](HOW-IT-WORKS.md#configuration-reference).
 </details>

## Usage

> Keep the **MakeMKV app closed** while ripping — only one program can read the
> optical drive at a time.

### Web UI (recommended)

```powershell
py src\webapp.py
```

Opens <http://127.0.0.1:8765>. Pick the drive → **Scan** → confirm the show →
drag any titles that need fixing onto the right episodes → **Rip**. The board
flags duplicates, extras, and episodes you already have on the NAS.

If the disc is already in **TheDiscDb**, Scan jumps straight to a pre-filled
board — a "Matched by TheDiscDb" banner, the right show/season selected, and
every episode already assigned — so you usually just click **Rip**.

### CLI

```powershell
py src\diskrip.py                # scan the disc, confirm, rip
py src\diskrip.py --dry-run      # show the plan, rip nothing
py src\diskrip.py --list-drives  # list optical drives
py src\diskrip.py --yes          # unattended (no confirmation)
```

The full flag list and the interactive commands are in
**[HOW-IT-WORKS.md](HOW-IT-WORKS.md#command-line-reference)**.

## Configuration

`install\setup.cmd` writes `config.json` for you. The keys you'll usually set are
`tmdb_api_key`, `tv_root`, and `movie_root`; everything else has sensible
defaults. See `config.example.json` and the
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

## Credits

Uses [MakeMKV](https://www.makemkv.com/) for ripping, the
[TMDB](https://www.themoviedb.org/) API for metadata, and
[TheDiscDb](https://thediscdb.com/) (MIT) for community disc-to-episode mappings.
This product uses the TMDB API but is not endorsed or certified by TMDB.
