@echo off
title COC Bot - Self Test (no device needed)
cd /d "%~dp0"
python farm_plus.py --selftest
pause
