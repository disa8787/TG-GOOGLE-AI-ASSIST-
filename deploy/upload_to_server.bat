@echo off
chcp 65001 >nul
cd /d "%~dp0\.."
title Перенос ассистента на сервер
echo.
echo === Перенос ассистента на сервер ===
echo.
echo Перенесётся всё: программа, настройки (.env, config.yaml), архив переписки, память и вход в Telegram.
echo Повторный запуск этого файла обновляет программу на сервере, данные там не трогаются.
echo.
echo ВАЖНО: сначала закрой окно start.bat на этом компьютере —
echo один вход в Telegram нельзя использовать в двух местах одновременно.
echo.
where ssh >nul 2>nul
if errorlevel 1 goto no_ssh

set "SERVER="
set /p SERVER=Адрес сервера, например root@203.0.113.10 : 
if "%SERVER%"=="" goto end

set "FLAG="
set "REPLACE="
if exist "data\MOVED_TO_SERVER.txt" set /p REPLACE=Заменить config.yaml на сервере этим? [y/N]: 
if /i "%REPLACE%"=="y" set "FLAG=--replace-config"

rem Ассистент здесь должен быть закрыт, а база сведена в один файл — иначе копия архива может быть битой
if not exist ".venv\Scripts\python.exe" goto pack
".venv\Scripts\python.exe" -m assistant prepare-move
if errorlevel 1 goto still_running

:pack
set "PKG=%TEMP%\tg-assistant.tar.gz"
echo.
echo Упаковываю...
tar -czf "%PKG%" --exclude=.venv --exclude=__pycache__ --exclude=.git --exclude=assistant.lock --exclude=MOVED_TO_SERVER.txt --exclude=*.log .
if errorlevel 1 goto pack_failed

echo Копирую на сервер (может спросить пароль от сервера)...
scp "%PKG%" "%SERVER%:tg-assistant.tar.gz"
set CODE=%ERRORLEVEL%
del /q "%PKG%" >nul 2>nul
if not "%CODE%"=="0" goto copy_failed

echo Устанавливаю на сервере (может снова спросить пароль)...
ssh -t "%SERVER%" "rm -rf ~/tg-assistant-upload && mkdir -p ~/tg-assistant-upload && tar -xzf ~/tg-assistant.tar.gz -C ~/tg-assistant-upload && rm -f ~/tg-assistant.tar.gz && sed -i 's/\r$//' ~/tg-assistant-upload/deploy/install_server.sh ~/tg-assistant-upload/deploy/tg-assistant; S=bash; [ \"$(id -u)\" -eq 0 ] || S='sudo bash'; $S ~/tg-assistant-upload/deploy/install_server.sh ~/tg-assistant-upload %FLAG%; R=$?; rm -rf ~/tg-assistant-upload; exit $R"
if errorlevel 1 goto install_failed

echo Ассистент перенесён на сервер. Здесь его не запускай: start.bat откажется, пока есть этот файл. > "data\MOVED_TO_SERVER.txt"
echo.
echo Готово! Ассистент работает на сервере круглосуточно.
echo На этом компьютере он отключён (файл data\MOVED_TO_SERVER.txt), чтобы случайно не запустить два сразу.
echo Управление на сервере: подключись (ssh %SERVER%) и набери tg-assistant help
goto end

:no_ssh
echo Не найдена программа ssh. В Windows 10/11 включи её:
echo Параметры - Приложения - Дополнительные компоненты - Добавить компонент - «Клиент OpenSSH».
goto end

:still_running
echo Не получилось подготовить данные (сообщение выше). Чаще всего ассистент ещё запущен:
echo закрой окно start.bat и запусти этот файл снова.
goto end

:pack_failed
echo Не удалось упаковать проект.
goto end

:copy_failed
echo Не удалось скопировать на сервер. Проверь адрес, пароль и что сервер включён.
goto end

:install_failed
echo Установка на сервере не завершилась — смотри сообщения выше.
goto end

:end
echo.
pause
