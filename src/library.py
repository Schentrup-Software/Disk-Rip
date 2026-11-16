#!/usr/bin/env python3
"""
library.py - Organize a folder of discs you have ALREADY ripped.

Where diskrip.py starts from an optical disc, this starts from a folder such as

    /Volumes/rips/
        Coco (2017)/Coco (2017).mkv
        Coco - disk 2/Coco_t04.mkv ...
        Coco Bonus Disc/Deleted Scene - To the Bridge.mkv ...
        IROBOT_US/title_t00.mkv ... title_t22.mkv

and turns it into the same library layout diskrip.py produces:

    <movie_root>/Coco (2017) [imdb-tt2380307]/Coco (2017) [imdb-tt2380307] - 2160p.mkv
    <movie_root>/Coco (2017) [imdb-tt2380307]/Deleted Scenes/To the Bridge.mkv

How it works (see HOW-IT-WORKS.md, "Library mode"):
  1. every top-level folder is one ripped disc; ffprobe reads each file's
     length, resolution, audio-track and chapter counts (headers only),
  2. disc folders that are the same movie ("X", "X - disk 2", "X Bonus Disc")
     are merged into one group,
  3. each group is identified on TMDB, using the main feature's runtime to pick
     between same-named films,
  4. every file gets a role: the main feature, an extra (with a Plex/Jellyfin
     extras category), or skipped (menus, warning loops, duplicates),
  5. after you confirm, files are MOVED (renamed) into place. Nothing is ever
     overwritten or deleted, and every run writes a log that --undo can reverse.

Movies only: a folder that looks like a TV disc is reported and left alone.
Stdlib only; needs ffprobe (part of ffmpeg) on the machine.
"""

import argparse
import errno
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import diskrip as dr
import libdb
import paths

VIDEO_EXT = {".mkv", ".mp4", ".m4v", ".mov", ".avi", ".ts", ".m2ts", ".mpg",
             ".mpeg", ".wmv", ".webm"}

# Extras sub-folders understood by both Plex and Jellyfin.
CATEGORIES = ["Behind The Scenes", "Deleted Scenes", "Featurettes", "Interviews",
              "Scenes", "Shorts", "Trailers", "Other"]

# Existing sub-folder names (lower-cased) -> canonical category.
CATEGORY_ALIASES = {
    "behind the scenes": "Behind The Scenes", "behindthescenes": "Behind The Scenes",
    "making of": "Behind The Scenes",
    "deleted scenes": "Deleted Scenes", "deleted scene": "Deleted Scenes",
    "deleted": "Deleted Scenes",
    "featurettes": "Featurettes", "featurette": "Featurettes",
    "interviews": "Interviews", "interview": "Interviews",
    "scenes": "Scenes", "scene": "Scenes",
    "shorts": "Shorts", "short": "Shorts",
    "trailers": "Trailers", "trailer": "Trailers",
    "other": "Other", "others": "Other", "extras": "Other", "extra": "Other",
    "bonus": "Other", "special features": "Other", "clips": "Other",
    "samples": "Other",
}

ROLES = ("main", "extra", "skip")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB", "MB") else f"{n:.1f} {unit}"
        n /= 1024.0


def quality_of(width, height):
    """Same labels diskrip.py uses, but robust to cropped frames (uses width too)."""
    if width >= 3000 or height >= 2000:
        return "2160p"
    if width >= 1800 or height >= 1000:
        return "1080p"
    if width >= 1200 or height >= 700:
        return "720p"
    return "480p"


def _norm(text):
    """Lower-case alphanumeric words only - for comparing titles."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _fid(root, path):
    rel = os.path.relpath(str(path), str(root))
    return hashlib.sha1(rel.encode("utf-8", "replace")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Locating the source folder (accepts a path or, on macOS, an smb:// URL)
# ---------------------------------------------------------------------------
def resolve_root(spec, work_dir=None):
    """Return the local Path of the rips folder `spec`: a normal path or, on
    macOS, an SMB URL such as smb://nas.example.com/rips/ (see
    paths.resolve_spec). Raises paths.RootError if it isn't usable."""
    spec = (spec or "").strip()
    if not spec:
        raise paths.RootError("rips", "is not set. Set it in config.json or pass --root.")
    local = paths.resolve_spec("rips", spec)
    return paths.validate_root("rips", local, must_write=False, work_dir=work_dir)


# ---------------------------------------------------------------------------
# ffprobe (headers only - fast even over SMB) with an on-disk cache
# ---------------------------------------------------------------------------
def find_ffprobe(cfg):
    cands = []
    if cfg.get("ffprobe"):
        cands.append(cfg["ffprobe"])
    ff = cfg.get("ffmpeg") or ""
    if ff and Path(ff).exists():
        sib = Path(ff).with_name("ffprobe" + Path(ff).suffix)
        cands.append(str(sib))
    cands += ["ffprobe", "/opt/homebrew/bin/ffprobe", "/usr/local/bin/ffprobe"]
    for c in cands:
        if Path(c).exists():
            return str(c)
        w = shutil.which(c)
        if w:
            return w
    return ""


def find_ffmpeg(cfg):
    for c in (cfg.get("ffmpeg") or "", "ffmpeg", "/opt/homebrew/bin/ffmpeg",
              "/usr/local/bin/ffmpeg"):
        if not c:
            continue
        if Path(c).exists():
            return str(c)
        w = shutil.which(c)
        if w:
            return w
    return ""


class Prober:
    def __init__(self, cfg):
        self.exe = find_ffprobe(cfg)
        if not self.exe:
            dr.die("ffprobe not found. Library mode reads each file's length and "
                   "resolution with ffprobe (part of ffmpeg).\n"
                   "macOS: brew install ffmpeg     Windows: winget install Gyan.FFmpeg\n"
                   "...or set 'ffprobe' in config.json to its full path.")
        self.db = libdb.open_db(cfg["work_dir"])
        self._lock = threading.Lock()
        self.cache = self.db.load_probes()
        self._dirty = {}
        if not self.cache:
            # one-time import of the JSON cache older versions kept
            legacy = Path(cfg["work_dir"]) / "library-probe-cache.json"
            try:
                self.cache = json.loads(legacy.read_text(encoding="utf-8"))
                self._dirty = dict(self.cache)
            except (OSError, ValueError):
                self.cache = {}

    def save(self):
        """Write new results to the database (call after a batch of probes)."""
        with self._lock:
            dirty, self._dirty = self._dirty, {}
        if dirty:
            try:
                self.db.save_probes(dirty)
            except Exception as e:      # a cache: never fail a scan over it
                print(f"Warning: couldn't save probe results ({e})", file=sys.stderr)

    def probe(self, path):
        """{duration, width, height, audio, chapters, title, ok} for one file."""
        st = os.stat(path)
        key = str(path)
        with self._lock:
            hit = self.cache.get(key)
        if hit and hit.get("size") == st.st_size and hit.get("mtime") == int(st.st_mtime):
            return hit["info"]
        info = {"duration": 0.0, "width": 0, "height": 0, "audio": 0,
                "chapters": 0, "title": "", "ok": False}
        try:
            out = subprocess.run(
                [self.exe, "-v", "error", "-print_format", "json",
                 "-show_entries",
                 "format=duration:format_tags=title:"
                 "stream=codec_type,codec_name,width,height:stream_disposition=attached_pic",
                 "-show_chapters", str(path)],
                capture_output=True, text=True, timeout=120).stdout
            d = json.loads(out or "{}")
            fmt = d.get("format") or {}
            streams = d.get("streams") or []
            video = [s for s in streams if s.get("codec_type") == "video"
                     and not (s.get("disposition") or {}).get("attached_pic")
                     and s.get("codec_name") not in ("mjpeg", "png")]
            if video:
                best = max(video, key=lambda s: (s.get("width") or 0) * (s.get("height") or 0))
                info["width"] = int(best.get("width") or 0)
                info["height"] = int(best.get("height") or 0)
            info["audio"] = sum(1 for s in streams if s.get("codec_type") == "audio")
            info["chapters"] = len(d.get("chapters") or [])
            info["duration"] = float(fmt.get("duration") or 0)
            info["title"] = ((fmt.get("tags") or {}).get("title") or "").strip()
            info["ok"] = info["duration"] > 0 and bool(video)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        if info["ok"]:   # never cache a failed read - the file may still be copying
            entry = {"size": st.st_size, "mtime": int(st.st_mtime), "info": info}
            with self._lock:
                self.cache[key] = self._dirty[key] = entry
        return info


class LibThumbs:
    """One preview frame per file, so unnamed titles ('title_t07.mkv') can be
    told apart at a glance. Plain ffmpeg - no libbluray needed, the file is
    already decrypted. Optional: without ffmpeg the page simply has no pictures."""

    def __init__(self, cfg):
        self.ffmpeg = find_ffmpeg(cfg)
        self.cache = Path(cfg["work_dir"]) / "thumbs"
        self.fraction = float(cfg.get("thumb_mid_fraction", 0.40))
        self.width = int(cfg.get("thumb_width", 240))
        self._sem = threading.Semaphore(3)     # a few at a time over the network

    def available(self):
        return bool(self.ffmpeg)

    def frame(self, f):
        """Path to a cached JPEG for LibFile `f`, or None."""
        if not self.ffmpeg or not f.ok:
            return None
        out = self.cache / f"lib_{f.id}_{int(f.mtime)}.jpg"
        if out.exists() and out.stat().st_size > 0:
            return out
        self.cache.mkdir(parents=True, exist_ok=True)
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
               "-ss", str(int(f.duration * self.fraction)), "-i", str(f.path),
               "-an", "-sn", "-frames:v", "1", "-vf", f"scale={self.width}:-2",
               "-f", "image2", "-vcodec", "mjpeg", "-q:v", "5", "-y", str(out)]
        with self._sem:
            if out.exists() and out.stat().st_size > 0:
                return out
            try:
                subprocess.run(cmd, capture_output=True, timeout=90)
            except (subprocess.TimeoutExpired, OSError):
                return None
        if out.exists() and out.stat().st_size > 0:
            return out
        try:
            out.unlink()
        except OSError:
            pass
        return None


# ---------------------------------------------------------------------------
# Data holders
# ---------------------------------------------------------------------------
class LibFile:
    """One video file found in a disc folder."""

    def __init__(self, root, folder, path):
        self.path = Path(path)
        self.folder = folder                       # top-level (disc) folder name
        self.rel = os.path.relpath(str(path), str(Path(root) / folder))
        parts = Path(self.rel).parts
        self.subdir = parts[0] if len(parts) > 1 else ""
        self.stem = self.path.stem
        self.ext = self.path.suffix.lower()
        self.id = _fid(root, path)
        st = os.stat(path)
        self.size = st.st_size
        self.mtime = st.st_mtime
        # filled by probe
        self.duration = 0.0
        self.width = self.height = 0
        self.audio = 0
        self.chapters = 0
        self.title_tag = ""
        self.ok = False
        # filled by classify
        self.role = "skip"
        self.category = "Other"
        self.name = self.stem          # output name (extras) without extension
        self.generic = False           # True when the rip has no meaningful name
        self.tno = None                # MakeMKV title number parsed from the name
        self.notes = []

    @property
    def quality(self):
        return quality_of(self.width, self.height)

    @property
    def pixels(self):
        return self.width * self.height

    @property
    def mbps(self):
        return (self.size * 8 / self.duration / 1e6) if self.duration > 0 else 0.0

    def to_json(self):
        return {
            "id": self.id, "folder": self.folder, "rel": self.rel,
            "stem": self.stem, "ext": self.ext,
            "size": self.size, "sizeH": human_size(self.size),
            "duration": round(self.duration, 1), "hms": dr.hms(self.duration),
            "res": f"{self.width}x{self.height}" if self.width else "",
            "quality": self.quality, "audio": self.audio, "chapters": self.chapters,
            "role": self.role, "category": self.category, "name": self.name,
            "generic": self.generic, "notes": self.notes,
        }


class Group:
    """One movie: one or more disc folders that belong together."""

    def __init__(self, key):
        self.key = key
        self.id = hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:10]
        self.query = ""
        self.year = None
        self.folders = []          # [{"name", "disc", "bonus"}]
        self.files = []
        self.candidates = []       # trimmed TMDB results
        self.details = None        # chosen TMDB movie details
        self.confidence = "none"   # high | medium | low | none
        self.notes = []
        self.busy = False
        self.looks_tv = False

    # --- naming (mirrors diskrip.MovieProposal.folder_and_file) -------------
    @property
    def title(self):
        return (self.details or {}).get("title", "")

    @property
    def movie_year(self):
        return dr.year_of((self.details or {}).get("release_date", ""))

    @property
    def imdb(self):
        return dr.imdb_from(self.details) if self.details else ""

    @property
    def runtime(self):
        return (self.details or {}).get("runtime") or 0

    def folder_name(self):
        if not self.details:
            return ""
        base = dr.sanitize(self.title)
        y = self.movie_year
        idpart = f" [imdb-{self.imdb}]" if self.imdb else ""
        return f"{base} ({y}){idpart}" if y else f"{base}{idpart}"

    def main_file(self):
        mains = [f for f in self.files if f.role == "main"]
        return max(mains, key=lambda f: (f.pixels, f.size)) if mains else None

    def to_json(self):
        main = self.main_file()
        d = self.details or {}
        return {
            "id": self.id, "query": self.query, "year": self.year,
            "folders": [f["name"] for f in self.folders],
            "files": [f.to_json() for f in sorted(self.files, key=_file_order)],
            "candidates": self.candidates,
            "match": ({
                "id": d.get("id"), "title": self.title, "year": self.movie_year,
                "imdb": self.imdb, "runtime": self.runtime,
                "overview": d.get("overview") or "",
                "poster": d.get("poster_path") or "",
                "folder": self.folder_name(),
            } if self.details else None),
            "confidence": self.confidence,
            "mainMinutes": round(main.duration / 60, 1) if main else None,
            "notes": self.notes, "busy": self.busy, "looksTv": self.looks_tv,
        }


def _file_order(f):
    return ({"main": 0, "extra": 1, "skip": 2}.get(f.role, 3), f.folder, f.rel.lower())


# ---------------------------------------------------------------------------
# Folder name -> searchable title (+ year / disc hints)
# ---------------------------------------------------------------------------
_YEAR_RE = re.compile(r"[\(\[]\s*((?:19|20)\d{2})\s*[\)\]]")
_DISC_RE = re.compile(
    r"[\s_\-–(\[]*\bdis[ck]\s*[#_]?\s*(\d{1,2}|one|two|three|four)\b[\)\]]*", re.I)
_BONUS_RE = re.compile(
    r"[\s_\-–(\[]*\b(?:bonus(?:\s+(?:dis[ck]|features?|materials?))?"
    r"|special\s+features?|extras?\s+dis[ck]|supplements?)\b[\)\]]*", re.I)
_MEDIA_RE = re.compile(
    r"[\s_\-–(\[]*\b(?:blu[\s\-]?ray|bluray|bd|dvd|uhd|4k|ultra\s*hd)\b[\)\]]*", re.I)
_REGION_RE = re.compile(r"\s(?:US|USA|UK|EU|EUR|CA|CAN|AU|AUS|NA|WS|FS)$")
_WORDNUM = {"one": 1, "two": 2, "three": 3, "four": 4}


def clean_folder_name(name):
    """'MARVEL'S DOCTOR STRANGE - BLU-RAY' -> ("Marvel's Doctor Strange", None, {...}).

    Returns (query, year, hints) where hints = {"disc": n|None, "bonus": bool}."""
    hints = {"disc": None, "bonus": False}
    text = name
    year = None
    m = _YEAR_RE.search(text)
    if m:
        year = int(m.group(1))
        text = _YEAR_RE.sub(" ", text)
    # "Title AKA Other Title AKA Third Title" - keep the first
    text = re.split(r"\s+AKA\s+", text)[0]
    # a raw disc label (IROBOT_US, THE_MATRIX): underscores are spaces, and a
    # trailing region code is not part of the title
    raw_label = bool(re.fullmatch(r"[A-Z0-9_\-'&!. ]+", text)) and ("_" in text or " " not in text)
    text = text.replace("_", " ")
    if raw_label:
        text = _REGION_RE.sub("", text)
    md = _DISC_RE.search(text)
    if md:
        tok = md.group(1).lower()
        hints["disc"] = int(tok) if tok.isdigit() else _WORDNUM.get(tok)
        text = _DISC_RE.sub(" ", text)
    if _BONUS_RE.search(text):
        hints["bonus"] = True
        text = _BONUS_RE.sub(" ", text)
    text = _MEDIA_RE.sub(" ", text)
    text = re.sub(r"\s*[-–]\s+|\s+[-–]\s*", " ", text)   # "John Wick- Chapter 2"
    text = re.sub(r"\s+", " ", text).strip(" -:.,")
    if text.isupper():
        text = text.title().replace("'S ", "'s ")
    return text, year, hints


# ---------------------------------------------------------------------------
# Scan: folders -> files -> groups
# ---------------------------------------------------------------------------
def _is_video(p):
    return p.suffix.lower() in VIDEO_EXT and not p.name.startswith("._")


def _hidden(name):
    return name.startswith(".") or name.startswith("_") or name.startswith("@") \
        or name in ("#recycle", "$RECYCLE.BIN", "System Volume Information")


def list_disc_folders(root, ignore=()):
    """Return (disc_folders, skipped). A disc folder directly contains video
    files, or only extras-category sub-folders that do. Anything else (a folder
    of folders such as 'Output/Movies/...') is reported, not descended into."""
    ignore_l = {i.lower() for i in ignore}
    discs, skipped = [], []
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name.lower())
    except OSError as e:
        dr.die(f"Can't read {root}: {e}")
    for e in entries:
        if _hidden(e.name):
            continue
        if not e.is_dir():
            continue
        if e.name.lower() in ignore_l:
            skipped.append({"name": e.name, "reason": "in library_ignore"})
            continue
        direct, subdirs = [], []
        try:
            for c in os.scandir(e.path):
                if c.is_dir() and not _hidden(c.name):
                    subdirs.append(c.name)
                elif c.is_file() and _is_video(Path(c.name)):
                    direct.append(c.name)
        except OSError as err:
            skipped.append({"name": e.name, "reason": f"unreadable ({err.strerror})"})
            continue
        foreign = [s for s in subdirs if s.lower() not in CATEGORY_ALIASES]
        nested_video = False
        for s in foreign:
            for _d, _dirs, fs in os.walk(os.path.join(e.path, s)):
                if any(_is_video(Path(f)) for f in fs):
                    nested_video = True
                    break
            if nested_video:
                break
        if not direct and nested_video:
            skipped.append({"name": e.name,
                            "reason": "holds other folders, not a ripped disc "
                                      "(scan it on its own to organize what's inside)"})
            continue
        discs.append(e.name)
    return discs, skipped


def collect_files(root, folder):
    out = []
    base = Path(root) / folder
    for d, dirs, fs in os.walk(base):
        dirs[:] = sorted(x for x in dirs if not _hidden(x))
        for f in sorted(fs):
            p = Path(d) / f
            if _is_video(p):
                try:
                    out.append(LibFile(root, folder, p))
                except OSError:
                    pass
    return out


def build_groups(root, discs, prober, progress=None):
    """Probe every file and merge disc folders that are the same movie."""
    per_folder = {name: collect_files(root, name) for name in discs}
    all_files = [f for fs in per_folder.values() for f in fs]
    total = len(all_files)
    done = [0]
    lock = threading.Lock()

    def work(f):
        info = prober.probe(f.path)
        f.duration, f.width, f.height = info["duration"], info["width"], info["height"]
        f.audio, f.chapters = info["audio"], info["chapters"]
        f.title_tag, f.ok = info["title"], info["ok"]
        with lock:
            done[0] += 1
            if progress:
                progress("Reading files", done[0], total)

    with ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(work, all_files))
    prober.save()

    groups, empty = {}, []
    now = time.time()
    for name in discs:
        files = per_folder[name]
        if not files:
            empty.append({"name": name, "reason": "no video files"})
            continue
        query, year, hints = clean_folder_name(name)
        key = _norm(query) or _norm(name) or name
        g = groups.get(key)
        if g is None:
            g = groups[key] = Group(key)
            g.query = query or name
        g.year = g.year or year
        g.folders.append({"name": name, "disc": hints["disc"], "bonus": hints["bonus"]})
        g.files.extend(files)
        if any(now - f.mtime < 180 for f in files):
            g.busy = True
    return sorted(groups.values(), key=lambda g: g.query.lower()), empty


# ---------------------------------------------------------------------------
# Classify: which file is the feature, which are extras, which are junk
# ---------------------------------------------------------------------------
_JUNK_NAME_RE = re.compile(
    r"(?:^|[\W_])(?:TMPL|WRN|FBI)(?:[\W_]|$)|main[ _]?menu|warning|(?:^|[\W_])logo(?:[\W_]|$)",
    re.I)
_TNO_RE = re.compile(r"^(.*?)[ _\-]*t(\d{2,3})$", re.I)
_CODE_TOKENS = {"sf", "seg", "fpl", "extra", "extras", "bonus"}


def _split_camel(tok):
    tok = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", tok)          # PussInBoots
    tok = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", tok)     # IKnow -> I Know
    return re.sub(r"(?<=[a-z])(?=\d)", " ", tok)            # Shrek2 -> Shrek 2


def pretty_name(f, group):
    """Derive a human name from a MakeMKV file name.

    Returns (name, generic, title_no). 'F1-SF_02_The_Anatomy_Of_A_Crash_t11'
    -> ('The Anatomy Of A Crash', False, 11); 'Coco_t04' -> ('', True, 4)."""
    stem = f.stem
    tno = None
    m = _TNO_RE.match(stem)
    if m:
        stem, tno = m.group(1), int(m.group(2))
    # drop the disc label MakeMKV prefixes every file with
    prefixes = {f.folder, f.title_tag, group.query} | {x["name"] for x in group.folders}
    for p in sorted((p for p in prefixes if p), key=len, reverse=True):
        if stem.lower() == p.lower():
            stem = ""
            break
        if stem.lower().startswith(p.lower()) and stem[len(p):len(p) + 1] in "-_ ":
            stem = stem[len(p):].lstrip("-_ ")
            break
    if tno is not None and _norm(stem) in ("", "title"):
        return "", True, tno
    if tno is None:
        return f.stem, False, None        # already a hand-given name: keep verbatim
    toks = [t for t in re.split(r"[_\s]+", stem) if t]
    while toks and toks[0].lower() in _CODE_TOKENS:
        toks.pop(0)
    while toks and toks[0].isdigit():
        toks.pop(0)
    if toks and toks[0].upper() == "MV":
        toks.pop(0)
        while toks and toks[0].isdigit():
            toks.pop(0)
        toks = ["Music Video -"] + toks
    name = " ".join(_split_camel(t) for t in toks).strip()
    if not name:
        return "", True, tno
    return name, False, tno


def guess_category(text, generic):
    low = text.lower()
    if re.search(r"deleted|alternate (?:ending|opening|scene)|extended scene", low):
        return "Deleted Scenes"
    if re.search(r"trailer|teaser|tv spot|promo\b", low):
        return "Trailers"
    if re.search(r"interview|q ?& ?a\b|conversation with", low):
        return "Interviews"
    if re.search(r"behind the scenes|making of|the making|on location|on set\b", low):
        return "Behind The Scenes"
    if re.search(r"music video|gag reel|blooper|outtake", low):
        return "Other"
    if re.search(r"\bshort film\b|\bshort\b", low):
        return "Shorts"
    return "Other" if generic else "Featurettes"


def classify_group(g, cfg):
    """Assign role / category / name / notes to every file in the group.

    Uses the matched movie's TMDB runtime when known to choose between several
    feature-length files."""
    movie_min = int(cfg.get("movie_min_minutes", 70)) * 60
    extras_min = int(cfg.get("extras_min_seconds", 60))
    junk_mbps = float(cfg.get("junk_max_mbps", 3.0))   # 0 disables the bitrate rule
    runtime = g.runtime * 60 if g.runtime else 0

    for f in g.files:
        f.role, f.category, f.notes = "extra", "Other", []
        f.name, f.generic, f.tno = pretty_name(f, g)

    readable = [f for f in g.files if f.ok]
    longs = [f for f in readable if f.duration >= movie_min and f.audio > 0]
    # a feature shorter than the usual floor (80-minute documentaries are fine,
    # but a 62-minute one is not): trust TMDB's runtime if a file matches it
    if not longs and runtime:
        longs = [f for f in readable if f.audio > 0 and abs(f.duration - runtime) <= 180]

    primary = None
    if longs:
        def rank(f):
            near = 1 if (runtime and abs(f.duration - runtime) <= 180) else 0
            return (near, f.pixels, f.duration, f.size)
        primary = max(longs, key=rank)
        primary.role = "main"
        for f in longs:
            if f is primary:
                continue
            same_len = abs(f.duration - primary.duration) <= max(90, primary.duration * 0.015)
            if same_len and f.quality != primary.quality:
                f.role = "main"
                f.notes.append(f"second version of the feature ({f.quality})")
            elif same_len:
                f.role = "skip"
                f.notes.append("same length and resolution as the feature "
                               "(duplicate playlist)")
            else:
                f.notes.append(f"feature-length ({int(f.duration // 60)} min) - "
                               "alternate cut or documentary?")

    # named files first, so of two identical files the unnamed one is dropped
    ordered = sorted(g.files, key=lambda f: (f.generic, f.folder, f.rel.lower()))
    seen = []
    for f in ordered:
        if f.role == "main":
            seen.append(f)
            continue
        if f.role == "skip":
            continue
        curated = CATEGORY_ALIASES.get(f.subdir.lower()) if f.subdir else None
        if not f.ok:
            f.role = "skip"
            f.notes.append("couldn't be read (still copying, or damaged?)")
            continue
        if f.audio == 0:
            f.role = "skip"
            f.notes.append("no audio track (menu / warning loop)")
        elif _JUNK_NAME_RE.search(f.stem) and not curated:
            f.role = "skip"
            f.notes.append("disc menu / warning screen")
        elif f.duration < extras_min:
            f.role = "skip"
            f.notes.append(f"shorter than {extras_min}s")
        else:
            twin = next((o for o in seen
                         if abs(o.duration - f.duration) <= 1.0
                         and abs(o.size - f.size) <= max(o.size, f.size) * 0.005), None)
            if twin is not None:
                f.role = "skip"
                f.notes.append(f"same length and size as '{twin.path.name}' (duplicate)")
            elif f.generic and primary and primary.height >= 2000 \
                    and f.height >= 2000 and f.duration < 600:
                f.role = "skip"
                f.notes.append("short 4K clip on a 4K disc - usually a menu background")
            elif f.height >= 1000 and f.mbps < junk_mbps:
                f.role = "skip"
                f.notes.append("very low bitrate (static screen)")
            elif f.generic and f.audio >= 5 and f.duration < 1800:
                f.role = "skip"
                f.notes.append(f"{f.audio} audio tracks - looks like a piece of the "
                               "feature, not an extra")
        seen.append(f)
        if curated:
            f.category = curated
        else:
            f.category = guess_category(f.stem + " " + f.name, f.generic)
        if f.generic:
            f.name = f"Extra t{f.tno:02d}" if f.tno is not None else f.stem
        f.name = dr.sanitize(f.name) or f.stem

    # Does this look like a TV disc rather than a movie? (several episode-length
    # files, nothing feature-length) - library mode does not handle those.
    ep_min = int(cfg.get("min_length_minutes", 15)) * 60
    eps = [f for f in readable if ep_min <= f.duration < movie_min and f.audio > 0
           and not f.subdir]
    g.looks_tv = primary is None and len(eps) >= 3
    g.notes = []
    if g.busy:
        g.notes.append("A file here changed in the last few minutes - is a rip "
                       "still running? Left unticked.")
    if g.looks_tv:
        g.notes.append("Looks like a TV disc (several episode-length files, no "
                       "feature). Library mode only files movies - left unticked.")
    elif primary is None:
        g.notes.append("No feature-length file here - extras only. They will be "
                       "filed into the movie's folder.")
    if len(g.folders) > 1:
        g.notes.append("Merged " + str(len(g.folders)) + " disc folders: "
                       + ", ".join(f["name"] for f in g.folders))


# ---------------------------------------------------------------------------
# Identify on TMDB (runtime of the main feature breaks ties between namesakes)
# ---------------------------------------------------------------------------
class Identifier:
    def __init__(self, tmdb):
        self.tmdb = tmdb
        self._details = {}
        self._lock = threading.Lock()

    def details(self, movie_id):
        with self._lock:
            d = self._details.get(movie_id)
        if d is None:
            d = self.tmdb.movie_details(movie_id)
            with self._lock:
                self._details[movie_id] = d
        return d

    @staticmethod
    def _trim(d):
        return {"id": d.get("id"), "name": d.get("title", ""),
                "year": dr.year_of(d.get("release_date", "")),
                "runtime": d.get("runtime") or 0,
                "overview": d.get("overview") or "",
                "poster": d.get("poster_path") or ""}

    def search(self, query, year=None, limit=6):
        """TMDB search + full details (runtime, imdb id) for the top results."""
        results = self.tmdb.search_movie(query, year) or []
        if not results and year:
            results = self.tmdb.search_movie(query) or []
        out = []
        for r in results[:limit]:
            try:
                out.append(self.details(r["id"]))
            except Exception:
                continue
        return out

    def _variants(self, g):
        seen, out = set(), []

        def add(q):
            q = (q or "").strip()
            if q and q.lower() not in seen:
                seen.add(q.lower())
                out.append(q)

        add(g.query)
        m = re.match(r"^\w+'s\s+(.+)$", g.query, re.I)     # "Marvel's Doctor Strange"
        if m:
            add(m.group(1))
        for tag in {f.title_tag for f in g.files if f.title_tag}:
            add(clean_folder_name(tag)[0])
        return out

    def identify(self, g):
        """Fill g.candidates / g.details / g.confidence. Never raises."""
        main = g.main_file()
        main_min = main.duration / 60 if main else 0
        try:
            cands = []
            for q in self._variants(g):
                cands = self.search(q, g.year)
                if cands:
                    break
            if not cands and main_min and re.fullmatch(r"[A-Za-z0-9]{4,}", g.query):
                # a squashed disc label ("IROBOT"): try it split in two, and only
                # believe a hit whose runtime matches the feature we have
                for i in range(1, min(len(g.query), 9)):
                    trial = self.search(g.query[:i] + " " + g.query[i:], g.year)
                    trial = [c for c in trial if c.get("runtime")
                             and abs(c["runtime"] - main_min) <= 3]
                    if trial:
                        cands = trial
                        break
        except dr.DiskRipError as e:
            g.notes.append(f"TMDB lookup failed: {e}")
            cands = []
        except Exception as e:                      # network hiccup etc.
            g.notes.append(f"TMDB lookup failed: {type(e).__name__}: {e}")
            cands = []
        g.candidates = [self._trim(c) for c in cands]
        if not cands:
            g.details, g.confidence = None, "none"
            return

        qn = _norm(g.query)
        qn2 = _norm(re.sub(r"^\w+'s\s+", "", g.query))

        def facts(i, c):
            names = {_norm(c.get("title")), _norm(c.get("original_title"))}
            exact = bool(names & {qn, qn2})
            cy = dr.year_of(c.get("release_date", ""))
            year_ok = bool(g.year and cy and int(cy) == g.year)
            rt = c.get("runtime") or 0
            delta = abs(rt - main_min) if (rt and main_min) else None
            return exact, year_ok, delta

        def score(item):
            i, c = item
            exact, year_ok, delta = facts(i, c)
            s = -0.4 * i
            s += 3 if exact else 0
            if g.year:
                cy = dr.year_of(c.get("release_date", ""))
                s += 3 if year_ok else (1 if cy and abs(int(cy) - g.year) <= 1 else -2)
            if delta is not None:
                s += 4 if delta <= 2 else 2 if delta <= 5 else -3 if delta > 15 else 0
            return s

        i, best = max(enumerate(cands), key=score)
        exact, year_ok, delta = facts(i, best)
        if delta is not None and delta <= 3 and (exact or year_ok or i == 0):
            conf = "high"
        elif exact and year_ok:
            conf = "high"
        elif exact and (i == 0) and (delta is None or delta <= 8):
            conf = "medium"
        else:
            conf = "low"
        g.details, g.confidence = best, conf

    def choose(self, g, movie_id):
        g.details = self.details(int(movie_id))
        g.confidence = "manual"
        if not any(c["id"] == g.details.get("id") for c in g.candidates):
            g.candidates.insert(0, self._trim(g.details))


# ---------------------------------------------------------------------------
# The scan result
# ---------------------------------------------------------------------------
class LibraryScan:
    def __init__(self, root, groups, skipped):
        self.root = Path(root)
        self.groups = groups
        self.skipped = skipped
        self.by_id = {g.id: g for g in groups}
        self.files = {f.id: f for g in groups for f in g.files}

    def to_json(self, cfg):
        return {
            "root": str(self.root),
            "movieRoot": str(cfg.get("movie_root", "")),
            "categories": CATEGORIES,
            "groups": [g.to_json() for g in self.groups],
            "skipped": self.skipped,
        }


def scan_library(cfg, tmdb, root=None, progress=None):
    """Scan the rips folder and return a LibraryScan with proposed roles and
    TMDB matches. Reads only - nothing is moved."""
    root = resolve_root(root or cfg.get("rips_root") or "", cfg.get("work_dir"))
    prober = Prober(cfg)
    if progress:
        progress("Listing folders", 0, 0)
    ignore = list(cfg.get("library_ignore") or [])
    discs, skipped = list_disc_folders(root, ignore)
    # never treat the destination library as a source
    mr = str(cfg.get("movie_root") or "")
    if mr:
        keep = []
        for d in discs:
            try:
                same = Path(mr).resolve() == (root / d).resolve()
            except OSError:
                same = False
            if same:
                skipped.append({"name": d, "reason": "this is the movie library (movie_root)"})
            else:
                keep.append(d)
        discs = keep
    groups, empty = build_groups(root, discs, prober, progress)
    skipped += empty
    ident = Identifier(tmdb)
    for g in groups:
        classify_group(g, cfg)            # provisional: finds the feature
    done = [0]
    lock = threading.Lock()

    def work(g):
        ident.identify(g)
        notes = [n for n in g.notes if n.startswith("TMDB lookup failed")]
        classify_group(g, cfg)            # final: now knows the TMDB runtime
        g.notes += notes
        with lock:
            done[0] += 1
            if progress:
                progress("Identifying on TMDB", done[0], len(groups))

    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(work, groups))
    scan = LibraryScan(root, groups, skipped)
    scan.identifier = ident
    return scan


# ---------------------------------------------------------------------------
# Plan: where would every file go?
# ---------------------------------------------------------------------------
def default_include(g):
    return bool(g.details) and g.confidence in ("high", "medium", "manual") \
        and not g.busy and not g.looks_tv


def build_plan(scan, cfg, overrides=None):
    """Resolve every file to a target path.

    overrides (all optional, as sent by the web UI):
      {"groups": {gid: {"include": bool}},
       "files":  {fid: {"role": main|extra|skip, "category": str, "name": str}}}

    Returns a list of items: {file, group, src, role, category, name, target,
    status} where status is one of
      move      - will be moved to target
      exists    - target already exists (left untouched, never overwritten)
      inplace   - already at its target
      skip      - role is 'skip' (stays where it is)
      excluded  - its movie is unticked
      unmatched - its movie has no TMDB match yet"""
    overrides = overrides or {}
    gov_all = overrides.get("groups") or {}
    fov_all = overrides.get("files") or {}
    movie_root = paths.validate_root("movies", cfg.get("movie_root"),
                                     work_dir=cfg.get("work_dir"))
    taken = set()
    items = []
    for g in scan.groups:
        gov = gov_all.get(g.id) or {}
        include = bool(gov.get("include", default_include(g)))
        folder = g.folder_name()
        for f in sorted(g.files, key=lambda f: (-f.pixels, -f.size, f.rel.lower())):
            fov = fov_all.get(f.id) or {}
            role = fov.get("role") if fov.get("role") in ROLES else f.role
            category = fov.get("category") if fov.get("category") in CATEGORIES else f.category
            name = dr.sanitize(str(fov.get("name") or f.name)) or f.stem
            item = {"file": f.id, "group": g.id, "src": str(f.path), "role": role,
                    "category": category, "name": name, "target": None,
                    "size": f.size, "status": "skip"}
            items.append(item)
            if role == "skip":
                continue
            if not folder:
                item["status"] = "unmatched"
                continue
            if role == "main":
                base, n = f"{folder} - {f.quality}", 1
                target = movie_root / folder / (base + f.ext)
                while str(target).lower() in taken:
                    n += 1
                    target = movie_root / folder / f"{base} v{n}{f.ext}"
            else:
                n = 1
                target = movie_root / folder / category / (name + f.ext)
                while str(target).lower() in taken:
                    n += 1
                    target = movie_root / folder / category / f"{name} ({n}){f.ext}"
            item["target"] = str(target)
            if not include:               # shown as "would go here", never moved
                item["status"] = "excluded"
                continue
            taken.add(str(target).lower())
            try:
                same = target.exists() and os.path.samefile(target, f.path)
            except OSError:
                same = False
            if same:
                item["status"] = "inplace"
            elif target.exists():
                item["status"] = "exists"
            else:
                item["status"] = "move"
    return items


# ---------------------------------------------------------------------------
# Apply: move files, logging every move so it can be undone
# ---------------------------------------------------------------------------
def _copy_then_remove(src, dst, on_bytes=None):
    """Cross-volume move with progress: copy to '<dst>.part', verify the size,
    rename into place, then remove the source.

    Any failure (a full disk, a dropped share, Ctrl+C) removes the .part file
    and leaves the source untouched - a half-written copy must never be left
    behind to fill the disk."""
    part = dst.with_name(dst.name + ".part")
    st = os.stat(src)
    total = st.st_size
    done = 0
    try:
        with open(src, "rb") as fi, open(part, "wb") as fo:
            while True:
                buf = fi.read(8 * 1024 * 1024)
                if not buf:
                    break
                fo.write(buf)
                done += len(buf)
                if on_bytes:
                    on_bytes(done, total)
            fo.flush()
            os.fsync(fo.fileno())
        if os.path.getsize(part) != total:
            raise OSError(errno.EIO, "copy was incomplete - source left untouched")
        # times only: copying the mode (shutil.copystat) would chmod the file,
        # which on an NFSv4-ACL dataset can strip the ACL it just inherited
        os.utime(part, ns=(st.st_atime_ns, st.st_mtime_ns))
        os.rename(part, dst)
    except BaseException:
        try:
            os.unlink(part)
        except OSError:
            pass
        raise
    os.remove(src)


def move_file(src, dst, on_bytes=None):
    """Move src to dst without ever overwriting. Same volume = instant rename;
    different volume = copy, verify, then delete the source."""
    src, dst = Path(src), Path(dst)
    paths.check_destination(dst.parent)
    if dst.exists():
        raise FileExistsError(f"already exists: {dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(src, dst)
        return "renamed"
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
    _copy_then_remove(src, dst, on_bytes)
    return "copied"


# ---------------------------------------------------------------------------
# Free space: check the whole job fits before the first file is touched
# ---------------------------------------------------------------------------
SPACE_HEADROOM = 1 << 30        # always leave at least 1 GiB free


def _existing(path):
    p = Path(os.path.abspath(str(path)))
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def needs_copy(src, dst_dir):
    """True when moving src into dst_dir is a real copy, not a rename: the two
    are on different filesystems, or (in a container) different mounts."""
    a, b = _existing(src), _existing(dst_dir)
    if os.stat(a).st_dev != os.stat(b).st_dev:
        return True
    ma, mb = paths.mount_point_of(a), paths.mount_point_of(b)
    return ma is not None and mb is not None and ma != mb


def free_bytes(path):
    st = os.statvfs(str(_existing(path)))
    return st.f_bavail * st.f_frsize


def check_space(moves):
    """moves: iterable of (src, dst, size). Raise DiskRipError, before anything
    is moved, if a destination filesystem hasn't room for every copy it will
    receive plus headroom (1 GiB or 2%, whichever is more). Renames within one
    filesystem need no space and are not counted.

    Note: on ZFS a snapshot keeps the blocks of files moved away, so moving
    files off a snapshotted dataset frees nothing - only real free space
    counts."""
    need = {}
    for src, dst, size in moves:
        dst_dir = Path(dst).parent
        try:
            if not needs_copy(src, dst_dir):
                continue
            dev = os.stat(_existing(dst_dir)).st_dev
        except OSError:
            continue
        entry = need.setdefault(dev, [0, dst_dir])
        entry[0] += size
    problems = []
    for total, where in need.values():
        margin = max(SPACE_HEADROOM, total // 50)
        free = free_bytes(where)
        if total + margin > free:
            problems.append(f"{_existing(where)}: needs {human_size(total)} plus "
                            f"{human_size(margin)} headroom, but only "
                            f"{human_size(free)} is free")
    if problems:
        dr.die("Not enough free space - nothing was moved.\n  " + "\n  ".join(problems))


def _sidecars(src):
    """Non-video files that belong to a video ('Movie.srt', 'Movie.en.srt')."""
    src = Path(src)
    out = []
    try:
        for p in src.parent.iterdir():
            if p.is_file() and p != src and p.name.startswith(src.stem + ".") \
                    and p.suffix.lower() not in VIDEO_EXT:
                out.append(p)
    except OSError:
        pass
    return out


def _write_log(path, log):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(log, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def prune_empty(folder):
    """Remove `folder` if only empty sub-folders and Finder droppings
    (.DS_Store, ._*) are left in it. Returns True if it was removed."""
    folder = Path(folder)
    junk = lambda n: n == ".DS_Store" or n.startswith("._") or n == "Thumbs.db"
    for d, dirs, fs in os.walk(folder):
        if any(not junk(f) for f in fs):
            return False
    for d, dirs, fs in os.walk(folder, topdown=False):
        for f in fs:
            os.remove(os.path.join(d, f))
        os.rmdir(d)
    return True


def _planned_moves(todo):
    out = []
    for i in todo:
        try:
            out.append((i["src"], i["target"], os.path.getsize(i["src"])))
        except OSError:
            continue
        for sc in _sidecars(i["src"]):
            try:
                out.append((str(sc), i["target"], sc.stat().st_size))
            except OSError:
                pass
    return out


def apply_plan(items, cfg, scan=None, prune=False, on_item=None):
    """Execute every item whose status is 'move'. Returns the log dict (also
    written to work_dir/library-logs/). on_item(item) is called on each status
    change so a UI can show progress; items gain 'state', 'pct', 'error'."""
    todo = [i for i in items if i["status"] == "move"]
    # check again right before writing: a share can drop between plan and apply
    paths.validate_root("movies", cfg.get("movie_root"), work_dir=cfg.get("work_dir"))
    if scan is not None:              # sources are removed after each move
        paths.validate_root("rips", str(scan.root), work_dir=cfg.get("work_dir"))
    check_space(_planned_moves(todo))
    log_dir = Path(cfg["work_dir"]) / "library-logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / time.strftime("organize-%Y%m%d-%H%M%S.json")
    log = {"started": time.strftime("%Y-%m-%d %H:%M:%S"),
           "root": str(scan.root) if scan else "", "movie_root": str(cfg["movie_root"]),
           "moves": [], "pruned": []}
    _write_log(log_path, log)
    log["path"] = str(log_path)

    def tell(i):
        if on_item:
            on_item(i)

    for i in todo:
        i["state"], i["pct"] = "moving", 0
        tell(i)

        def on_bytes(done, total, _i=i):
            pct = int(done * 100 / total) if total else 100
            if pct != _i["pct"]:
                _i["pct"] = pct
                tell(_i)

        try:
            sidecars = _sidecars(i["src"])
            how = move_file(i["src"], i["target"], on_bytes)
            log["moves"].append({"src": i["src"], "dst": i["target"], "how": how})
            tstem = Path(i["target"]).with_suffix("")
            for sc in sidecars:
                sdst = Path(str(tstem) + sc.name[len(Path(i["src"]).stem):])
                try:
                    move_file(sc, sdst)
                    log["moves"].append({"src": str(sc), "dst": str(sdst), "how": "sidecar"})
                except OSError:
                    pass
            i["state"], i["pct"] = "done", 100
        except FileExistsError as e:
            i["state"], i["error"] = "skipped", str(e)
        except OSError as e:
            i["state"], i["error"] = "failed", f"{e.strerror or e}"
        _write_log(log_path, {k: v for k, v in log.items() if k != "path"})
        tell(i)

    if prune and scan is not None:
        for folder in sorted({scan.files[i["file"]].folder for i in todo
                              if i.get("state") == "done" and i["file"] in scan.files}):
            try:
                if prune_empty(scan.root / folder):
                    log["pruned"].append(folder)
            except OSError:
                pass
        _write_log(log_path, {k: v for k, v in log.items() if k != "path"})
    return log


def undo(log_path):
    """Reverse a previous run from its log. Returns (restored, problems)."""
    log = json.loads(Path(log_path).read_text(encoding="utf-8"))
    restored, problems = 0, []
    library_root = Path(log.get("movie_root") or "/").expanduser()
    # everything that will actually be copied back must fit first
    back = []
    for mv in log.get("moves", []):
        src, dst = Path(mv["src"]), Path(mv["dst"])
        if dst.is_file() and not src.exists():
            back.append((dst, src, dst.stat().st_size))
    check_space(back)
    for mv in reversed(log.get("moves", [])):
        src, dst = Path(mv["src"]), Path(mv["dst"])
        if not dst.exists():
            problems.append(f"missing, can't restore: {dst}")
            continue
        if src.exists():
            problems.append(f"original path is occupied, left alone: {src}")
            continue
        try:
            move_file(dst, src)
            restored += 1
        except OSError as e:
            problems.append(f"{dst}: {e}")
            continue
        # tidy the now-empty library folders we created (rmdir only removes
        # empty directories, so nothing can be lost)
        d = dst.parent
        for _ in range(2):
            if d == library_root:        # never remove the library folder itself
                break
            try:
                d.rmdir()
            except OSError:
                break
            d = d.parent
    return restored, problems


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def print_plan(scan, items, cfg, show_skips=True):
    by_group = {}
    for i in items:
        by_group.setdefault(i["group"], []).append(i)
    movie_root = Path(cfg["movie_root"])
    counts = {"move": 0, "exists": 0, "inplace": 0}
    for g in scan.groups:
        gi = by_group.get(g.id, [])
        main = g.main_file()
        print("\n" + dr.bold("=" * 78))
        if g.details:
            badge = {"high": dr.green("match: high"), "manual": dr.green("match: yours"),
                     "medium": dr.yellow("match: medium"),
                     "low": dr.red("match: LOW - check")}.get(g.confidence, g.confidence)
            rt = ""
            if main and g.runtime:
                d = abs(main.duration / 60 - g.runtime)
                flag = dr.green("✓") if d <= 3 else dr.red(f"⚠ Δ{int(d)}m")
                rt = f"   feature {int(main.duration // 60)}m vs TMDB {g.runtime}m {flag}"
            print(f"  {dr.bold(g.folder_name())}   {badge}{rt}")
        else:
            print(f"  {dr.bold(g.query)}   " + dr.red("no TMDB match - use --match or the web UI"))
        print(dr.dim("  from: " + ", ".join(f["name"] for f in g.folders)))
        for n in g.notes:
            print(dr.yellow(f"  ! {n}"))
        excluded = any(i["status"] == "excluded" for i in gi)
        if excluded:
            why = ("a rip may still be running" if g.busy else
                   "looks like TV" if g.looks_tv else
                   "low-confidence match: confirm it with --match "
                   f"\"{g.folders[0]['name']}=tmdb:{g.details.get('id')}\", "
                   "or pass --include-low" if g.confidence == "low" else "unticked")
            print(dr.yellow(f"  (not moved - {why})"))
        order = {"main": 0, "extra": 1, "skip": 2}
        for i in sorted(gi, key=lambda i: (order[i["role"]], i["src"].lower())):
            f = scan.files[i["file"]]
            src = f"{f.folder}/{f.rel}"
            meta = f"{dr.hms(f.duration):>8} {f.quality:>5} {human_size(f.size):>8}"
            if i["role"] == "skip":
                if show_skips:
                    why = "; ".join(f.notes) or "skipped"
                    print(dr.dim(f"    skip   {src[:58]:<58} {meta}  ({why})"))
                continue
            tag = dr.green("MAIN ") if i["role"] == "main" else "extra"
            if i["target"]:
                rel = os.path.relpath(i["target"], movie_root)
                tgt = rel.split(os.sep, 1)[1] if os.sep in rel else rel
            else:
                tgt = "?"
            st = {"exists": dr.red("  ● already in library (left alone)"),
                  "inplace": dr.dim("  (already in place)")}.get(i["status"], "")
            if i["status"] in counts:
                counts[i["status"]] += 1
            print(f"    {tag}  {src[:58]:<58} {meta}")
            print(f"           {dr.dim('->')} {tgt}{st}")
            for n in f.notes:
                print(dr.yellow(f"           ! {n}"))
    if scan.skipped:
        print("\n" + dr.dim("Folders left alone:"))
        for s in scan.skipped:
            print(dr.dim(f"  {s['name']}  - {s['reason']}"))
    print(f"\n{dr.bold(str(counts['move']))} file(s) to move into {movie_root}"
          + (f", {counts['exists']} already there" if counts["exists"] else ""))
    return counts


def main():
    ap = argparse.ArgumentParser(
        description="Organize an existing folder of ripped discs into the movie library.")
    ap.add_argument("--config", default=str(Path(__file__).parent.parent / "config.json"))
    ap.add_argument("--root", default=None,
                    help="Folder of ripped discs (path, or smb://host/share on macOS). "
                         "Default: 'rips_root' in config.json.")
    ap.add_argument("--only", action="append", default=[],
                    help="Only folders whose name contains this text (repeatable).")
    ap.add_argument("--match", action="append", default=[], metavar="FOLDER=TITLE",
                    help="Fix an identification: 'IROBOT_US=I, Robot' or "
                         "'IROBOT_US=tmdb:2048' (repeatable).")
    ap.add_argument("--include-low", action="store_true",
                    help="Also move groups whose TMDB match is low-confidence.")
    ap.add_argument("--dry-run", action="store_true", help="Show the plan and exit.")
    ap.add_argument("--yes", action="store_true", help="Move without asking.")
    ap.add_argument("--prune-empty", action="store_true",
                    help="After moving, delete source folders that are left empty.")
    ap.add_argument("--hide-skips", action="store_true",
                    help="Don't list skipped files in the plan.")
    ap.add_argument("--undo", metavar="LOG", help="Reverse a previous run from its log file.")
    args = ap.parse_args()

    if args.undo:
        restored, problems = undo(args.undo)
        print(dr.green(f"Restored {restored} file(s)."))
        for p in problems:
            print(dr.yellow("  ! " + p))
        return

    cfg = dr.load_config(args.config)
    if not cfg.get("movie_root"):
        dr.die("Set 'movie_root' in config.json (where organized movies go).")
    tmdb = dr.TMDB(cfg["tmdb_api_key"], cfg.get("language", "en"))

    last = [""]
    tty = sys.stdout.isatty()

    def progress(phase, done, total):
        if not tty:                      # piped/logged: one line per phase
            if phase != last[0]:
                last[0] = phase
                print(f"  {phase}...", flush=True)
            return
        msg = f"{phase}... {done}/{total}" if total else f"{phase}..."
        if msg != last[0]:
            last[0] = msg
            print("\r  " + dr.dim(msg) + " " * 12, end="", flush=True)

    scan = scan_library(cfg, tmdb, args.root, progress)
    print()
    if args.only:
        wanted = [o.lower() for o in args.only]
        scan.groups = [g for g in scan.groups
                       if any(w in f["name"].lower() for f in g.folders for w in wanted)]
    for spec in args.match:
        if "=" not in spec:
            dr.die(f"--match needs FOLDER=TITLE, got: {spec}")
        folder, want = (s.strip() for s in spec.split("=", 1))
        hits = [g for g in scan.groups
                if any(folder.lower() == f["name"].lower() for f in g.folders)]
        if not hits:
            dr.die(f"--match: no scanned folder is named '{folder}'")
        for g in hits:
            if want.lower().startswith("tmdb:"):
                scan.identifier.choose(g, int(want[5:]))
            else:
                g.query = want
                g.year = None
                scan.identifier.identify(g)
                if g.details:
                    g.confidence = "manual" if g.confidence != "low" else "low"
            classify_group(g, cfg)

    overrides = {"groups": {}}
    if args.include_low:
        for g in scan.groups:
            if g.details and not g.busy and not g.looks_tv:
                overrides["groups"][g.id] = {"include": True}
    items = build_plan(scan, cfg, overrides)
    counts = print_plan(scan, items, cfg, show_skips=not args.hide_skips)
    if args.dry_run or not counts["move"]:
        if args.dry_run:
            print(dr.dim("[dry-run] nothing was moved."))
        return
    if not args.yes:
        ans = input(f"\nMove {counts['move']} file(s) now? [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("Nothing moved.")
            return

    def on_item(i):
        if i.get("state") == "moving":
            print(f"\r  moving {Path(i['src']).name[:50]:<50} {i.get('pct', 0):3d}%",
                  end="", flush=True)
        elif i.get("state") == "done":
            print(f"\r  {dr.green('✓')} {Path(i['target']).name[:70]:<70}")
        elif i.get("state") in ("failed", "skipped"):
            print(f"\r  {dr.red('✗')} {Path(i['src']).name}: {i.get('error')}")

    log = apply_plan(items, cfg, scan, prune=args.prune_empty, on_item=on_item)
    print(dr.green(f"\nDone. {len(log['moves'])} file(s) moved."))
    print(dr.dim(f"Undo with:  python3 src/library.py --undo \"{log['path']}\""))


if __name__ == "__main__":
    try:
        main()
    except (dr.DiskRipError, paths.RootError) as e:
        print(dr.red("\nERROR: ") + str(e), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
