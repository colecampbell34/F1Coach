#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

if command -v python3 >/dev/null 2>&1; then
  python3 -m f1coach dashboard --open-browser --show-packets
elif command -v python >/dev/null 2>&1; then
  python -m f1coach dashboard --open-browser --show-packets
else
  echo "Python 3.10 or newer is required."
  echo "Download Python from https://www.python.org/downloads/"
  read -r -p "Press Enter to close..."
  exit 1
fi
