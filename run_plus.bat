@echo off
title COC Bot - THE KRAKEN (plus)
cd /d "%~dp0"
python farm_plus.py %*
if errorlevel 1 pause
pause
