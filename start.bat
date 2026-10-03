@echo off
REM Double-click to run the ChatGPT usage display.
cd /d "%~dp0"
python app.py --debug
if errorlevel 1 pause
