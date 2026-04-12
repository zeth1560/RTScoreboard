@echo off
REM Restart scoreboard from repo root (pilot machine). Logs go to the console.
cd /d "%~dp0\.."
python main.py
