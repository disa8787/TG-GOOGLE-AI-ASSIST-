#!/usr/bin/env bash
# Установка или обновление ассистента на сервере (Ubuntu 22.04+ / Debian 12+).
#
#   sudo bash deploy/install_server.sh [папка_с_проектом] [--replace-config]
#
# Первая установка: переносит код, архив (data), настройки (.env, config.yaml, credentials),
# ставит библиотеки, создаёт службу и запускает её.
# Повторный запуск = обновление: меняется только код. Архив, память и настройки на сервере не трогаются
# (кроме config.yaml, если указать --replace-config).
set -euo pipefail

SRC=""
REPLACE_CONFIG=0
for arg in "$@"; do
    case "$arg" in
        --replace-config) REPLACE_CONFIG=1 ;;
        *) SRC="$arg" ;;
    esac
done
SRC="${SRC:-$(cd "$(dirname "$0")/.." && pwd)}"
SRC="$(cd "$SRC" && pwd)"

# Пути можно переопределить переменными окружения (например, для проверки установки в отдельную папку)
APP="${TG_APP_DIR:-/opt/tg-assistant}"
APP_USER="${TG_APP_USER:-tg-assistant}"
BIN_DIR="${TG_BIN_DIR:-/usr/local/bin}"
CRON_DIR="${TG_CRON_DIR:-/etc/cron.daily}"
BACKUP_DIR="${TG_BACKUP_DIR:-/var/backups/tg-assistant}"
SVC=tg-assistant
HAS_SYSTEMD=0
[ -d /run/systemd/system ] && [ "${TG_NO_SYSTEMD:-0}" != "1" ] && HAS_SYSTEMD=1

say() { printf '\n== %s\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "Запусти от root: sudo bash $0 $*"; exit 1; }
[ -f "$SRC/assistant/__main__.py" ] || { echo "Не нашёл проект в $SRC"; exit 1; }

say "Системные пакеты"
if [ "${TG_SKIP_APT:-0}" != "1" ]; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq python3 python3-venv python3-pip rsync nano >/dev/null
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || { echo "Нужен Python 3.10 или новее (Ubuntu 22.04+ / Debian 12+)"; exit 1; }

say "Служебный пользователь $APP_USER (ассистент работает не от root)"
id "$APP_USER" >/dev/null 2>&1 || useradd --system --home-dir "$APP" --no-create-home --shell /usr/sbin/nologin "$APP_USER"

FIRST_INSTALL=1
[ -d "$APP/assistant" ] && FIRST_INSTALL=0
if [ "$HAS_SYSTEMD" -eq 1 ] && systemctl is-active --quiet "$SVC"; then
    say "Останавливаю работающего ассистента на время обновления"
    systemctl stop "$SVC"
fi

say "Код программы"
mkdir -p "$APP"
rsync -a --delete \
    --exclude .venv --exclude __pycache__ --exclude .git \
    --exclude /data --exclude /.env --exclude /config.yaml --exclude /credentials \
    "$SRC/" "$APP/"

say "Архив и настройки"
copied=""
if [ -d "$SRC/data" ] && [ ! -e "$APP/data" ]; then
    rsync -a --exclude assistant.lock --exclude MOVED_TO_SERVER.txt --exclude '*.log' "$SRC/data/" "$APP/data/"
    copied="$copied архив(data)"
fi
if [ -d "$SRC/credentials" ] && [ ! -e "$APP/credentials" ]; then
    cp -a "$SRC/credentials" "$APP/credentials"; copied="$copied credentials"
fi
if [ -f "$SRC/.env" ] && [ ! -e "$APP/.env" ]; then
    cp "$SRC/.env" "$APP/.env"; copied="$copied .env"
fi
if [ -f "$SRC/config.yaml" ] && { [ ! -e "$APP/config.yaml" ] || [ "$REPLACE_CONFIG" -eq 1 ]; }; then
    cp "$SRC/config.yaml" "$APP/config.yaml"; copied="$copied config.yaml"
fi
[ -f "$APP/config.yaml" ] || cp "$APP/config.example.yaml" "$APP/config.yaml"
[ -f "$APP/.env" ] || cp "$APP/.env.example" "$APP/.env"
mkdir -p "$APP/data" "$APP/credentials"
rm -f "$APP/data/assistant.lock" "$APP/data/MOVED_TO_SERVER.txt"
if [ -n "$copied" ]; then echo "Перенесено:$copied"; else echo "Данные и настройки на сервере сохранены как есть"; fi

say "Python-библиотеки (пара минут)"
[ -x "$APP/.venv/bin/python" ] || python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q --upgrade pip
"$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"

say "Часовой пояс"
# на сервере часы обычно в UTC — без явного пояса отчёты пришли бы не вовремя
TZ_NOW=$("$APP/.venv/bin/python" - "$APP/config.yaml" <<'PYEOF'
import sys, yaml
try:
    data = yaml.safe_load(open(sys.argv[1], encoding="utf-8-sig")) or {}
except Exception:
    data = {}
print((data.get("timezone") or "").strip() if isinstance(data, dict) else "")
PYEOF
)
if [ -z "$TZ_NOW" ]; then
    NEW_TZ="${TG_TIMEZONE:-}"
    if [ -z "$NEW_TZ" ] && [ -t 0 ]; then
        read -r -p "В каком часовом поясе ты работаешь? (например Europe/Moscow, America/Chicago): " NEW_TZ || true
    fi
    if [ -n "$NEW_TZ" ] && "$APP/.venv/bin/python" -c "import sys, zoneinfo; zoneinfo.ZoneInfo(sys.argv[1])" "$NEW_TZ" 2>/dev/null; then
        if grep -qE '^timezone:' "$APP/config.yaml"; then
            sed -i -E "s|^timezone:.*$|timezone: \"$NEW_TZ\"|" "$APP/config.yaml"
        else
            printf 'timezone: "%s"\n' "$NEW_TZ" | cat - "$APP/config.yaml" > "$APP/config.yaml.tmp" && mv "$APP/config.yaml.tmp" "$APP/config.yaml"
        fi
        echo "Часовой пояс: $NEW_TZ"
    else
        echo "⚠️  Часовой пояс не задан — укажи его: tg-assistant config (строка timezone)"
    fi
else
    echo "Часовой пояс: $TZ_NOW"
fi

say "Права доступа (ключи и сессия Telegram — только для ассистента)"
chown -R "$APP_USER:$APP_USER" "$APP"
chmod 700 "$APP/data" "$APP/credentials"
chmod 600 "$APP/.env"
find "$APP/data" -maxdepth 1 -name '*.session' -exec chmod 600 {} +

say "Команда tg-assistant и ежедневная резервная копия"
mkdir -p "$BIN_DIR"
sed -e "s|^APP=.*|APP=\"$APP\"|" -e "s|^APP_USER=.*|APP_USER=$APP_USER|" \
    -e "s|^BACKUP_DIR=.*|BACKUP_DIR=\"$BACKUP_DIR\"|" "$APP/deploy/tg-assistant" > "$BIN_DIR/tg-assistant"
chmod 755 "$BIN_DIR/tg-assistant"
if [ -d "$CRON_DIR" ]; then
    printf '#!/bin/sh\n%s/tg-assistant backup >/dev/null 2>&1\n' "$BIN_DIR" > "$CRON_DIR/tg-assistant-backup"
    chmod 755 "$CRON_DIR/tg-assistant-backup"
fi

if [ "$HAS_SYSTEMD" -eq 1 ]; then
    say "Служба (автозапуск и перезапуск после сбоя)"
    sed -e "s|/opt/tg-assistant|$APP|g" -e "s|^User=.*|User=$APP_USER|" -e "s|^Group=.*|Group=$APP_USER|" \
        "$APP/deploy/tg-assistant.service" > "/etc/systemd/system/$SVC.service"
    chmod 644 "/etc/systemd/system/$SVC.service"
    systemctl daemon-reload
    systemctl enable --quiet "$SVC"
else
    say "systemd не найден — службу не ставлю (запуск вручную: tg-assistant run)"
fi

if [ ! -f "$APP/data/user.session" ]; then
    say "Вход в Telegram"
    if [ -t 0 ]; then
        runuser -u "$APP_USER" -- env ASSISTANT_SERVER=1 HOME="$APP" "$APP/.venv/bin/python" -m assistant login || true
    else
        echo "Войди в Telegram: tg-assistant login"
    fi
fi

if [ "$HAS_SYSTEMD" -eq 1 ]; then
    say "Запуск"
    systemctl restart "$SVC"
    sleep 5
    systemctl --no-pager status "$SVC" | head -5 || true
fi

if [ "$HAS_SYSTEMD" -eq 1 ]; then
    printf '\nГотово ✅ Ассистент работает на сервере и будет работать всегда, в том числе после перезагрузки.\n'
else
    printf '\nГотово ✅ Установлено. systemd нет — запускай вручную: tg-assistant run\n'
fi
cat <<'EOF'

  tg-assistant status   — работает ли
  tg-assistant logs     — что он делает прямо сейчас (выход — Ctrl+C)
  tg-assistant doctor   — проверить всё
  tg-assistant help     — все команды

Не запускай ассистента одновременно на своём компьютере — один вход в Telegram нельзя использовать в двух местах.
EOF
