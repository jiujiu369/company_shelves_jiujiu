@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist ".venv\Scripts\python.exe" (
  echo Python environment missing. See README.md.
  pause
  exit /b 1
)
if "%~1"=="" (
  ".venv\Scripts\python.exe" -m qianniu menu
) else (
  ".venv\Scripts\python.exe" -m qianniu %*
)
