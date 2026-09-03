#!/bin/sh
set -eu

python -m pip install -r requirements.txt
python -m pip install --no-deps -r requirements-voice.txt
python -m pip check
