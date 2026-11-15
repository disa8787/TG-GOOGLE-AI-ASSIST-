@echo off
chcp 65001 >nul
cd /d "%~dp0"
title ИИ-ассистент
if not exist .venv\Scripts\python.exe (
  echo Сначала запусти install.bat
  pause
  exit /b 1
)
set RESTARTS=0
:loop
.venv\Scripts\python -m assistant run
set CODE=%ERRORLEVEL%
if "%CODE%"=="0" goto stopped
if "%CODE%"=="2" goto needs_user
set /a RESTARTS+=1
if %RESTARTS% GEQ 20 goto too_many
echo.
echo Сбой (подробности в data\crash.log). Перезапуск через 30 секунд... Чтобы остановить — закрой это окно.
timeout /t 30 /nobreak >nul
goto loop

:needs_user
echo.
echo Нужно поправить настройки — см. сообщение выше.
echo Проверить всё сразу: двойной щелчок по doctor.bat
pause
exit /b 2

:too_many
echo.
echo Слишком много сбоев подряд. Запусти doctor.bat и пришли файл data\crash.log
pause
exit /b 1

:stopped
echo.
echo Ассистент остановлен.
pause
