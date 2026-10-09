@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Python environment missing. See README.md.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -X utf8 -m qianniu.ui --open
