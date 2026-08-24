#!/bin/bash
cd "$(dirname "$0")"
source .venv/bin/activate
export PYTHONPATH=.
exec python app/main.py
