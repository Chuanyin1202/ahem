#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${AHEM_PYTHON:-python}"
export PYTHONPATH="${PYTHONPATH:-src}"

echo "[demo-team] focused runtime tests"
"$PYTHON_BIN" -m pytest -q \
  tests/test_spectator.py \
  tests/test_spectator_auth.py \
  tests/test_azure_voice.py \
  tests/test_voice.py \
  tests/test_live_shutdown.py

echo "[demo-team] full public test suite"
"$PYTHON_BIN" -m pytest -q tests

echo "[demo-team] confirm the demo UI does not contain redaction placeholders"
if git grep -n '已隱去' -- \
    src/meeting_host/spectator.py \
    src/meeting_host/spectator/index.html; then
  echo "Demo UI contains redaction placeholders" >&2
  exit 1
fi

echo "[demo-team] PASS"
