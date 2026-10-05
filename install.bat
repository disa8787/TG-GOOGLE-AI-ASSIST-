@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === Установка ИИ-ассистента ===
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo Python не найден. Установи Python 3.11 или новее с https://www.python.org/downloads/
  echo При установке обязательно отметь галочку "Add python.exe to PATH".
  pause
  exit /b 1
)
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 (
  echo Нужен Python 3.10 или новее. Обнови Python с https://www.python.org/downloads/
  pause
  exit /b 1
)
if not exist .venv\Scripts\python.exe (
  echo Создаю окружение .venv ...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo Не удалось создать окружение.
    pause
    exit /b 1
  )
)
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt
if errorlevel 1 (
  echo Ошибка установки библиотек. Проверь интернет и запусти install.bat ещё раз.
  pause
  exit /b 1
)
.venv\Scripts\python -m assistant init
echo.
echo Готово! Теперь заполни файлы .env и config.yaml по инструкции из README.md,
echo затем выполни:  assistant.bat login
pause
