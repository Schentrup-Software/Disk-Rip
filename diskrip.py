#!/usr/bin/env python3
"""
diskrip.py - Automated disc ripping with MakeMKV + TMDB naming.

Scans an optical disc with MakeMKV, identifies the movie or TV disc via TMDB,
proposes Jellyfin/tinyMediaManager-style names, shows a rich confirmation screen,
then rips and files the titles onto the NAS.

Naming conventions produced (matching the existing library):
  Movie:   <movie_root>/Title (Year) [imdb-ttID]/Title (Year) [imdb-ttID] - 1080p.mkv
  TV:      <tv_root>/Show (Year) [imdb-ttID]/Season N/Show (Year) - s01e01.mkv

Stdlib only (urllib for TMDB) - no pip install needed.
"""

import argparse
import csv
import io
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

# Windows console: force UTF-8 so the ✓/⚠/Δ symbols don't crash cp1252, and
# turn on ANSI escape processing so colors render instead of showing raw codes.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if os.name == "nt":
    os.system("")  # enables virtual-terminal (ANSI) processing in the console

# ---------------------------------------------------------------------------
# MakeMKV robot-mode attribute IDs (from apdefs.h AP_ItemAttributeId)
# ---------------------------------------------------------------------------
AP_NAME = 2
AP_CHAPTER_COUNT = 8
AP_DURATION = 9
AP_SOURCE_FILENAME = 16
AP_OUTPUT_FILENAME = 27
AP_VOLUME_NAME = 32
AP_VIDEO_SIZE = 19


# ---------------------------------------------------------------------------
# Small data holders
# ---------------------------------------------------------------------------
class Title:
    """One rippable title on the disc."""

    def __init__(self, tid):
        self.id = tid
        self.duration = 0            # seconds
        self.chapters = 0
        self.source = ""             # e.g. 00800.mpls
        self.output_name = ""        # name MakeMKV will write, e.g. LABEL_t00.mkv
        self.resolution = ""         # e.g. 1920x1080
        # Assignment decided during confirmation:
        self.assign = None           # dict describing what to do, or None = skip

    @property
    def height(self):
        m = re.search(r"\d+x(\d+)", self.resolution)
        return int(m.group(1)) if m else 0

    @property
    def quality(self):
        h = self.height
        if h >= 2000:
            return "2160p"
        if h >= 1000:
            return "1080p"
        if h >= 700:
            return "720p"
        if h > 0:
            return "480p"
        return "480p"


class Disc:
    def __init__(self, drive_index):
        self.drive_index = drive_index
        self.label = ""
        self.titles = []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def hms(seconds):
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def parse_duration(text):
    parts = text.strip().split(":")
    try:
        parts = [int(p) for p in parts]
    except ValueError:
        return 0
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts[-3], parts[-2], parts[-1]
    return h * 3600 + m * 60 + s


def sanitize(name):
    """Make a title safe for a Windows filename, matching TMM's conventions."""
    if name is None:
        return ""
    # "Title: Subtitle" -> "Title - Subtitle"
    name = name.replace(": ", " - ")
    name = name.replace(":", " -")
    # Remaining illegal characters
    name = re.sub(r'[\\/*?"<>|]', "", name)
    name = re.sub(r"\s+", " ", name).strip().rstrip(". ")
    return name


def color(text, code):
    if os.environ.get("NO_COLOR"):
        return text
    return f"\033[{code}m{text}\033[0m"


def bold(t):
    return color(t, "1")


def dim(t):
    return color(t, "2")


def green(t):
    return color(t, "32")


def yellow(t):
    return color(t, "33")


def red(t):
    return color(t, "31")


# ---------------------------------------------------------------------------
# MakeMKV control
# ---------------------------------------------------------------------------
class MakeMKV:
    # Scan floor kept deliberately low so MakeMKV reports every real title;
    # the episode/movie length thresholds are applied later in Python. If this
    # were set to the episode threshold, short titles (extras, and any genuinely
    # short episode) would never be reported at all.
    SCAN_MIN_SECONDS = 20

    def __init__(self, exe):
        self.exe = exe
        if not Path(exe).exists():
            die(f"makemkvcon not found at: {exe}\nFix 'makemkvcon' in your config.")

    # Abort a MakeMKV call if it produces no output at all for this long. A
    # healthy scan/rip emits status or progress lines every second or two; total
    # silence this long means the drive is wedged on an unreadable disc.
    STALL_TIMEOUT = 120

    def _run(self, args, on_line=None, stall_timeout=None):
        """Run makemkvcon, streaming lines to on_line. Returns (rc, lines).

        rc is None if the process stalled (no output for stall_timeout seconds)
        and had to be killed."""
        stall_timeout = stall_timeout or self.STALL_TIMEOUT
        cmd = [self.exe, "-r", f"--minlength={self.SCAN_MIN_SECONDS}"] + args
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        # Read on a background thread so the main thread can enforce a timeout:
        # a blocking read on a wedged pipe would otherwise hang forever.
        q = queue.Queue()

        def reader():
            for raw in proc.stdout:
                q.put(raw)
            q.put(None)  # EOF sentinel

        threading.Thread(target=reader, daemon=True).start()

        lines = []
        stalled = False
        while True:
            try:
                raw = q.get(timeout=stall_timeout)
            except queue.Empty:
                stalled = True
                break
            if raw is None:
                break
            line = raw.rstrip("\n")
            lines.append(line)
            if on_line:
                on_line(line)

        if stalled:
            try:
                proc.kill()
            except Exception:
                pass
            proc.wait()
            return None, lines

        proc.wait()
        return proc.returncode, lines

    @staticmethod
    def _fields(rest):
        return next(csv.reader(io.StringIO(rest)))

    def list_drives(self):
        """Return list of (index, drive_name, disc_label, loaded)."""
        _, lines = self._run(["--cache=1", "info", "disc:9999"])
        drives = []
        for ln in lines:
            if not ln.startswith("DRV:"):
                continue
            f = self._fields(ln[4:])
            if len(f) < 7:
                continue
            index = int(f[0])
            visible = f[1]
            drive_name = f[4]
            disc_label = f[5]
            device = f[6]
            if not drive_name:
                continue
            loaded = visible == "2" or bool(device)
            drives.append((index, drive_name, disc_label, loaded))
        return drives

    def scan(self, drive_index):
        """Scan one disc and return a populated Disc object."""
        disc = Disc(drive_index)
        titles = {}

        def get(tid):
            if tid not in titles:
                titles[tid] = Title(tid)
            return titles[tid]

        def on_line(ln):
            # Surface MakeMKV's own status text live, so a slow (or wedged)
            # scan is visible instead of a silent hang. Errors stick on their
            # own line; routine status overwrites a single live line.
            if ln.startswith("MSG:"):
                f = self._fields(ln[4:])
                text = f[3] if len(f) > 3 else ""
                low = text.lower()
                if any(w in low for w in
                       ("error", "fail", "cannot", "can't", "retry", "scsi", "hash")):
                    print("\r  " + red(text) + " " * 8)
                elif text:
                    print("\r  " + dim(text[:72]), end="", flush=True)

        rc, lines = self._run(["--cache=1", "info", f"disc:{drive_index}"], on_line)
        print()  # end the live status line
        if rc is None:
            die("MakeMKV stalled while scanning the disc "
                f"(no response for {self.STALL_TIMEOUT}s).\n"
                "The disc is likely dirty or scratched, or the drive can't read "
                "it. Clean the disc, reseat it, and try again.")
        for ln in lines:
            if ln.startswith("CINFO:"):
                f = self._fields(ln[6:])
                aid = int(f[0])
                val = f[2] if len(f) > 2 else ""
                if aid in (AP_VOLUME_NAME, AP_NAME) and val and not disc.label:
                    disc.label = val
            elif ln.startswith("TINFO:"):
                f = self._fields(ln[6:])
                tid, aid, val = int(f[0]), int(f[1]), f[3] if len(f) > 3 else ""
                t = get(tid)
                if aid == AP_DURATION:
                    t.duration = parse_duration(val)
                elif aid == AP_CHAPTER_COUNT:
                    t.chapters = int(val) if val.isdigit() else 0
                elif aid == AP_SOURCE_FILENAME:
                    t.source = val
                elif aid == AP_OUTPUT_FILENAME:
                    t.output_name = val
            elif ln.startswith("SINFO:"):
                f = self._fields(ln[6:])
                tid, aid, val = int(f[0]), int(f[2]), f[4] if len(f) > 4 else ""
                if aid == AP_VIDEO_SIZE and val:
                    t = get(tid)
                    if not t.resolution:
                        t.resolution = val

        disc.titles = sorted(titles.values(), key=lambda t: t.id)
        if rc != 0 and not disc.titles:
            die("MakeMKV could not read the disc. Is it inserted and readable?")
        return disc

    def rip(self, drive_index, title, dest_folder):
        """Rip a single title into dest_folder. Returns path to the .mkv."""
        Path(dest_folder).mkdir(parents=True, exist_ok=True)
        before = set(Path(dest_folder).glob("*.mkv"))
        last_pct = -1

        def on_line(ln):
            nonlocal last_pct
            if ln.startswith("PRGV:"):
                f = self._fields(ln[5:])
                try:
                    cur, total, mx = int(f[0]), int(f[1]), int(f[2])
                    pct = int(cur * 100 / mx) if mx else 0
                except (ValueError, IndexError):
                    return
                if pct != last_pct:
                    last_pct = pct
                    bar = "#" * (pct // 3) + "-" * (33 - pct // 3)
                    print(f"\r    ripping [{bar}] {pct:3d}%", end="", flush=True)
            elif ln.startswith("MSG:"):
                f = self._fields(ln[4:])
                if len(f) >= 4 and f[0] in ("5003", "5004", "5010"):
                    print(f"\r    {red(f[3])}{' ' * 20}")

        rc, _ = self._run(
            ["--progress=-same", "mkv", f"disc:{drive_index}", str(title.id), dest_folder],
            on_line,
        )
        print()  # newline after progress bar
        if rc is None:
            print(red(f"    stalled: no progress for {self.STALL_TIMEOUT}s "
                      "(disc may be unreadable at this title) - aborted."))
            return None
        if rc != 0:
            return None
        # Locate the freshly written file
        candidate = Path(dest_folder) / title.output_name
        if candidate.exists():
            return candidate
        after = set(Path(dest_folder).glob("*.mkv")) - before
        if after:
            return max(after, key=lambda p: p.stat().st_mtime)
        return None


# ---------------------------------------------------------------------------
# TMDB client
# ---------------------------------------------------------------------------
class TMDB:
    BASE = "https://api.themoviedb.org/3"

    def __init__(self, api_key, language="en"):
        self.api_key = api_key
        self.language = language

    def _get(self, path, **params):
        params["api_key"] = self.api_key
        params.setdefault("language", self.language)
        url = f"{self.BASE}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 401:
                die("TMDB rejected the API key (401). Check 'tmdb_api_key' in config.")
            raise
        except urllib.error.URLError as e:
            die(f"Could not reach TMDB: {e.reason}")

    def search_movie(self, query, year=None):
        p = {"query": query}
        if year:
            p["year"] = year
        return self._get("/search/movie", **p).get("results", [])

    def movie_details(self, movie_id):
        d = self._get(f"/movie/{movie_id}", append_to_response="external_ids")
        return d

    def search_tv(self, query, year=None):
        p = {"query": query}
        if year:
            p["first_air_date_year"] = year
        return self._get("/search/tv", **p).get("results", [])

    def tv_details(self, tv_id):
        return self._get(f"/tv/{tv_id}", append_to_response="external_ids")

    def season(self, tv_id, season_number):
        return self._get(f"/tv/{tv_id}/season/{season_number}").get("episodes", [])


# ---------------------------------------------------------------------------
# Identification / proposal building
# ---------------------------------------------------------------------------
def guess_query_from_label(label):
    """Turn an ugly disc volume label into a searchable title guess + hints.

    Handles: 'THE_OFFICE_S1D2', 'KORRA_SEASON_2_DISC_1',
    'Avatar: The Last Airbender Book One: Water Disc 1'.
    """
    hints = {"season": None, "disc": None}
    if not label:
        return "", hints
    text = label

    # --- Season: 'S2', 'Season 2', 'Book Two', 'Volume 3' ---
    ms = re.search(r"\bS(?:EASON)?[ _]?0*(\d{1,2})\b", text, re.I)
    if ms:
        hints["season"] = int(ms.group(1))
    else:
        mb = re.search(r"\b(?:BOOK|VOLUME|VOL)[ _]+(\w+)", text, re.I)
        if mb:
            tok = mb.group(1).lower()
            hints["season"] = int(tok) if tok.isdigit() else WORD_NUM.get(tok)

    # --- Disc: 'D1', 'Disc 1', 'Disk 2' ---
    md = re.search(r"\bD(?:ISC|ISK)?[ _]?0*(\d{1,2})\b", text, re.I)
    if md:
        hints["disc"] = int(md.group(1))

    # --- Title guess: everything before the first season/disc/book keyword ---
    name = re.split(
        r"[ _\-]S(?:EASON)?[ _]?\d|[ _\-]D(?:ISC|ISK)?[ _]?\d|\b(?:BOOK|VOLUME|VOL)\b",
        text,
        maxsplit=1,
        flags=re.I,
    )[0]
    name = re.sub(r"\b(DVD|BLU-?RAY|BD|VIDEO)\b", " ", name, flags=re.I)
    name = re.sub(r"[_\.]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" :-")
    if name.isupper():
        name = name.title()
    return name, hints


WORD_NUM = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20,
}


def classify(disc, movie_min_sec, min_len_sec):
    long_titles = [t for t in disc.titles if t.duration >= movie_min_sec]
    ep_titles = [t for t in disc.titles if min_len_sec <= t.duration < movie_min_sec]
    if len(ep_titles) >= 2 and len(long_titles) <= 1:
        return "tv"
    if len(long_titles) >= 1 and len(ep_titles) <= 1:
        return "movie"
    return "tv" if len(ep_titles) > len(long_titles) else "movie"


def year_of(date_str):
    if date_str and len(date_str) >= 4:
        return date_str[:4]
    return ""


def imdb_from(details):
    ext = details.get("external_ids") or {}
    return ext.get("imdb_id") or ""


# ---------------------------------------------------------------------------
# Proposal objects (hold the resolved identity + per-title plan)
# ---------------------------------------------------------------------------
class MovieProposal:
    def __init__(self, tmdb, disc, movie_min_sec):
        self.tmdb = tmdb
        self.disc = disc
        self.movie_min_sec = movie_min_sec
        self.details = None
        self.main_title = None
        self._pick_main_title()

    def _pick_main_title(self):
        longs = [t for t in self.disc.titles if t.duration >= self.movie_min_sec]
        pool = longs or self.disc.titles
        self.main_title = max(pool, key=lambda t: t.duration) if pool else None

    def identify(self, query, year=None):
        results = self.tmdb.search_movie(query, year)
        if not results:
            return []
        return results

    def choose(self, movie_id):
        self.details = self.tmdb.movie_details(movie_id)

    @property
    def title(self):
        return self.details.get("title", "") if self.details else ""

    @property
    def year(self):
        return year_of(self.details.get("release_date", "")) if self.details else ""

    @property
    def imdb(self):
        return imdb_from(self.details) if self.details else ""

    def folder_and_file(self):
        base = sanitize(self.title)
        y = self.year
        idpart = f" [imdb-{self.imdb}]" if self.imdb else ""
        folder = f"{base} ({y}){idpart}" if y else f"{base}{idpart}"
        q = self.main_title.quality if self.main_title else "480p"
        fname = f"{folder} - {q}.mkv"
        return folder, fname


class TvProposal:
    def __init__(self, tmdb, disc, min_len_sec, movie_min_sec):
        self.tmdb = tmdb
        self.disc = disc
        self.min_len_sec = min_len_sec
        self.movie_min_sec = movie_min_sec
        self.details = None
        self.season_number = 1
        self.start_episode = 1
        self.episodes = []           # TMDB episodes for the season
        self.skip = set()            # title ids the user chose to drop
        self.force_include = set()   # title ids force-added back from excluded

    # --- title buckets -----------------------------------------------------
    def episode_titles(self):
        """Episode-length titles: long enough, but shorter than a feature.

        The upper bound (movie_min_sec) filters out 'Play All' titles - the
        multi-hour concatenation of every episode that TV discs include and
        that must never be ripped as a single episode."""
        return [t for t in self.disc.titles
                if self.min_len_sec <= t.duration < self.movie_min_sec]

    def excluded_long(self):
        """Play-all / feature-length titles, excluded by default."""
        return [t for t in self.disc.titles if t.duration >= self.movie_min_sec]

    def excluded_short(self):
        """Sub-threshold titles (extras, menus, recaps), excluded by default."""
        return [t for t in self.disc.titles if t.duration < self.min_len_sec]

    def active_titles(self):
        """The titles that will actually be ripped, in disc order."""
        pool = {t.id: t for t in self.episode_titles()}
        for t in self.disc.titles:
            if t.id in self.force_include:
                pool[t.id] = t
        return [t for tid, t in sorted(pool.items()) if tid not in self.skip]

    def identify(self, query, year=None):
        return self.tmdb.search_tv(query, year)

    def choose(self, tv_id):
        self.details = self.tmdb.tv_details(tv_id)

    def load_season(self):
        if not self.details:
            return
        try:
            self.episodes = self.tmdb.season(self.details["id"], self.season_number)
        except urllib.error.HTTPError:
            self.episodes = []

    @property
    def title(self):
        return self.details.get("name", "") if self.details else ""

    @property
    def year(self):
        return year_of(self.details.get("first_air_date", "")) if self.details else ""

    @property
    def imdb(self):
        return imdb_from(self.details) if self.details else ""

    def show_folder(self):
        base = sanitize(self.title)
        y = self.year
        idpart = f" [imdb-{self.imdb}]" if self.imdb else ""
        return f"{base} ({y}){idpart}" if y else f"{base}{idpart}"

    def episode_meta(self, ep_num):
        """TMDB episode dict for an episode number, or a blank placeholder."""
        for e in self.episodes:
            if e.get("episode_number") == ep_num:
                return e
        return {"episode_number": ep_num, "name": "", "overview": "",
                "runtime": None, "air_date": ""}

    def plan(self):
        """Map active titles sequentially to episodes from start_episode."""
        return [(t, self.episode_meta(self.start_episode + i))
                for i, t in enumerate(self.active_titles())]

    def file_for(self, ep_num):
        base = sanitize(self.title)
        y = self.year
        show = f"{base} ({y})" if y else base
        s = self.season_number
        return f"{show} - s{s:02d}e{ep_num:02d}.mkv"


# ---------------------------------------------------------------------------
# Confirmation screens
# ---------------------------------------------------------------------------
def choose_from_results(results, kind, tmdb):
    """Let the user pick which TMDB match is correct."""
    if not results:
        return None
    print(f"\n  TMDB {kind} matches:")
    for i, r in enumerate(results[:8]):
        if kind == "movie":
            name = r.get("title", "?")
            date = r.get("release_date", "")
        else:
            name = r.get("name", "?")
            date = r.get("first_air_date", "")
        y = year_of(date)
        overview = (r.get("overview") or "").strip()
        print(f"    [{i}] {bold(name)} ({y or '----'})  "
              f"{dim('tmdb:' + str(r.get('id')))}")
        if overview:
            print(f"        {dim(overview[:100] + ('...' if len(overview) > 100 else ''))}")
    while True:
        raw = input(f"  Pick match [0-{min(len(results),8)-1}], or type a new title to re-search: ").strip()
        if raw == "":
            return results[0]
        if raw.isdigit() and int(raw) < min(len(results), 8):
            return results[int(raw)]
        # treat as a new query
        return ("research", raw)


def initial_pick(p, results, kind, interactive):
    """Resolve the TMDB match. Interactive + ambiguous -> show the picker so
    the user disambiguates (e.g. the 2005 vs 2024 'Avatar' remake) before we
    commit. Unattended or single result -> take the top hit."""
    if not interactive or len(results) <= 1:
        return results[0]
    while True:
        pick = choose_from_results(results, kind, p.tmdb)
        if isinstance(pick, tuple) and pick[0] == "research":
            new = p.identify(pick[1])
            if new:
                results = new
                continue
            print(red("  no matches for that title"))
            continue
        return pick or results[0]


def show_movie_proposal(p):
    folder, fname = p.folder_and_file()
    print("\n" + bold("=" * 70))
    print(bold("  MOVIE"))
    print(bold("=" * 70))
    print(f"  Identified : {bold(p.title)} ({p.year})   imdb-{p.imdb or '?'}")
    plot = (p.details.get("overview") or "").strip()
    if plot:
        print(f"  Plot       : {dim(plot[:150] + ('...' if len(plot) > 150 else ''))}")
    tmdb_rt = p.details.get("runtime") or 0
    mt = p.main_title
    delta = ""
    if tmdb_rt and mt:
        d = mt.duration - tmdb_rt * 60
        flag = red(" ⚠ differs") if abs(d) > 8 * 60 else green(" ✓")
        delta = f"  (TMDB {tmdb_rt}m, disc {hms(mt.duration)}{flag})"
    print(f"  Main title : t{mt.id:02d}  {hms(mt.duration)}  {mt.resolution or '?'}  "
          f"{mt.chapters}ch{delta}")
    print(f"  {dim('Folder')}     : {folder}")
    print(f"  {dim('File')}       : {folder}\\{fname}")


def show_tv_proposal(p):
    print("\n" + bold("=" * 70))
    print(bold(f"  TV  —  Season {p.season_number}, starting at episode {p.start_episode}"))
    print(bold("=" * 70))
    print(f"  Identified : {bold(p.title)} ({p.year})   imdb-{p.imdb or '?'}")
    print(f"  {dim('Show folder')}: {p.show_folder()}\\Season {p.season_number}\\")
    if not p.episodes:
        print(yellow("  ! No TMDB episode data for this season "
                     "(runtimes/titles unavailable) - mapping by disc order only."))
    print()
    print(f"  {'title':<6}{'length':<9}{'res':<11}{'ep':<7}{'name / air / TMDB-runtime'}")
    print(f"  {dim('-' * 62)}")
    for t, ep in p.plan():
        epnum = ep.get("episode_number")
        epname = ep.get("name") or ""
        air = ep.get("air_date") or ""
        rt = ep.get("runtime")
        # runtime cross-check
        note = ""
        if rt:
            d = t.duration - rt * 60
            if abs(d) > 6 * 60:
                note = red(f"⚠ Δ{'+' if d>0 else ''}{int(d/60)}m")
            else:
                note = green("✓")
            note += dim(f" TMDB {rt}m")
        eplabel = f"s{p.season_number:02d}e{epnum:02d}"
        namecell = f"{epname[:34]:<34}"  # pad plain text first, color after
        print(f"  t{t.id:02d}   {hms(t.duration):<9}{(t.resolution or '?'):<11}"
              f"{eplabel:<7}{bold(namecell)} {dim(air)} {note}")
        overview = (ep.get("overview") or "").strip()
        if overview:
            print(f"         {dim(overview[:96] + ('...' if len(overview) > 96 else ''))}")
    # Excluded titles - shown so the user can 'keep' one if the guess is wrong
    long_ex = [t for t in p.excluded_long() if t.id not in p.force_include]
    if long_ex:
        print(yellow(f"\n  Excluded {len(long_ex)} feature-length title(s) "
                     f"(≥ {p.movie_min_sec//60}m — almost certainly 'Play All'): "
                     + ", ".join(f"t{t.id:02d}={hms(t.duration)}" for t in long_ex)))
    short_ex = [t for t in p.excluded_short()]
    if short_ex:
        print(dim(f"  Excluded {len(short_ex)} short title(s) "
                  f"(< {p.min_len_sec//60}m, likely extras/menus): "
                  + ", ".join(f"t{t.id:02d}={hms(t.duration)}" for t in short_ex)))
    if p.skip:
        print(dim("  User-skipped: " + ", ".join(f"t{i:02d}" for i in sorted(p.skip))))


# ---------------------------------------------------------------------------
# Interactive edit loops
# ---------------------------------------------------------------------------
def edit_movie(p):
    while True:
        show_movie_proposal(p)
        print("\n  " + bold("[Enter]") + " rip & file    "
              + bold("t") + "itle <name>    "
              + bold("y") + "ear <yyyy>    "
              + bold("q") + "uit")
        cmd = input("  > ").strip()
        if cmd == "":
            return True
        if cmd == "q":
            return False
        low = cmd.lower()
        if low.startswith("title") or low.startswith("t "):
            q = cmd.split(" ", 1)[1] if " " in cmd else ""
            _reidentify_movie(p, q)
        elif low.startswith("year") or low.startswith("y "):
            y = cmd.split(" ", 1)[1].strip() if " " in cmd else ""
            _reidentify_movie(p, p.title or "", y)
        else:
            print(red("  ? unknown command"))


def _reidentify_movie(p, query, year=None):
    if not query:
        query = input("  Search title: ").strip()
    results = p.identify(query, year)
    if not results:
        print(red("  no matches"))
        return
    pick = choose_from_results(results, "movie", p.tmdb)
    if isinstance(pick, tuple) and pick[0] == "research":
        return _reidentify_movie(p, pick[1])
    if pick:
        p.choose(pick["id"])


def edit_tv(p):
    while True:
        show_tv_proposal(p)
        print("\n  " + bold("[Enter]") + " rip & file    "
              + bold("season") + " <n>    "
              + bold("start") + " <ep#>    "
              + bold("title") + " <name>")
        print("  " + bold("skip") + " <tid..>   drop titles     "
              + bold("keep") + " <tid..>   re-include titles     "
              + bold("q") + "uit")
        cmd = input("  > ").strip()
        if cmd == "":
            return True
        if cmd == "q":
            return False
        low = cmd.lower()
        parts = cmd.split()
        if low.startswith("season") and len(parts) > 1 and parts[1].isdigit():
            p.season_number = int(parts[1])
            p.load_season()
        elif low.startswith("start") and len(parts) > 1 and parts[1].isdigit():
            p.start_episode = int(parts[1])
        elif low.startswith("title"):
            q = cmd.split(" ", 1)[1] if " " in cmd else ""
            _reidentify_tv(p, q)
        elif low.startswith("skip"):
            for tok in parts[1:]:
                tid = _tid(tok)
                if tid is not None:
                    p.skip.add(tid)
                    p.force_include.discard(tid)
        elif low.startswith("keep"):
            for tok in parts[1:]:
                tid = _tid(tok)
                if tid is not None:
                    p.skip.discard(tid)
                    p.force_include.add(tid)
        else:
            print(red("  ? unknown command"))


def _tid(token):
    """Parse a title id from 'T05', 't5', or '5'."""
    m = re.search(r"\d+", token)
    return int(m.group()) if m else None


def _reidentify_tv(p, query, year=None):
    if not query:
        query = input("  Search show: ").strip()
    results = p.identify(query, year)
    if not results:
        print(red("  no matches"))
        return
    pick = choose_from_results(results, "tv", p.tmdb)
    if isinstance(pick, tuple) and pick[0] == "research":
        return _reidentify_tv(p, pick[1])
    if pick:
        p.choose(pick["id"])
        p.load_season()


# ---------------------------------------------------------------------------
# NFO (optional, minimal - helps Jellyfin/TMM match reliably)
# ---------------------------------------------------------------------------
def write_movie_nfo(path, p):
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<movie>\n'
        f"  <title>{_x(p.title)}</title>\n"
        f"  <year>{p.year}</year>\n"
        f'  <uniqueid type="tmdb">{p.details.get("id","")}</uniqueid>\n'
        f'  <uniqueid type="imdb" default="true">{p.imdb}</uniqueid>\n'
        f"  <id>{p.imdb}</id>\n</movie>\n"
    )
    path.write_text(xml, encoding="utf-8")


def write_episode_nfo(path, p, ep):
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<episodedetails>\n'
        f"  <title>{_x(ep.get('name',''))}</title>\n"
        f"  <season>{p.season_number}</season>\n"
        f"  <episode>{ep.get('episode_number','')}</episode>\n"
        f"  <aired>{ep.get('air_date','')}</aired>\n"
        f"  <plot>{_x(ep.get('overview',''))}</plot>\n</episodedetails>\n"
    )
    path.write_text(xml, encoding="utf-8")


def _x(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------------------
# Rip + file execution
# ---------------------------------------------------------------------------
def execute_movie(mk, p, cfg, dry_run):
    folder, fname = p.folder_and_file()
    dest_dir = Path(cfg["movie_root"]) / folder
    final = dest_dir / fname
    print(f"\n{bold('Ripping main feature')} -> {final}")
    if final.exists():
        print(yellow(f"  ! Already exists, leaving it untouched: {final}"))
        return
    if dry_run:
        print(dim("  [dry-run] would rip title "
                  f"t{p.main_title.id:02d} and move to the path above"))
        return
    ripped = mk.rip(p.disc.drive_index, p.main_title, cfg["work_dir"])
    if not ripped:
        die("Rip failed - MakeMKV returned an error (see messages above).")
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(ripped), str(final))
    print(green(f"  ✓ {final}"))
    if cfg.get("write_nfo"):
        write_movie_nfo(dest_dir / (fname[:-4] + ".nfo"), p)


def execute_tv(mk, p, cfg, dry_run):
    season_dir = Path(cfg["tv_root"]) / p.show_folder() / f"Season {p.season_number}"
    plan = p.plan()
    print(f"\n{bold('Ripping ' + str(len(plan)) + ' episode(s)')} -> {season_dir}")
    for idx, (t, ep) in enumerate(plan, 1):
        epnum = ep.get("episode_number")
        fname = p.file_for(epnum)
        final = season_dir / fname
        print(f"\n  [{idx}/{len(plan)}] t{t.id:02d} ({hms(t.duration)}) "
              f"-> s{p.season_number:02d}e{epnum:02d}  {ep.get('name','')}")
        if final.exists():
            print(yellow(f"      ! Already exists, leaving it untouched: {final}"))
            continue
        if dry_run:
            print(dim(f"      [dry-run] would move to {final}"))
            continue
        ripped = mk.rip(p.disc.drive_index, t, cfg["work_dir"])
        if not ripped:
            print(red(f"      ! rip failed for t{t.id:02d}, skipping"))
            continue
        season_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(ripped), str(final))
        print(green(f"      ✓ {final}"))
        if cfg.get("write_nfo"):
            write_episode_nfo(season_dir / (fname[:-4] + ".nfo"), p, ep)


# ---------------------------------------------------------------------------
# Config / CLI
# ---------------------------------------------------------------------------
def die(msg):
    print(red("\nERROR: ") + msg, file=sys.stderr)
    sys.exit(1)


def load_config(path):
    p = Path(path)
    if not p.exists():
        die(f"Config not found: {path}\n"
            "Copy config.example.json to config.json and fill in your TMDB key.")
    cfg = json.loads(p.read_text(encoding="utf-8"))
    if "PASTE_YOUR" in cfg.get("tmdb_api_key", ""):
        die("Set 'tmdb_api_key' in your config (free key from themoviedb.org).")
    Path(cfg["work_dir"]).mkdir(parents=True, exist_ok=True)
    return cfg


def pick_drive(mk, requested):
    drives = mk.list_drives()
    if not drives:
        die("No optical drives found.")
    loaded = [d for d in drives if d[3]]
    if requested is not None:
        return requested
    if len(loaded) == 1:
        idx = loaded[0][0]
        print(f"Using drive {idx}: {loaded[0][1]}  disc: {loaded[0][2] or '(no label)'}")
        return idx
    print("Drives:")
    for idx, name, label, is_loaded in drives:
        tag = green("disc: " + (label or "(unlabeled)")) if is_loaded else dim("empty")
        print(f"  [{idx}] {name}  {tag}")
    raw = input("Drive index to rip: ").strip()
    if not raw.isdigit():
        die("No drive selected.")
    return int(raw)


def main():
    ap = argparse.ArgumentParser(description="Rip a disc with MakeMKV and file it by TMDB naming.")
    ap.add_argument("--config", default=str(Path(__file__).parent / "config.json"))
    ap.add_argument("--drive", type=int, default=None, help="Drive index (default: auto).")
    ap.add_argument("--type", choices=["movie", "tv", "auto"], default="auto")
    ap.add_argument("--title", default=None, help="Override the title guess for TMDB search.")
    ap.add_argument("--season", type=int, default=None, help="Force season number (TV).")
    ap.add_argument("--start-episode", type=int, default=None, help="First episode # on this disc (TV).")
    ap.add_argument("--dry-run", action="store_true", help="Identify and show the plan, but do not rip/move.")
    ap.add_argument("--yes", action="store_true", help="Skip confirmation (unattended).")
    ap.add_argument("--list-drives", action="store_true")
    args = ap.parse_args()

    cfg = load_config(args.config)
    min_len = int(cfg.get("min_length_minutes", 15)) * 60
    movie_min = int(cfg.get("movie_min_minutes", 70)) * 60
    mk = MakeMKV(cfg["makemkvcon"])
    tmdb = TMDB(cfg["tmdb_api_key"], cfg.get("language", "en"))

    if args.list_drives:
        for idx, name, label, loaded in mk.list_drives():
            print(f"[{idx}] {name}  {'disc: ' + (label or '(unlabeled)') if loaded else 'empty'}")
        return

    drive_index = pick_drive(mk, args.drive)
    print("\nScanning disc (this reads the disc structure, no ripping yet)...")
    disc = mk.scan(drive_index)
    if not disc.titles:
        die("No titles found on the disc.")
    print(f"Disc label: {bold(disc.label or '(none)')}   titles: {len(disc.titles)}")

    disc_type = args.type
    if disc_type == "auto":
        disc_type = classify(disc, movie_min, min_len)
        print(f"Auto-detected type: {bold(disc_type.upper())} "
              f"{dim('(override with --type)')}")

    query, hints = guess_query_from_label(disc.label)
    if args.title:
        query = args.title

    if disc_type == "movie":
        p = MovieProposal(tmdb, disc, movie_min)
        results = p.identify(query) if query else []
        if not results:
            q = input(f"  Couldn't auto-match. Movie title{f' [{query}]' if query else ''}: ").strip() or query
            results = p.identify(q)
        if not results:
            die("No TMDB movie matches. Re-run with --title \"Exact Name\".")
        p.choose(initial_pick(p, results, "movie", not args.yes)["id"])
        if not args.yes:
            if not edit_movie(p):
                print("Aborted."); return
        else:
            show_movie_proposal(p)
        execute_movie(mk, p, cfg, args.dry_run)

    else:
        p = TvProposal(tmdb, disc, min_len, movie_min)
        p.season_number = args.season or hints.get("season") or 1
        p.start_episode = args.start_episode or 1
        results = p.identify(query) if query else []
        if not results:
            q = input(f"  Couldn't auto-match. Show title{f' [{query}]' if query else ''}: ").strip() or query
            results = p.identify(q)
        if not results:
            die("No TMDB show matches. Re-run with --title \"Exact Name\".")
        p.choose(initial_pick(p, results, "tv", not args.yes)["id"])
        p.load_season()
        if not args.yes:
            if not edit_tv(p):
                print("Aborted."); return
        else:
            show_tv_proposal(p)
        execute_tv(mk, p, cfg, args.dry_run)

    print(green("\nDone."))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
