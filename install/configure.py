#!/usr/bin/env python3
"""
configure.py - write config.json for Disk-Rip (used by install/setup.sh on macOS;
works on Linux too). Stdlib only.

Asks for the TMDB key and folders, locates makemkvcon / ffmpeg / ffprobe, writes
config.json next to config.example.json, then checks that everything works.
Re-running is safe: your current values become the defaults.

  python3 install/configure.py
  python3 install/configure.py --non-interactive --tmdb-key abc123 \\
      --rips-root smb://nas.example.com/rips --movie-root /Volumes/rips/Movies
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TTY = sys.stdout.isatty()


def c(text, code):
    return f"\033[{code}m{text}\033[0m" if TTY else text


def info(m):
    print(c(m, "36"))


def ok(m):
    print(c("  OK  ", "32") + m)


def warn(m):
    print(c("  !   ", "33") + m)


def load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def looks_windows(value):
    """True for the Windows placeholders in config.example.json."""
    return isinstance(value, str) and ("\\" in value or re.match(r"^[A-Za-z]:", value))


def find_makemkvcon():
    for cand in ("/Applications/MakeMKV.app/Contents/MacOS/makemkvcon",
                 str(Path.home() / "Applications/MakeMKV.app/Contents/MacOS/makemkvcon")):
        if Path(cand).is_file():
            return cand
    return shutil.which("makemkvcon") or ""


def find_tool(name):
    for cand in (shutil.which(name), f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}",
                 str(Path.home() / f".homebrew/bin/{name}")):
        if cand and Path(cand).is_file():
            return cand
    return ""


def smb_to_local(spec):
    """smb://host/share/sub -> /Volumes/share/sub (where macOS mounts it)."""
    m = re.match(r"^smb://(?:[^@/]+@)?[^/]+/([^/]+)(/.*)?$", spec or "", re.I)
    if not m:
        return spec
    return str(Path("/Volumes") / m.group(1) / (m.group(2) or "").strip("/")).rstrip("/")


def main():
    ap = argparse.ArgumentParser(description="Write Disk-Rip's config.json.")
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--tmdb-key")
    ap.add_argument("--rips-root", help="Folder of already-ripped discs (path or smb://host/share).")
    ap.add_argument("--movie-root", help="Movie library folder (organized movies go here).")
    ap.add_argument("--tv-root", help="TV library folder (only used when ripping TV discs).")
    ap.add_argument("--work-dir")
    ap.add_argument("--makemkv-key", help="MakeMKV licence / beta key to register.")
    ap.add_argument("--non-interactive", action="store_true")
    args = ap.parse_args()
    interactive = not args.non_interactive and sys.stdin.isatty()

    def ask(label, current, hint=""):
        if not interactive:
            return current
        if hint:
            print(c("      " + hint, "2"))
        try:
            ans = input(f"  {label}" + (f" [{current}]" if current else "") + ": ").strip()
        except EOFError:
            return current
        return ans or current

    example = load_json(ROOT / "config.example.json")
    if not example:
        sys.exit("ERROR: config.example.json not found in the repo root.")
    existing = load_json(args.config) or {}
    cfg = dict(example)
    cfg.update(existing)
    # the template carries Windows placeholders - never offer those as defaults here
    for key in ("makemkvcon", "tv_root", "movie_root", "work_dir"):
        if looks_windows(cfg.get(key)):
            cfg[key] = ""
    if "PASTE_" in str(cfg.get("tmdb_api_key", "")):
        cfg["tmdb_api_key"] = ""

    info("\nLocating tools ...")
    makemkv = find_makemkvcon()
    ffmpeg, ffprobe = find_tool("ffmpeg"), find_tool("ffprobe")
    if makemkv:
        ok(f"makemkvcon: {makemkv}")
        cfg["makemkvcon"] = makemkv
    else:
        warn("MakeMKV not found - fine for organizing already-ripped folders; "
             "needed only to rip discs (brew install --cask makemkv).")
    if ffprobe:
        ok(f"ffprobe: {ffprobe}")
        cfg["ffprobe"] = ffprobe
    else:
        warn("ffprobe not found - the Library page needs it (brew install ffmpeg).")
    if ffmpeg:
        ok(f"ffmpeg: {ffmpeg}")
        cfg["ffmpeg"] = ffmpeg

    info("\nConfiguring ...")
    cfg["tmdb_api_key"] = args.tmdb_key or ask(
        "TMDB v3 API key", cfg.get("tmdb_api_key", ""),
        "Free: themoviedb.org/settings/api (the short 'API Key', not the long token).")
    cfg["rips_root"] = args.rips_root or ask(
        "Folder of discs you've already ripped", cfg.get("rips_root", ""),
        "A path (/Volumes/rips) or the share's address (smb://nas.example.com/rips). "
        "Enter to skip.")
    local_rips = smb_to_local(cfg["rips_root"]) if cfg["rips_root"] else ""
    movie_default = cfg.get("movie_root") or (f"{local_rips}/Movies" if local_rips else "")
    cfg["movie_root"] = args.movie_root or ask(
        "Movie library folder", movie_default,
        "Organized movies are moved here. Keep it on the SAME share as the rips: a "
        "move is then an instant rename instead of a copy over the network.")
    cfg["tv_root"] = args.tv_root or ask(
        "TV library folder", cfg.get("tv_root", ""),
        "Only used when ripping TV discs. Enter to skip.")
    cfg["work_dir"] = args.work_dir or ask(
        "Local scratch folder", cfg.get("work_dir") or str(ROOT / "_work"),
        "Rips land here before moving to the library; also holds caches and move logs.")

    Path(args.config).write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    ok(f"wrote {args.config}")

    if makemkv:
        key = args.makemkv_key
        if not key and interactive:
            print()
            warn("MakeMKV needs a licence key, or the free beta key "
                 "(forum.makemkv.com/forum/viewtopic.php?t=1053).")
            key = ask("MakeMKV key to register now (Enter to do it later in the app)", "")
        if key:
            r = subprocess.run([makemkv, "reg", key], capture_output=True, text=True)
            ok("MakeMKV registered") if r.returncode == 0 else \
                warn("MakeMKV registration failed - enter the key in the MakeMKV app instead.")

    info("\nVerifying ...")
    key = cfg.get("tmdb_api_key")
    if key:
        try:
            with urllib.request.urlopen(
                    f"https://api.themoviedb.org/3/movie/550?api_key={key}", timeout=20) as r:
                title = json.loads(r.read().decode("utf-8")).get("title")
            ok(f"TMDB key works (test lookup: '{title}')")
        except urllib.error.HTTPError as e:
            warn(f"TMDB rejected the key (HTTP {e.code}) - use the v3 'API Key' from "
                 "themoviedb.org/settings/api.")
        except Exception as e:
            warn(f"couldn't reach TMDB to test the key ({e}).")
    else:
        warn("no TMDB key set - add 'tmdb_api_key' to config.json before running.")
    if ffmpeg:
        try:
            protos = subprocess.run([ffmpeg, "-hide_banner", "-protocols"],
                                    capture_output=True, text=True, timeout=15).stdout
            if "bluray" in protos:
                ok("ffmpeg has the bluray protocol (thumbnails straight from a disc)")
            else:
                print(c("      note: this ffmpeg has no bluray protocol, so the rip page "
                        "shows no thumbnails from the disc\n            (brew install "
                        "ffmpeg-full adds it). Library-page thumbnails work regardless.", "2"))
        except Exception:
            pass
    if local_rips:
        if Path(local_rips).is_dir():
            ok(f"rips folder is reachable: {local_rips}")
        else:
            warn(f"{local_rips} isn't mounted right now - Disk-Rip will try to connect "
                 "to the share when you scan (or connect in Finder: Go > Connect to Server).")
    if cfg.get("movie_root") and local_rips and \
            Path(cfg["movie_root"]).parts[:3] != Path(local_rips).parts[:3]:
        warn("the movie library is on a different volume than the rips - moves will "
             "copy every file across the network instead of renaming it.")

    info("\n=== Done ===\n")
    print("Next:")
    print("  Organize already-ripped folders (web UI):   python3 src/webapp.py --library")
    print("  ...or preview the plan in the terminal:     python3 src/library.py --dry-run")
    if makemkv:
        print("  Rip a disc (close the MakeMKV app first):   python3 src/webapp.py")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)
