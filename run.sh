#!/bin/sh
set -eu
cd "$(dirname "$0")"

BASE_PYTHON="${PYTHON:-python3}"
if ! command -v "$BASE_PYTHON" >/dev/null 2>&1; then
  echo "Python 3 was not found. Install it from https://www.python.org/downloads/macos/ or with Homebrew (brew install python)."
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  "$BASE_PYTHON" -m venv .venv
fi
PYTHON="$(pwd)/.venv/bin/python"
if ! "$PYTHON" -m playwright --version >/dev/null 2>&1; then
  "$PYTHON" -m pip install --upgrade playwright
fi
if [ ! -f .venv/.chromium_installed ]; then
  "$PYTHON" -m playwright install chromium
  touch .venv/.chromium_installed
fi

exec "$PYTHON" mu_bot.py
