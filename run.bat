@echo off
setlocal
cd /d "%~dp0"
title DMU Food and Drink voucher generator

echo.
echo   DMU Food and Drink voucher generator
echo   ------------------------------------
echo   Checking what it needs...
echo.

where python >nul 2>&1
if errorlevel 1 (
  echo   Python is not installed on this computer, or is not on the PATH.
  echo   Install Python 3.10 or newer from python.org, tick "Add to PATH"
  echo   during setup, then run this file again.
  echo.
  pause
  exit /b 1
)

python -c "import flask" >nul 2>&1
if errorlevel 1 (
  echo   Installing Flask...
  python -m pip install --quiet flask
)

python -c "import segno" >nul 2>&1
if errorlevel 1 (
  echo   Installing the QR code library...
  python -m pip install --quiet segno
)

python -c "import fitz" >nul 2>&1
if errorlevel 1 (
  echo   Installing the PDF library...
  python -m pip install --quiet pymupdf
)

python -c "import playwright" >nul 2>&1
if errorlevel 1 (
  echo   Installing the PDF printer...
  python -m pip install --quiet playwright
  python -m playwright install chromium
)

rem The very first run draws the whole set of 500,000 voucher numbers, which
rem takes about twenty seconds. Do it before the browser opens, or it looks like
rem the app has failed to start. Every run after this skips straight past: the
rem numbers are already drawn and must never be drawn a second time.
python -c "import vouchers; vouchers.ensure_pool(progress=lambda m: print('  ' + m))"
if errorlevel 1 (
  echo.
  echo   Could not set up the voucher numbers. The message above says why.
  echo.
  pause
  exit /b 1
)

echo   Starting. Your browser should open in a moment.
echo   Leave this window open while you use the app. Close it when you are done.
echo.

start "" http://127.0.0.1:5057
python app.py

echo.
echo   The app has stopped.
pause
