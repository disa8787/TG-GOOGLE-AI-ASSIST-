@echo off
chcp 65001 >nul
cd /d "%~dp0"
title ИИ-ассистент
if not exist .venv\Scripts\python.exe (
  echo Сначала запусти install.bat
  pause
  exit /b 1
)
.venv\Scripts\python -m assistant run
echo.
echo Ассистент остановлен.
pause
