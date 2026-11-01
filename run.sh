#!/usr/bin/env bash
# Start NetLab. Needs root (creates bridges/veths, runs docker).
# Env: NETLAB_HOST (default 0.0.0.0), NETLAB_PORT (default 8765), NETLAB_TOKEN, NETLAB_DATA
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
exec .venv/bin/python server.py
