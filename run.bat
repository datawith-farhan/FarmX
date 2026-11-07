@echo off
title COC Bot - THE KRAKEN
cd /d "%~dp0"
python app.py %*
if errorlevel 1 pause
pause
