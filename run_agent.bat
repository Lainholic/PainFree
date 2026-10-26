@echo off
chcp 949 >nul
title PainFree Snap-Agent (Nebius x NVIDIA)
echo [PainFree Snap-Agent] Starting server powered by Nebius & NVIDIA...

start "" "http://localhost:8585"
python C:\AI\project_nebius_agent\app.py

pause
