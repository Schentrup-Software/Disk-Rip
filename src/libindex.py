#!/usr/bin/env python3
"""
libindex.py - What is already in the movie library?

Before files are planned into <movie_root>, every top-level library folder is
indexed so a scanned movie can be matched to a folder that already holds it,
even when that folder has another name ("I, Robot (2004)" vs. the standard
"I, Robot (2004) [imdb-tt0343818]").

A folder matches a scanned movie when:
  1. its name carries the same IMDb or TMDB id ([imdb-tt...], {imdb-tt...},
     [tmdbid-...], {tmdb-...}), or one Disk-Rip learned for it earlier; a
     folder whose id is for a *different* movie never matches by name, or
  2. its title, compared without case, punctuation or a leading article,
     equals the movie's TMDB title or original title, and its year (if it has
     one) is within a year of the release year.

The video files in a matched folder are probed (length, resolution) so the
planner can tell duplicates from new extras and new resolutions. Folder
listings, probe results and learned ids live in the database (libdb), so a
second audit only re-reads what changed.
"""

import json
import os
import re
from pathlib import Path

import diskrip as dr
import library as lib

_TAG_RE = re.compile(
    r"\s*[\[{]\s*(imdb(?:id)?|tmdb(?:id)?)\s*[-=:]?\s*(tt\d{5,}|\d+)\s*[\]}]", re.I)
_YEAR_RE = re.compile(r"\s*\(\s*((?:19|20)\d{2})\s*\)")
_ARTICLE_RE = re.compile(r"^(?:the|a|an) ")


def title_key(text):
    """'The Devil's Advocate' -> 'devil s advocate': for comparing titles."""
    t = lib._norm((text or "").replace("&", " and "))
    return _ARTICLE_RE.sub("", t)


def parse_folder(name):
    """'I, Robot (2004) [imdb-tt0343818]' ->
    {"title": "I, Robot", "year": 2004, "imdb": "tt0343818", "tmdb": None, ...}"""
    imdb = tmdb = None
    for m in _TAG_RE.finditer(name):
        kind, val = m.group(1).lower(), m.group(2).lower()
        if kind.startswith("imdb") and val.startswith("tt"):
            imdb = val
        elif kind.startswith("tmdb") and val.isdigit():
            tmdb = int(val)
    rest = _TAG_RE.sub("", name)
    year = None
    m = _YEAR_RE.search(rest)
    if m:
        year = int(m.group(1))
        rest = rest[:m.start()]
    title = rest.strip(" -._")
    return {"name": name, "title": title, "year": year, "imdb": imdb, "tmdb": tmdb,
            "key": title_key(title)}


def _walk(folder):
    """(sig, files): every video under `folder` and a fingerprint of its
    directory tree (each directory's mtime) to tell later whether it changed."""
    dirs, files = [], []
    for d, sub, fs in os.walk(folder):
        sub[:] = sorted(x for x in sub if not lib._hidden(x))
        try:
            dirs.append([os.path.relpath(d, folder), os.stat(d).st_mtime_ns])
        except OSError:
            continue
        for f in sorted(fs):
            p = Path(d) / f
            if not lib._is_video(p):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            files.append({"rel": os.path.relpath(p, folder), "size": st.st_size,
                          "mtime": int(st.st_mtime)})
    return json.dumps(dirs), files


def _sig_still_valid(folder, sig):
    if not sig:
        return False
    try:
        dirs = json.loads(sig)
        for rel, mtime_ns in dirs:
            if os.stat(os.path.join(folder, rel)).st_mtime_ns != mtime_ns:
                return False
    except (OSError, ValueError, TypeError):
        return False
    return True


class LibraryIndex:
    def __init__(self, root, prober, db=None):
        self.root = Path(root)
        self.prober = prober
        self.db = db if db is not None else prober.db
        self.folders = {}           # name -> parse_folder() + learned ids
        self._videos = {}           # name -> videos() result for this session

    def refresh(self):
        """List the library's top-level folders (cheap: names only)."""
        try:
            names = sorted(e.name for e in os.scandir(self.root)
                           if e.is_dir() and not lib._hidden(e.name))
        except OSError:
            names = []
        known = self.db.folders(self.root)
        present = set(names)
        self.db.forget_folders(self.root, [n for n in known if n not in present])
        self.db.touch_folders(self.root, names)
        self.folders = {}
        for n in names:
            info = parse_folder(n)
            k = known.get(n) or {}
            info["learned_tmdb"], info["learned_imdb"] = k.get("tmdb"), k.get("imdb")
            self.folders[n] = info
        self._videos = {}
        return self

    # --- matching ------------------------------------------------------------
    def matches(self, details):
        """Names of library folders that hold the TMDB movie `details`."""
        if not details:
            return []
        imdb = (dr.imdb_from(details) or "").lower() or None
        tmdb = details.get("id")
        y = dr.year_of(details.get("release_date", ""))
        year = int(y) if y.isdigit() else None
        keys = {title_key(details.get("title")), title_key(details.get("original_title"))}
        keys.discard("")
        out = []
        for name, f in self.folders.items():
            fimdb = f["imdb"] or f["learned_imdb"]
            ftmdb = f["tmdb"] or f["learned_tmdb"]
            if imdb and fimdb:
                if fimdb == imdb:
                    out.append(name)
                continue                    # another movie's id: never by name
            if tmdb and ftmdb:
                if ftmdb == tmdb:
                    out.append(name)
                continue
            if f["key"] in keys and (f["year"] is None or year is None
                                     or abs(f["year"] - year) <= 1):
                out.append(name)
        return out

    def learn(self, name, details):
        """Record that folder `name` holds `details` (after a confirmed move)."""
        if details:
            self.db.learn_ids(self.root, name, details.get("id"),
                              (dr.imdb_from(details) or "").lower() or None)

    def duplicates(self):
        """Sets of library folders that look like the same movie, judged by
        name alone: same id, or same title and year. [[name, ...], ...]"""
        by = {}
        for name, f in self.folders.items():
            imdb = f["imdb"] or f["learned_imdb"]
            tmdb = f["tmdb"] or f["learned_tmdb"]
            key = (("imdb", imdb) if imdb else ("tmdb", tmdb) if tmdb
                   else ("title", f["key"], f["year"]) if f["key"] and f["year"] else None)
            if key:
                by.setdefault(key, []).append(name)
        # a tagged folder and an untagged "Title (Year)" of the same movie
        tagged = {(f["key"], f["year"]): k for k, names in by.items() if k[0] != "title"
                  for f in (self.folders[n] for n in names)}
        for k in [k for k in by if k[0] == "title"]:
            other = tagged.get((k[1], k[2]))
            if other is not None and other in by:
                by[other].extend(by.pop(k))
        return [sorted(set(v)) for v in by.values() if len(set(v)) > 1]

    # --- contents --------------------------------------------------------------
    def videos(self, name):
        """Video files in library folder `name`, with length and resolution:
        [{"rel", "path", "size", "duration", "width", "height", "quality",
          "top"}] where top=True for files directly in the folder (features)."""
        if name in self._videos:
            return self._videos[name]
        folder = self.root / name
        sig, files = self.db.folder_files(self.root, name)
        if not _sig_still_valid(folder, sig):
            sig, files = _walk(folder)
            self.db.store_folder_files(self.root, name, sig, files)
        out = []
        for f in files:
            p = folder / f["rel"]
            try:
                info = self.prober.probe(p)
                size = p.stat().st_size
            except OSError:
                continue
            out.append({"rel": f["rel"], "path": str(p), "size": size,
                        "duration": info["duration"], "width": info["width"],
                        "height": info["height"],
                        "quality": lib.quality_of(info["width"], info["height"]),
                        "top": os.sep not in f["rel"] and "/" not in f["rel"]})
        self.prober.save()
        self._videos[name] = out
        return out

    def all_files(self, name):
        """Every file (any type) under library folder `name`, relative paths."""
        folder = self.root / name
        out = []
        for d, sub, fs in os.walk(folder):
            sub[:] = sorted(sub)
            for f in sorted(fs):
                out.append(os.path.relpath(os.path.join(d, f), folder))
        return out
