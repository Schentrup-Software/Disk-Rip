#!/bin/sh
# Container entrypoint: make sure there is a config file, then start Disk-Rip.
#
# The first start writes /config/config.json with the container's folder
# layout (/rips, /movies, /tv). Edit that file to tune anything else; it is
# kept across restarts. DISKRIP_TMDB_API_KEY, when set, is copied into it on
# every start so the key can live in the app settings instead.
set -eu

CFG="${DISKRIP_CONFIG_FILE:-/config/config.json}"
DIR="$(dirname "$CFG")"
mkdir -p "$DIR" 2>/dev/null || true
if [ ! -w "$DIR" ]; then
  echo "ERROR: $DIR is not writable by uid $(id -u). Give that user write access to the config folder." >&2
  exit 1
fi

python3 - "$CFG" <<'EOF'
import json
import os
import sys

path = sys.argv[1]
defaults = {
    "tmdb_api_key": "",
    "makemkvcon": "",
    "rips_root": "/rips",
    "movie_root": "/movies",
    "tv_root": "/tv",
    "work_dir": os.path.join(os.path.dirname(path), "work"),
    "ffmpeg": "/usr/bin/ffmpeg",
    "ffprobe": "/usr/bin/ffprobe",
    "min_length_minutes": 15,
    "movie_min_minutes": 70,
    "language": "en",
    "write_nfo": False,
    "segment_overlap_threshold": 0.6,
    "thumb_mid_fraction": 0.40,
    "thumb_tail_seconds": 120,
    "thumb_width": 240,
    "discdb": True,
    "discdb_endpoint": "https://thediscdb.com/graphql/",
    "discdb_timeout": 4,
    "library_ignore": [],
    "extras_min_seconds": 60,
}
try:
    with open(path, encoding="utf-8-sig") as f:
        cfg = json.load(f)
    created = False
except FileNotFoundError:
    cfg, created = dict(defaults), True

key = os.environ.get("DISKRIP_TMDB_API_KEY", "").strip()
changed = created
if key and cfg.get("tmdb_api_key") != key:
    cfg["tmdb_api_key"], changed = key, True

if changed:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, path)
if created:
    print(f"Wrote default settings to {path}")
if not cfg.get("tmdb_api_key"):
    print("WARNING: no TMDB API key - movies can't be identified. "
          "Set DISKRIP_TMDB_API_KEY or edit " + path, file=sys.stderr)
EOF

exec "$@"
