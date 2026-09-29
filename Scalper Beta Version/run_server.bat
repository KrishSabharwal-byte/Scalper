@echo off
title Slicer Nifty x Sensex Server
cd /d "%~dp0"
echo Starting Slicer Server on http://localhost:8000 ...
python app.py
pause
