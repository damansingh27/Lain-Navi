@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"
if not exist "navi-env\Scripts\pythonw.exe" (
  echo Missing virtualenv interpreter at navi-env\Scripts\pythonw.exe
  exit /b 1
)
start "" "navi-env\Scripts\pythonw.exe" "launcher.py"