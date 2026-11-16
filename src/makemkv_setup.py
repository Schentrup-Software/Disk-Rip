#!/usr/bin/env python3
"""
makemkv_setup.py - Write MakeMKV's settings (and licence key) before Disk-Rip
starts. The container entrypoint runs it on every start.

MakeMKV keeps its settings in $HOME/.MakeMKV/settings.conf (HOME=/config in the
image, so they persist). The key comes from:

  MAKEMKV_KEY (or the file named by MAKEMKV_KEY_FILE)
      empty or "BETA"  use MakeMKV's free beta key, fetched from the MakeMKV
                       forum at every start (it changes every month or two);
                       the last one fetched is kept as a fallback
      "UNSET"          leave settings.conf alone - you manage it yourself
      anything else    your purchased key

settings.conf is rewritten each start because an expired beta key can leave
MakeMKV with broken settings. The key is never printed or logged.
Stdlib only.
"""

import os
import re
import sys
import urllib.request
from pathlib import Path

BETA_URL = "https://forum.makemkv.com/forum/viewtopic.php?f=5&t=1053"
_CODE_RE = re.compile(r"<code>\s*([^<\s]+)\s*</code>", re.I)


def read_key(environ):
    """The configured key ('' if none), from MAKEMKV_KEY or MAKEMKV_KEY_FILE.
    Whitespace is stripped: a trailing space makes MakeMKV reject a key."""
    path = (environ.get("MAKEMKV_KEY_FILE") or "").strip()
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError as e:
            raise SystemExit(f"MAKEMKV_KEY_FILE can't be read: {e.strerror}")
    return (environ.get("MAKEMKV_KEY") or "").strip()


def fetch_beta_key(timeout=20):
    """The current beta key: the first <code> block of the forum topic."""
    req = urllib.request.Request(BETA_URL, headers={"User-Agent": "Disk-Rip"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        html = r.read().decode("utf-8", "replace")
    m = _CODE_RE.search(html)
    if not m:
        raise ValueError("no key found on the MakeMKV forum page")
    return m.group(1)


def settings_text(key, data_dir):
    lines = [f'app_DataDir = "{data_dir}"',
             'app_UpdateEnable = "0"']
    if key:
        lines.insert(0, f'app_Key = "{key}"')
    return "\n".join(lines) + "\n"


def _write(path, text, mode=0o600):
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def setup(environ=None, home=None, fetch=fetch_beta_key, log=print):
    """Write settings.conf. Returns the key status: 'registered', 'beta',
    'beta-cached' (fetch failed, last known beta key used), 'missing' or
    'unset'."""
    environ = os.environ if environ is None else environ
    home = Path(home or environ.get("HOME") or Path.home()) / ".MakeMKV"
    key = read_key(environ)
    if key.upper() == "UNSET":
        log("MakeMKV: MAKEMKV_KEY=UNSET - leaving settings.conf as it is.")
        return "unset"
    home.mkdir(parents=True, exist_ok=True)
    (home / "data").mkdir(exist_ok=True)
    cache = home / "beta_key"
    if key and key.upper() != "BETA":
        status = "registered"
    else:
        try:
            key = fetch()
            _write(cache, key + "\n")
            status = "beta"
        except Exception as e:                      # offline, forum down, ...
            try:
                key = cache.read_text(encoding="utf-8").strip()
                status = "beta-cached"
                log(f"MakeMKV: couldn't fetch the beta key ({e}); using the last one.")
            except OSError:
                key, status = "", "missing"
                log(f"MakeMKV: couldn't fetch the beta key ({e}) and none is saved - "
                    "ripping Blu-rays will fail until it can be fetched or "
                    "MAKEMKV_KEY is set.")
    _write(home / "settings.conf", settings_text(key, home / "data"))
    log({"registered": "MakeMKV: using your registered key.",
         "beta": "MakeMKV: using the current beta key.",
         "beta-cached": "MakeMKV: using the saved beta key.",
         "missing": "MakeMKV: no key."}[status])
    return status


if __name__ == "__main__":
    try:
        setup()
    except SystemExit:
        raise
    except Exception as e:                          # never block startup
        print(f"MakeMKV setup failed: {e}", file=sys.stderr)
        sys.exit(1)
