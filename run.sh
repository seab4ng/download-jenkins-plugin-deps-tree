#!/usr/bin/env bash
set -e
pip install -r requirements.txt -q 2>/dev/null || \
  pip install -r requirements.txt -q --break-system-packages
python app.py
