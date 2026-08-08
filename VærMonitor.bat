@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
)
python weather_monitor_gui.py
pause
