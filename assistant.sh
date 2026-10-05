#!/usr/bin/env bash
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || { echo "Сначала запусти ./install.sh"; exit 1; }
exec .venv/bin/python -m assistant "$@"
