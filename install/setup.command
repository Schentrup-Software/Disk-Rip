#!/bin/bash
# Double-click this in Finder to set up Disk-Rip on macOS
# (installs prerequisites with Homebrew + writes config.json).
cd "$(dirname "$0")" || exit 1
bash ./setup.sh "$@"
echo
read -r -p "Press Return to close this window. " _
