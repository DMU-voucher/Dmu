#!/bin/bash
# DMU Food & Drink voucher generator, Mac launcher.
# If double-clicking does nothing, run this once in Terminal:
#   chmod +x "run.command"

cd "$(dirname "$0")" || exit 1

echo
echo "  DMU Food and Drink voucher generator"
echo "  ------------------------------------"
echo "  Checking what it needs..."
echo

PY=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then PY="$candidate"; break; fi
done

if [ -z "$PY" ]; then
  echo "  Python is not installed on this Mac."
  echo "  Install Python 3.10 or newer from python.org, then run this file again."
  echo
  read -r -p "  Press return to close. "
  exit 1
fi

"$PY" -c "import flask" >/dev/null 2>&1 || { echo "  Installing Flask..."; "$PY" -m pip install --quiet flask; }
"$PY" -c "import segno" >/dev/null 2>&1 || { echo "  Installing the QR code library..."; "$PY" -m pip install --quiet segno; }
"$PY" -c "import fitz" >/dev/null 2>&1 || { echo "  Installing the PDF library..."; "$PY" -m pip install --quiet pymupdf; }
if ! "$PY" -c "import playwright" >/dev/null 2>&1; then
  echo "  Installing the PDF printer..."
  "$PY" -m pip install --quiet playwright
  "$PY" -m playwright install chromium
fi

# The very first run draws the whole set of 500,000 voucher numbers, which takes
# about twenty seconds. Do it before the browser opens, or it looks like the app
# has failed to start. Every run after this skips straight past: the numbers are
# already drawn and must never be drawn a second time.
if ! "$PY" -c "import vouchers; vouchers.ensure_pool(progress=lambda m: print('  ' + m))"; then
  echo
  echo "  Could not set up the voucher numbers. The message above says why."
  read -r -p "  Press return to close. "
  exit 1
fi

echo "  Starting. Your browser should open in a moment."
echo "  Leave this window open while you use the app."
echo

( sleep 2; open "http://127.0.0.1:5057" ) &
"$PY" app.py

echo
echo "  The app has stopped."
read -r -p "  Press return to close. "
