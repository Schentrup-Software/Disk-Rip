#!/usr/bin/env python3
"""
thumbs.py - Extract per-title thumbnail frames straight from a Blu-ray disc.

Uses ffmpeg's `bluray:` protocol (libbluray) to read ONE frame from a specific
playlist without ripping the whole 5 GB title. AACS-protected discs are decrypted
on the fly by pointing libbluray at MakeMKV's libmmbd via the LIBAACS_PATH /
LIBBDPLUS_PATH environment variables (libbluray >= 0.5.0).

This module is intentionally kept OUT of diskrip.py so the core engine stays
stdlib-only and dependency-free. Everything here degrades gracefully: if ffmpeg
isn't configured, lacks the bluray protocol, or a frame can't be decoded, the
caller just gets None and shows no thumbnail.

Requirements (all optional, feature is off without them):
  - ffmpeg built with libbluray (e.g. the gyan.dev "full" Windows build)
  - MakeMKV installed & registered (libmmbd launches a hidden MakeMKV to decrypt)
"""

import os
import re
import shutil
import subprocess
import threading
from pathlib import Path


class Thumbnailer:
    def __init__(self, ffmpeg_path, work_dir, makemkvcon_path=None, opts=None):
        # Accept an absolute path, a bare "ffmpeg", or "" -> resolve via PATH so
        # the feature survives ffmpeg version updates without a config edit.
        self.ffmpeg = ""
        if ffmpeg_path and Path(ffmpeg_path).exists():
            self.ffmpeg = ffmpeg_path
        else:
            self.ffmpeg = shutil.which(ffmpeg_path or "ffmpeg") or ""
        self.cache = Path(work_dir) / "thumbs"
        opts = opts or {}
        self.mid_fraction = float(opts.get("thumb_mid_fraction", 0.40))
        self.tail_seconds = int(opts.get("thumb_tail_seconds", 120))
        self.width = int(opts.get("thumb_width", 240))
        self.timeout = int(opts.get("thumb_timeout", 60))
        self._libmmbd = self._find_libmmbd(makemkvcon_path)
        self._env = self._build_env()
        self._aacs_ready = self._ensure_aacs_dlls()
        self._available = None  # cached feature-detect
        self._lock = threading.Lock()  # serialize drive access (one ffmpeg at a time)

    # --- capability ------------------------------------------------------
    def available(self):
        """True if ffmpeg exists and advertises the bluray protocol."""
        if self._available is not None:
            return self._available
        self._available = False
        if not self.ffmpeg or not Path(self.ffmpeg).exists():
            return False
        try:
            out = subprocess.run(
                [self.ffmpeg, "-hide_banner", "-protocols"],
                capture_output=True, text=True, timeout=15).stdout.lower()
            self._available = "bluray" in out
        except Exception:
            self._available = False
        return self._available

    def _build_env(self):
        """Expose the MakeMKV dir on PATH so libmmbd's sibling DLLs resolve when
        libbluray loads it. (On Windows the LIBAACS_PATH env var is NOT honored;
        the library must be a file named libaacs.dll next to ffmpeg.exe - see
        _ensure_aacs_dlls.)"""
        env = os.environ.copy()
        if self._libmmbd:
            env["PATH"] = str(self._libmmbd.parent) + os.pathsep + env.get("PATH", "")
        return env

    def _ensure_aacs_dlls(self):
        """libbluray decrypts via a file literally named libaacs.dll (and
        libbdplus.dll) in ffmpeg.exe's own directory. MakeMKV's libmmbd emulates
        both, so we copy it in under those names. Re-copies if missing/stale
        (e.g. after an ffmpeg update). Best-effort: returns False on any failure
        and the feature simply yields no thumbnails."""
        if not self.ffmpeg or not self._libmmbd:
            return False
        ff_dir = Path(self.ffmpeg).resolve().parent
        try:
            src_size = self._libmmbd.stat().st_size
            for name in ("libaacs.dll", "libbdplus.dll"):
                dst = ff_dir / name
                if not dst.exists() or dst.stat().st_size != src_size:
                    shutil.copyfile(self._libmmbd, dst)
            return True
        except OSError:
            return False  # e.g. ffmpeg in a read-only Program Files dir

    @staticmethod
    def _find_libmmbd(makemkvcon_path):
        if not makemkvcon_path:
            return None
        d = Path(makemkvcon_path).parent
        for name in ("libmmbd64.dll", "libmmbd.dll", "libmmbd.so.0", "libmmbd.dylib"):
            if (d / name).exists():
                return d / name
        return None

    # --- extraction ------------------------------------------------------
    def _playlist_no(self, title):
        stem = Path(title.source or "").stem  # "01610.mpls" -> "01610"
        return int(stem) if stem.isdigit() else None

    def _offset(self, title, pos):
        if pos == "tail":
            return max(0, title.duration - self.tail_seconds)
        return max(0, int(title.duration * self.mid_fraction))

    def frame(self, disc, title, pos="mid"):
        """Return a Path to a cached JPEG frame for this title, or None.

        Extracts lazily on first request and caches by disc label + title + pos."""
        if not self.available() or not getattr(disc, "device", ""):
            return None
        playlist = self._playlist_no(title)
        if playlist is None:
            return None

        self.cache.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^\w.-]", "_", disc.label or "disc")[:60]
        out = self.cache / f"{safe}_t{title.id:02d}_{pos}.jpg"
        if out.exists() and out.stat().st_size > 0:
            return out

        device = disc.device.rstrip("\\/")          # "F:"
        # libbluray on Windows expects the drive root, e.g. bluray:F:\
        src = f"bluray:{device}\\" if re.fullmatch(r"[A-Za-z]:", device) else f"bluray:{device}"
        cmd = [
            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-playlist", str(playlist),
            "-ss", str(self._offset(title, pos)),
            "-i", src,
            # thumbnail=... scans a window of frames and picks the most
            # representative one, skipping black/fade/transition frames that a
            # single fixed-offset grab often lands on.
            "-vf", f"thumbnail=n=100,scale={self.width}:-1",
            "-frames:v", "1",
            "-f", "image2", "-vcodec", "mjpeg", "-y", str(out),
        ]
        # Serialize: the browser requests every thumbnail at once, but they all
        # read the same optical drive - running them in parallel thrashes it.
        with self._lock:
            if out.exists() and out.stat().st_size > 0:  # filled while we waited
                return out
            try:
                subprocess.run(cmd, capture_output=True, env=self._env,
                               timeout=self.timeout)
            except (subprocess.TimeoutExpired, OSError):
                return None
        if out.exists() and out.stat().st_size > 0:
            return out
        # clean up a zero-byte file ffmpeg may have created on failure
        try:
            out.unlink(missing_ok=True)
        except OSError:
            pass
        return None
