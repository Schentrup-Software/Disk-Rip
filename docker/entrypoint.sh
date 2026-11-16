#!/bin/sh
# Container entrypoint.
#
# 1. Started as root (the image default), it drops to PUID:PGID (default
#    568:568) before running anything else, adding the group that owns each
#    optical drive passed in (/dev/srN, /dev/sgN; usually cdrom, 24) so MakeMKV
#    can use it. It never adds group 0 (root) or 6 (disk, which can read every
#    hard drive). Started as another user (docker run --user), it skips this.
# 2. Makes sure /config/config.json exists, with the container's folder layout
#    (/rips, /movies, /tv) on first start. DISKRIP_TMDB_API_KEY, when set, is
#    copied into it on every start.
# 3. Writes MakeMKV's settings and key (src/makemkv_setup.py; see MAKEMKV_KEY).
set -eu

DEV_DIR="${DISKRIP_DEV_DIR:-/dev}"     # overridable for tests only

if [ "$(id -u)" = "0" ]; then
  PUID="${PUID:-568}"
  PGID="${PGID:-568}"
  groups=""
  for dev in "$DEV_DIR"/sr[0-9]* "$DEV_DIR"/sg[0-9]*; do
    [ -e "$dev" ] || continue
    g="$(stat -c %g "$dev")"
    case "$g" in
      0|6)
        echo "WARNING: $dev belongs to group $g; not joining it (it would give access to more than the drive). Make the drive's group cdrom (24) on the host." >&2
        continue ;;
    esac
    case ",$groups," in *",$g,"*) ;; *) groups="${groups:+$groups,}$g" ;; esac
  done
  if [ -n "${PGROUPS:-}" ]; then
    groups="${groups:+$groups,}$PGROUPS"
  fi
  # a brand-new, empty config folder created by root: hand it to the user
  CFG_DIR="$(dirname "${DISKRIP_CONFIG_FILE:-/config/config.json}")"
  mkdir -p "$CFG_DIR"
  if [ "$(stat -c %u "$CFG_DIR")" = "0" ] && [ -z "$(ls -A "$CFG_DIR")" ]; then
    chown "$PUID:$PGID" "$CFG_DIR"
  fi
  echo "Running as uid $PUID, gid $PGID${groups:+, extra groups $groups}"
  if [ -n "$groups" ]; then
    exec setpriv --reuid="$PUID" --regid="$PGID" --groups="$groups" --inh-caps=-all -- "$0" "$@"
  fi
  exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups --inh-caps=-all -- "$0" "$@"
fi

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
import shutil
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

changed = created
key = os.environ.get("DISKRIP_TMDB_API_KEY", "").strip()
if key and cfg.get("tmdb_api_key") != key:
    cfg["tmdb_api_key"], changed = key, True
# the image's MakeMKV, unless the config points at another one that exists
mkv = shutil.which("makemkvcon") or ""
if mkv and not os.path.isfile(cfg.get("makemkvcon") or ""):
    cfg["makemkvcon"], changed = mkv, True

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

if command -v makemkvcon >/dev/null 2>&1; then
  python3 /app/src/makemkv_setup.py || echo "WARNING: MakeMKV setup failed; ripping may not work." >&2
fi

exec "$@"
