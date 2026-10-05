#!/usr/bin/env bash
# Установка на macOS / Linux
set -e
cd "$(dirname "$0")"
PY=${PYTHON:-python3}
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || {
  echo "Нужен Python 3.10 или новее"; exit 1; }
[ -x .venv/bin/python ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m assistant init
echo
echo "Готово! Заполни .env и config.yaml (см. README.md), затем: ./assistant.sh login"
