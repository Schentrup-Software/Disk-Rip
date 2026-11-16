#!/bin/bash
#
# Disk-Rip one-time setup for macOS.
#
# Installs the prerequisites with Homebrew (Python 3, ffmpeg and - optionally -
# MakeMKV), then asks for your TMDB key and folders and writes config.json.
#
#   bash install/setup.sh                 (or double-click install/setup.command)
#   bash install/setup.sh --skip-install  (only write / update config.json)
#
# Anything else on the command line is passed to install/configure.py, e.g.
#   bash install/setup.sh --non-interactive --tmdb-key abc123 \
#        --rips-root smb://nas.example.com/rips --movie-root /Volumes/rips/Movies
#
# Re-running is safe: existing config.json values become the defaults.

set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"

SKIP_INSTALL=0
WITH_MAKEMKV=ask
PASS=()
for arg in "$@"; do
  case "$arg" in
    --skip-install)    SKIP_INSTALL=1 ;;
    --with-makemkv)    WITH_MAKEMKV=yes ;;
    --without-makemkv) WITH_MAKEMKV=no ;;
    --non-interactive) PASS+=("$arg"); [ "$WITH_MAKEMKV" = ask ] && WITH_MAKEMKV=no ;;
    *)                 PASS+=("$arg") ;;
  esac
done

info() { printf '\033[36m%s\033[0m\n' "$*"; }
ok()   { printf '\033[32m  OK  \033[0m%s\n' "$*"; }
warn() { printf '\033[33m  !   \033[0m%s\n' "$*"; }
fail() { printf '\033[31mERROR: \033[0m%s\n' "$*"; exit 1; }

info ""
info "=== Disk-Rip setup (macOS) ==="
info ""

[ "$(uname -s)" = "Darwin" ] || warn "this script is written for macOS; continuing anyway"

# --- Homebrew -----------------------------------------------------------------
BREW="$(command -v brew || true)"
for cand in /opt/homebrew/bin/brew /usr/local/bin/brew "$HOME/.homebrew/bin/brew"; do
  [ -z "$BREW" ] && [ -x "$cand" ] && BREW="$cand"
done
[ -n "$BREW" ] && eval "$("$BREW" shellenv)"   # puts brew's bin on PATH for this run

py_ok() { command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; }
have_makemkv() { [ -x /Applications/MakeMKV.app/Contents/MacOS/makemkvcon ] || [ -x "$HOME/Applications/MakeMKV.app/Contents/MacOS/makemkvcon" ]; }

if [ "$SKIP_INSTALL" = 1 ]; then
  warn "--skip-install: not installing anything"
else
  if [ -z "$BREW" ]; then
    if py_ok && command -v ffprobe >/dev/null 2>&1; then
      warn "Homebrew not found, but Python 3 and ffmpeg are already here - continuing"
    else
      fail "Homebrew is needed to install the prerequisites. Install it from https://brew.sh, then re-run this script."
    fi
  else
    if py_ok; then ok "Python 3 already installed ($(python3 --version 2>&1))"
    else info "Installing Python 3 ..."; "$BREW" install python || warn "brew install python failed"; fi

    if command -v ffprobe >/dev/null 2>&1 && command -v ffmpeg >/dev/null 2>&1; then
      ok "ffmpeg already installed"
    else
      info "Installing ffmpeg (ffprobe reads each file's length and resolution) ..."
      "$BREW" install ffmpeg || warn "brew install ffmpeg failed"
    fi

    if have_makemkv; then
      ok "MakeMKV already installed"
    else
      if [ "$WITH_MAKEMKV" = ask ]; then
        echo
        echo "  MakeMKV is only needed to RIP discs. Organizing folders you have"
        echo "  already ripped works without it."
        printf "  Install MakeMKV now? [y/N] "
        read -r reply || reply=""
        case "$reply" in y|Y|yes|YES) WITH_MAKEMKV=yes ;; *) WITH_MAKEMKV=no ;; esac
      fi
      if [ "$WITH_MAKEMKV" = yes ]; then
        info "Installing MakeMKV ..."
        "$BREW" install --cask makemkv || warn "brew install --cask makemkv failed - get it from https://www.makemkv.com"
      else
        warn "skipping MakeMKV (re-run with --with-makemkv to add it later)"
      fi
    fi
  fi
fi

py_ok || fail "Python 3.9+ not found. Install it (brew install python) and re-run."

# --- config + verification (shared, cross-platform) ---------------------------
exec python3 "$HERE/configure.py" ${PASS[@]+"${PASS[@]}"}
