@echo off
setlocal
cd /d "%~dp0"
echo Iniciando SeriesAdmin...
python run_web.py
if errorlevel 1 pause
endlocal
