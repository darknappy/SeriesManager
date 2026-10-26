@echo off
setlocal
cd /d "%~dp0"
python -m series_manager %*
if errorlevel 1 pause
endlocal
