#!/usr/bin/env bash
pip install -r requirements.txt
export PLAYWRIGHT_BROWSERS_PATH=/opt/render/.cache/ms-playwright
playwright install chromium
