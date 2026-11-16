#!/usr/bin/env python3
"""
libdb.py - Disk-Rip's small database (work_dir/disk-rip.sqlite).

Reading a video's length and resolution means running ffprobe on it, and
auditing the movie library means doing that for every file in every folder
that might hold the same movie. This database remembers those answers so each
file is only read again when its size or modification time changes:

  probes       ffprobe results, keyed by file path (+ size and mtime)
  lib_folders  movie-library folders: a fingerprint of their directory tree
               (so an unchanged folder isn't walked again) and the TMDB/IMDb
               ids Disk-Rip has learned for them
  lib_files    the video files last seen in each library folder

Everything here is a cache - deleting the file only costs a slower next scan.
If the database can't be opened (read-only or full disk), Disk-Rip carries on
without it. Stdlib only (sqlite3).
"""

import json
import sqlite3
import sys
import threading
import time
from pathlib import Path

FILENAME = "disk-rip.sqlite"
SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS probes (
    path    TEXT PRIMARY KEY,
    size    INTEGER NOT NULL,
    mtime   INTEGER NOT NULL,
    info    TEXT NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS lib_folders (
    root    TEXT NOT NULL,
    name    TEXT NOT NULL,
    sig     TEXT,
    tmdb    INTEGER,
    imdb    TEXT,
    updated REAL NOT NULL,
    PRIMARY KEY (root, name)
);
CREATE TABLE IF NOT EXISTS lib_files (
    root   TEXT NOT NULL,
    folder TEXT NOT NULL,
    rel    TEXT NOT NULL,
    size   INTEGER NOT NULL,
    mtime  INTEGER NOT NULL,
    PRIMARY KEY (root, folder, rel)
);
"""


class DB:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
        with self._lock, self.conn:
            self.conn.executescript(_SCHEMA)
            self.conn.execute("INSERT OR IGNORE INTO meta VALUES ('schema', ?)",
                              (str(SCHEMA_VERSION),))

    # --- probes ------------------------------------------------------------
    def load_probes(self):
        """{path: {"size", "mtime", "info"}} - the shape Prober caches."""
        with self._lock:
            rows = self.conn.execute("SELECT path, size, mtime, info FROM probes").fetchall()
        out = {}
        for path, size, mtime, info in rows:
            try:
                out[path] = {"size": size, "mtime": mtime, "info": json.loads(info)}
            except ValueError:
                continue
        return out

    def save_probes(self, entries):
        now = time.time()
        rows = [(p, e["size"], e["mtime"], json.dumps(e["info"]), now)
                for p, e in entries.items()]
        with self._lock, self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO probes (path, size, mtime, info, updated) "
                "VALUES (?, ?, ?, ?, ?)", rows)

    # --- library folders -----------------------------------------------------
    def folders(self, root):
        """{name: {"sig", "tmdb", "imdb"}} for one library root."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT name, sig, tmdb, imdb FROM lib_folders WHERE root = ?",
                (str(root),)).fetchall()
        return {n: {"sig": s, "tmdb": t, "imdb": i} for n, s, t, i in rows}

    def touch_folders(self, root, names):
        """Make sure every listed folder has a row (keeping what's known)."""
        now = time.time()
        with self._lock, self.conn:
            self.conn.executemany(
                "INSERT OR IGNORE INTO lib_folders (root, name, updated) VALUES (?, ?, ?)",
                [(str(root), n, now) for n in names])

    def forget_folders(self, root, names):
        with self._lock, self.conn:
            for n in names:
                self.conn.execute("DELETE FROM lib_folders WHERE root = ? AND name = ?",
                                  (str(root), n))
                self.conn.execute("DELETE FROM lib_files WHERE root = ? AND folder = ?",
                                  (str(root), n))

    def learn_ids(self, root, name, tmdb=None, imdb=None):
        """Remember which movie a library folder holds (a confirmed match)."""
        now = time.time()
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO lib_folders (root, name, updated) VALUES (?, ?, ?)",
                (str(root), name, now))
            self.conn.execute(
                "UPDATE lib_folders SET tmdb = COALESCE(?, tmdb), imdb = COALESCE(?, imdb), "
                "updated = ? WHERE root = ? AND name = ?",
                (tmdb, imdb, now, str(root), name))

    def folder_files(self, root, name):
        """(sig, [{"rel", "size", "mtime"}]) last stored for a folder."""
        with self._lock:
            row = self.conn.execute(
                "SELECT sig FROM lib_folders WHERE root = ? AND name = ?",
                (str(root), name)).fetchone()
            files = self.conn.execute(
                "SELECT rel, size, mtime FROM lib_files WHERE root = ? AND folder = ? "
                "ORDER BY rel", (str(root), name)).fetchall()
        return (row[0] if row else None,
                [{"rel": r, "size": s, "mtime": m} for r, s, m in files])

    def store_folder_files(self, root, name, sig, files):
        now = time.time()
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO lib_folders (root, name, updated) VALUES (?, ?, ?)",
                (str(root), name, now))
            self.conn.execute(
                "UPDATE lib_folders SET sig = ?, updated = ? WHERE root = ? AND name = ?",
                (sig, now, str(root), name))
            self.conn.execute("DELETE FROM lib_files WHERE root = ? AND folder = ?",
                              (str(root), name))
            self.conn.executemany(
                "INSERT INTO lib_files (root, folder, rel, size, mtime) VALUES (?, ?, ?, ?, ?)",
                [(str(root), name, f["rel"], f["size"], f["mtime"]) for f in files])

    def rename_folder(self, root, old, new):
        """Keep learned ids when Disk-Rip renames or merges a library folder."""
        with self._lock:
            row = self.conn.execute(
                "SELECT tmdb, imdb FROM lib_folders WHERE root = ? AND name = ?",
                (str(root), old)).fetchone()
        self.forget_folders(root, [old])
        if row and (row[0] or row[1]):
            self.learn_ids(root, new, row[0], row[1])


class NullDB:
    """Stand-in when the database can't be opened: remembers nothing."""

    path = None

    def load_probes(self):
        return {}

    def folders(self, root):
        return {}

    def folder_files(self, root, name):
        return None, []

    def __getattr__(self, name):          # every write is a no-op
        return lambda *a, **kw: None


_OPEN = {}
_OPEN_LOCK = threading.Lock()


def open_db(work_dir):
    """The shared database in work_dir (one connection per file), or a NullDB
    if it can't be opened."""
    path = Path(work_dir) / FILENAME
    key = str(path)
    with _OPEN_LOCK:
        db = _OPEN.get(key)
        if db is None:
            try:
                db = DB(path)
            except (OSError, sqlite3.Error) as e:
                print(f"Warning: can't open {path} ({e}); scans won't be cached.",
                      file=sys.stderr)
                db = NullDB()
            _OPEN[key] = db
    return db
