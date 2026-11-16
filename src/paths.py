#!/usr/bin/env python3
"""
paths.py - Make sure Disk-Rip only ever writes where you meant it to.

Every library root (rips_root, movie_root, tv_root) and the work_dir pass
through validate_root() before anything is planned, moved or ripped. A root
must be an absolute, existing folder (writable, when we are going to write to
it), and it may not be a URL. A media root may also not sit inside the Disk-Rip
folder or its work_dir.

This exists because of a real accident: movie_root was set to
"smb://nas/Movies/", which Path() quietly turns into the *relative* path
"smb:/nas/Movies", and files were copied into <repo>/smb:/... on the laptop
instead of onto the NAS.

Stdlib only.
"""

import os
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent   # the Disk-Rip folder

# Short name (used in errors and the web API) -> config.json key.
ROOT_KEYS = {"rips": "rips_root", "movies": "movie_root", "tv": "tv_root",
             "work": "work_dir"}
MEDIA_ROOTS = ("rips", "movies", "tv")
URL_PREFIXES = ("smb:", "afp:", "nfs:")


class RootError(Exception):
    """A library root (or a destination under one) is unusable.

    `name` is the short root name ("movies", "tv", "rips", "work" or
    "destination"); `reason` says what is wrong in words a user can act on."""

    def __init__(self, name, reason):
        self.name = name
        self.reason = reason
        super().__init__(f"{ROOT_KEYS.get(name, name)} {reason}")


def looks_like_url(value):
    v = str(value).strip().lower()
    return "://" in v or v.startswith(URL_PREFIXES)


def inside(child, parent):
    """True if `child` is `parent` or anywhere below it, after resolving
    symlinks. Works for paths that don't exist yet."""
    c = os.path.realpath(os.path.expanduser(str(child)))
    p = os.path.realpath(os.path.expanduser(str(parent)))
    if os.name == "nt":
        c, p = os.path.normcase(c), os.path.normcase(p)
    return c == p or c.startswith(p.rstrip(os.sep) + os.sep)


def _write_probe(folder):
    """Create and delete a small file in `folder`. Returns None on success,
    otherwise the reason it failed."""
    probe = Path(folder) / f".diskrip-write-test-{os.getpid()}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        try:
            fd = os.open(probe, flags, 0o600)
        except FileExistsError:          # left behind by an earlier crash
            os.unlink(probe)
            fd = os.open(probe, flags, 0o600)
        os.close(fd)
    except OSError as e:
        return e.strerror or str(e)
    try:
        os.unlink(probe)
    except OSError as e:
        return f"a file could be created but not deleted: {e.strerror or e}"
    return None


def _url_hint():
    return ("Mount the share and use its local folder instead (on macOS "
            "/Volumes/<share>, in Docker or TrueNAS the container path such "
            "as /movies).")


SMB_RE = re.compile(r"^smb://(?:[^@/]+@)?([^/]+)/([^/]+)(/.*)?$", re.I)


def _osascript_mount(url):
    try:
        subprocess.run(["osascript", "-e", f'mount volume "{url}"'],
                       capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        pass


def resolve_spec(name, value, *, platform=None, volumes="/Volumes", mount=None):
    """Turn an smb://host/share/sub address into its local folder on macOS
    (/Volumes/share/sub), asking Finder to mount the share if needed;
    credentials come from the Keychain, or macOS prompts once. Anything that
    isn't an smb:// address is returned unchanged, for validate_root() to
    judge. Raises RootError if the share can't be reached, or on any other OS."""
    value = (value or "").strip()
    m = SMB_RE.match(value)
    if not m:
        return value
    if (platform or sys.platform) != "darwin":
        raise RootError(name, f"is '{value}'. smb:// addresses are only mounted "
                              f"automatically on macOS. {_url_hint()}")
    share = urllib.parse.unquote(m.group(2))
    sub = (m.group(3) or "").strip("/")
    mount_dir = Path(volumes) / share
    if not mount_dir.is_dir():
        (mount or _osascript_mount)(f"smb://{m.group(1)}/{m.group(2)}")
    if not mount_dir.is_dir():
        raise RootError(name, f"could not be mounted ({value}). Connect to it in "
                              "Finder (Go > Connect to Server); it should appear "
                              f"at {mount_dir}.")
    p = mount_dir / urllib.parse.unquote(sub) if sub else mount_dir
    if not p.is_dir():
        raise RootError(name, f"folder not found on the share: {p}")
    return str(p)


def resolve_config_roots(cfg, **kw):
    """Replace every smb:// root in cfg (rips, movies, tv and work) with its
    local folder. Returns {name: reason} for roots that couldn't be resolved;
    those keep their original value."""
    errors = {}
    for name, key in ROOT_KEYS.items():
        value = str(cfg.get(key) or "").strip()
        if not value:
            continue
        try:
            cfg[key] = resolve_spec(name, value, **kw)
        except RootError as e:
            errors[name] = e.reason
    return errors


def validate_root(name, value, *, must_write=True, media=None, app_dir=None,
                  work_dir=None):
    """Return the root as an absolute Path, or raise RootError.

    Rejects: an empty value; a URL (anything containing '://' or starting with
    smb:, afp: or nfs:); a relative path; a path that doesn't exist or isn't a
    folder; a folder we can't write to (when must_write); and, for media roots,
    a folder inside the Disk-Rip folder (app_dir) or inside work_dir."""
    value = "" if value is None else str(value).strip()
    if not value:
        raise RootError(name, "is not set.")
    if looks_like_url(value):
        raise RootError(name, f"is '{value}', which is a network address, "
                              f"not a folder. {_url_hint()}")
    p = Path(value).expanduser()
    if not p.is_absolute():
        raise RootError(name, f"is '{value}', which is a relative path. "
                              "Give the full path to the folder.")
    if not p.exists():
        raise RootError(name, f"folder does not exist: {p}. If it is on a NAS, "
                              "check that the share is connected or mounted.")
    if not p.is_dir():
        raise RootError(name, f"is not a folder: {p}")
    if media is None:
        media = name in MEDIA_ROOTS
    if media:
        app = Path(app_dir) if app_dir else APP_DIR
        if inside(p, app):
            raise RootError(name, f"is inside the Disk-Rip folder ({app}): {p}. "
                                  "Point it at your library instead.")
        if work_dir and inside(p, work_dir):
            raise RootError(name, f"is inside work_dir ({work_dir}): {p}")
    if must_write:
        err = _write_probe(p)
        if err:
            raise RootError(name, f"folder is not writable: {p} ({err})")
    return p


def check_destination(path, *, app_dir=None):
    """Last line of defence before a file is written: `path` must be an
    absolute local path outside the Disk-Rip folder. Doesn't need to exist."""
    s = str(path)
    if looks_like_url(s) or not Path(s).is_absolute():
        raise RootError("destination", f"'{s}' is not an absolute local path; "
                                       "refusing to write there.")
    app = Path(app_dir) if app_dir else APP_DIR
    if inside(s, app):
        raise RootError("destination", f"'{s}' is inside the Disk-Rip folder "
                                       f"({app}); refusing to write there.")


def overlap_warnings(roots):
    """Warnings (not errors) for a library root nested inside the rips root
    or the other way round. `roots` maps short name -> path (or None)."""
    out = []
    rips = roots.get("rips")
    if not rips:
        return out
    for name in ("movies", "tv"):
        lib = roots.get(name)
        if not lib:
            continue
        if inside(lib, rips):
            out.append(f"{ROOT_KEYS[name]} ({lib}) is inside rips_root ({rips}). "
                       "Library scans skip it, but separate folders are safer.")
        elif inside(rips, lib):
            out.append(f"rips_root ({rips}) is inside {ROOT_KEYS[name]} ({lib}). "
                       "Separate folders are safer.")
    return out


def check_roots(cfg, *, app_dir=None, errors=None):
    """Validate every root named in cfg without raising.

    Returns (status, warnings). status maps short name -> {"key", "path",
    "ok", "error"}; a root that isn't set has ok=False and error=None (it is
    only an error once something needs it). `errors` holds reasons found
    earlier (from resolve_config_roots) that take precedence."""
    status, good = {}, {}
    errors = errors or {}
    work = cfg.get("work_dir") or None
    for name, key in ROOT_KEYS.items():
        value = str(cfg.get(key) or "").strip()
        entry = {"key": key, "path": value, "ok": False, "error": None}
        status[name] = entry
        if not value:
            continue
        if name in errors:
            entry["error"] = errors[name]
            continue
        try:
            good[name] = validate_root(name, value, app_dir=app_dir,
                                       work_dir=work if name != "work" else None)
            entry["ok"] = True
        except RootError as e:
            entry["error"] = e.reason
    return status, overlap_warnings(good)
