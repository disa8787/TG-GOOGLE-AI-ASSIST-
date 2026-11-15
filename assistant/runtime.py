"""Надёжный запуск: понятные сообщения об ошибках, журнал сбоев, защита от второго окна."""

from __future__ import annotations

import os
import platform
import socket
import sqlite3
import sys
import traceback
from datetime import datetime
from pathlib import Path

# Коды выхода для start.bat: 0 — остановили сами, 1 — сбой (перезапуститься), 2 — нужна правка настроек
EXIT_OK = 0
EXIT_CRASH = 1
EXIT_NEEDS_USER = 2

CRASH_LOG = "crash.log"


MOVED_MARKER = "MOVED_TO_SERVER.txt"  # ассистент перенесён на сервер — на этом компьютере не запускать


def on_server() -> bool:
    """Запущен как служба на сервере (deploy/install_server.sh задаёт ASSISTANT_SERVER=1)."""
    return os.getenv("ASSISTANT_SERVER") == "1"


def how_to(action: str) -> str:
    """Как выполнить действие там, где сейчас работает программа: на сервере, в Windows или macOS/Linux."""
    server = {"start": "tg-assistant start", "login": "tg-assistant login", "doctor": "tg-assistant doctor",
              "config": "tg-assistant config"}
    windows = {"start": "start.bat", "login": "start.bat (он сам попросит войти)", "doctor": "doctor.bat",
               "config": "config.yaml в блокноте"}
    unix = {"start": "./assistant.sh run", "login": "./assistant.sh login", "doctor": "./doctor.sh",
            "config": "config.yaml в редакторе"}
    table = server if on_server() else windows if os.name == "nt" else unix
    return table.get(action, action)


def check_not_moved(data_dir: Path) -> None:
    marker = data_dir / MOVED_MARKER
    if marker.exists() and not on_server():
        raise NeedsUser("Ассистент перенесён на сервер и работает там. Два запуска с одним входом в Telegram "
                        "ломают сессию, поэтому здесь он отключён. Чтобы снова запускать на этом компьютере — "
                        f"сначала останови его на сервере (tg-assistant stop), потом удали файл {marker}.")


class NeedsUser(Exception):
    """Ошибка, которую исправляет человек (настройки, ключи, вход) — перезапуск не поможет."""


def explain(exc: BaseException) -> tuple[str, bool] | None:
    """Известная ошибка → (понятное сообщение, нужна ли правка человеком). None — неизвестная."""
    try:
        from telethon import errors as tg
    except Exception:  # noqa: BLE001
        tg = None

    if isinstance(exc, NeedsUser):
        return str(exc), True
    if isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower():
        return ("Ассистент уже запущен в другом окне (или в нём ещё идёт команда). "
                "Закрой лишнее окно и запусти снова."), True
    if tg is not None:
        if isinstance(exc, tg.ApiIdInvalidError):
            return "Неверные TG_API_ID / TG_API_HASH в файле .env — скопируй их заново с my.telegram.org.", True
        if isinstance(exc, (tg.AccessTokenInvalidError, tg.AccessTokenExpiredError)):
            return ("Неверный BOT_TOKEN в файле .env. Возьми токен у @BotFather (/mybots → бот → API Token) "
                    "и вставь целиком, без кавычек и пробелов."), True
        if isinstance(exc, (tg.AuthKeyUnregisteredError, tg.SessionRevokedError, tg.SessionExpiredError,
                            tg.AuthKeyDuplicatedError)):
            return ("Сессия Telegram больше недействительна (вышли из устройства или вход с двух мест одновременно). "
                    f"Войди заново: {how_to('login')}"), True
        if isinstance(exc, (tg.UserDeactivatedError, tg.UserDeactivatedBanError, tg.PhoneNumberBannedError)):
            return "Telegram сообщает, что аккаунт заблокирован или удалён.", True
        if isinstance(exc, tg.PhoneNumberInvalidError):
            return "Неверный номер телефона. Введи его в международном формате, например +79991234567.", True
        if isinstance(exc, tg.FloodWaitError):
            return (f"Telegram просит подождать {getattr(exc, 'seconds', '?')} секунд из-за частых запросов. "
                    "Программа перезапустится сама."), False
    if isinstance(exc, (ConnectionError, TimeoutError, socket.gaierror)):
        return ("Нет связи с Telegram. Проверь интернет. Если Telegram у тебя работает только через "
                "VPN/прокси — укажи прокси в config.yaml (telegram.proxy). Программа попробует снова сама."), False
    return None


def write_crash_log(data_dir: Path, exc: BaseException, command: str) -> Path:
    path = data_dir / CRASH_LOG
    data_dir.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S}  команда: {command}\n")
        f.write(f"Python {sys.version.split()[0]} · {platform.platform()}\n")
        f.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    return path


def last_crash(data_dir: Path) -> tuple[float, str] | None:
    """(время записи, последняя строка ошибки) последнего сбоя — чтобы рассказать о нём после перезапуска."""
    path = data_dir / CRASH_LOG
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return (path.stat().st_mtime, lines[-1][:300]) if lines else None


class InstanceLock:
    """Не даёт запустить две копии, работающие с Telegram, одновременно (иначе «database is locked»
    и даже сброс сессии). Блокировка снимается сама, если программа закрылась или упала."""

    def __init__(self, data_dir: Path):
        self.path = data_dir / "assistant.lock"
        self._fh = None

    def __enter__(self) -> InstanceLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+")  # noqa: SIM115 — держим открытым всё время работы
        try:
            if os.name == "nt":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._fh.close()
            self._fh = None
            raise NeedsUser("Ассистент уже запущен в другом окне. Закрой его (или дождись конца команды) "
                            "и запусти снова.") from None
        return self

    def __exit__(self, *exc) -> None:
        if self._fh is not None:
            try:
                if os.name == "nt":
                    import msvcrt
                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            self._fh.close()
            self._fh = None
