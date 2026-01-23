#!/usr/bin/env bash
set -e
pip install -r requirements.txt
export PLAYWRIGHT_BROWSERS_PATH=/opt/render/.cache/ms-playwright
playwright install chromium --with-deps || playwright install chromium
