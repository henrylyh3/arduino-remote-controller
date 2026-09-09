#!/bin/sh
set -eu

cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi

.venv/bin/python -m pip install -r requirements-build.txt
.venv/bin/python -m PyInstaller --noconfirm --clean SmartHomeController.spec

printf '\nBuilt: %s\n' "$(pwd)/dist/Smart Home Controller.app"
