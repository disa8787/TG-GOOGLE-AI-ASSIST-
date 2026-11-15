"""Проверка всего, что нужно ассистенту: настройки, ключи, Telegram, бот, Claude, таблицы, архив."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys

from .config import Config

TIMEOUT = 40  # секунд на каждую сетевую проверку
MIN_FREE_GB = 1.0  # меньше — архив, журнал и резервные копии могут упереться в диск


class Report:
    def __init__(self):
        self.problems: list[str] = []

    def line(self, ok: bool, text: str, fix: str = "") -> bool:
        print(("  ✅ " if ok else "  ❌ ") + text, flush=True)
        if not ok:
            if fix:
                print(f"       → {fix}", flush=True)
            self.problems.append(text)
        return ok

    def info(self, text: str) -> None:
        print(f"  ℹ️  {text}", flush=True)


def how_to(action: str) -> str:
    from .runtime import how_to as _how_to

    return _how_to(action)


def _err(e: BaseException) -> str:
    from .runtime import explain

    known = explain(e)
    return known[0] if known else f"{type(e).__name__}: {e}"


async def check_telegram(cfg: Config, rep: Report) -> int | None:
    """Вход в аккаунт (только чтение). Возвращает id владельца или None."""
    from .telegram_archive import connect_with_timeout, make_user_client

    if not (cfg.api_id and cfg.api_hash):
        return None
    client = make_user_client(cfg)
    try:
        await connect_with_timeout(client)
        if not await client.is_user_authorized():
            rep.line(False, "Telegram: вход ещё не выполнен", f"выполни: {how_to('login')}")
            return None
        me = await client.get_me()
        rep.line(True, f"Telegram: вход выполнен — {me.first_name or ''} {me.last_name or ''}".rstrip())
        dialogs = await asyncio.wait_for(client.get_dialogs(limit=50), TIMEOUT)
        rep.line(True, f"Telegram: чаты видны (первые {len(dialogs)})")
        return me.id
    except asyncio.TimeoutError:
        rep.line(False, "Telegram: не удалось подключиться за 40 секунд",
                 "проверь интернет; если Telegram работает только через VPN/прокси — укажи telegram.proxy")
    except Exception as e:  # noqa: BLE001
        rep.line(False, f"Telegram: {_err(e)}")
    finally:
        await client.disconnect()
    return None


async def check_bot(cfg: Config, rep: Report, owner_id: int | None) -> None:
    from .telegram_archive import make_bot_client

    bot = make_bot_client(cfg)
    try:
        await asyncio.wait_for(bot.start(bot_token=cfg.bot_token), TIMEOUT)
        me = await bot.get_me()
        rep.line(True, f"Бот: @{me.username} работает")
        target = cfg.owner_id or owner_id
        if target:
            try:
                await bot.send_message(target, "✅ Проверка связи (assistant doctor): я могу тебе писать.")
                rep.line(True, "Бот может писать тебе (проверь сообщение в Telegram)")
            except Exception:  # noqa: BLE001
                rep.line(False, "Бот не может написать тебе", f"открой @{me.username} в Telegram и нажми Start")
    except asyncio.TimeoutError:
        rep.line(False, "Бот: не удалось подключиться за 40 секунд", "проверь интернет / telegram.proxy")
    except Exception as e:  # noqa: BLE001
        rep.line(False, f"Бот: {_err(e)}")
    finally:
        await bot.disconnect()


async def check_claude(cfg: Config, rep: Report) -> None:
    import anthropic

    client = anthropic.AsyncAnthropic(max_retries=1)
    models = {cfg.model, cfg.watch_model}
    for model in sorted(models):
        try:
            await asyncio.wait_for(client.models.retrieve(model), TIMEOUT)  # бесплатный запрос
            rep.line(True, f"Claude: ключ работает, модель {model} доступна")
        except anthropic.AuthenticationError:
            rep.line(False, "Claude: неверный ключ",
                     "проверь ANTHROPIC_API_KEY в .env (console.anthropic.com → API Keys)")
            return
        except anthropic.NotFoundError:
            rep.line(False, f"Claude: модель {model} недоступна", "поменяй claude.model в config.yaml")
        except anthropic.PermissionDeniedError as e:
            rep.line(False, f"Claude: нет доступа ({e.message})")
        except (anthropic.APIConnectionError, asyncio.TimeoutError):
            rep.line(False, "Claude: нет связи с сервером", "проверь интернет")
            return
        except TypeError as e:  # нет ключа совсем
            rep.line(False, f"Claude: {e}", "впиши ANTHROPIC_API_KEY в .env")
            return
        except anthropic.APIStatusError as e:
            rep.line(False, f"Claude: ошибка {e.status_code}: {e.message}")


async def check_sheets(cfg: Config, rep: Report) -> None:
    from .sheets import Sheets

    if not cfg.sheets:
        rep.info("Google-таблицы не настроены (необязательно)")
        return
    if not cfg.google_credentials.exists():
        rep.line(False, f"Google: нет файла ключа {cfg.google_credentials}", "см. README, раздел «Google Таблицы»")
        return
    sheets = Sheets(cfg)
    email = sheets.service_email()
    for source in cfg.sheets:
        try:
            titles = await asyncio.wait_for(sheets.aworksheet_titles(source), TIMEOUT)
            rep.line(True, f"Таблица «{source.name}»: листы {', '.join(titles[:6])}")
        except Exception as e:  # noqa: BLE001
            text = f"{type(e).__name__}: {e}"
            fix = (f"открой таблицу → «Настройки доступа» → добавь {email} как «Читатель»"
                   if ("403" in text or "PERMISSION" in text.upper() or "NotFound" in text) and email else "")
            rep.line(False, f"Таблица «{source.name}»: {text[:150]}", fix)


def check_archive(cfg: Config, rep: Report) -> None:
    from .db import DB
    from .runtime import data_file_hint, last_crash

    db = DB(cfg.data_dir / "assistant.db")
    try:
        st = db.message_stats()
        chats = db.chats()
        learned = db.conn.execute("SELECT count(*) AS n FROM messages WHERE learned=1").fetchone()["n"]
        rep.info(f"Архив: {st['n']} сообщений в {len(chats)} чатах, изучено {learned}; "
                 f"заметок в памяти {db.memory_count()}")
        if not st["n"]:
            rep.info(f"Архив пуст — при запуске ({how_to('start')}) история скачается")
        crash = last_crash(cfg.data_dir)
        if crash:
            rep.info(f"Последний сбой: {crash[1]} ({data_file_hint('crash.log')})")
    finally:
        db.conn.close()


def check_disk(cfg: Config, rep: Report) -> None:
    """Место на диске и права на запись: на сервере это частые причины «молча перестал работать»."""
    size = 0
    not_writable = []
    for path in cfg.data_dir.rglob("*"):
        try:
            if path.is_file():
                size += path.stat().st_size
            if not os.access(path, os.W_OK):
                not_writable.append(path.name)
        except OSError:
            continue
    rep.info(f"Папка data занимает {size / 1024 ** 2:.0f} МБ")
    free = shutil.disk_usage(cfg.data_dir).free / 1024 ** 3
    rep.line(free >= MIN_FREE_GB, f"Свободно на диске: {free:.1f} ГБ",
             "освободи место (старые резервные копии, журналы) — иначе архив перестанет пополняться")
    writable = os.access(cfg.data_dir, os.W_OK) and not not_writable
    rep.line(writable, "Папка data доступна для записи" + (
        f" — кроме: {', '.join(not_writable[:5])}" if not_writable else ""),
        "tg-assistant restart (права поправятся сами)" if _on_server() else "проверь права на папку data")


def _on_server() -> bool:
    from .runtime import on_server

    return on_server()


async def run_doctor(cfg: Config) -> bool:
    from .runtime import InstanceLock, NeedsUser, on_server
    from .telegram_archive import proxy_settings

    rep = Report()
    print("\nПроверяю ассистента…\n", flush=True)

    print("Компьютер и настройки:")
    rep.line(sys.version_info >= (3, 10), f"Python {sys.version.split()[0]}", "нужен Python 3.10 или новее")
    rep.line(not cfg.warnings, "config.yaml прочитан" + (" — есть ошибки:" if cfg.warnings else ""),
             "; ".join(cfg.warnings))
    tz_set = bool(str(cfg.raw.get("timezone") or "").strip())
    if on_server():
        rep.line(tz_set, f"Часовой пояс: {cfg.tz_name}" if tz_set else "Часовой пояс не задан (на сервере это UTC)",
                 f"укажи свой: {how_to('config')} → строка timezone, например Europe/Moscow")
    else:
        rep.info(f"Часовой пояс: {cfg.tz_name}" + ("" if tz_set else " (часовой пояс компьютера)"))
    rep.info(f"Чаты: {'все' if cfg.all_chats else ', '.join(map(str, cfg.chats))}; модель {cfg.model}")
    if cfg.commands:
        rep.info("Свои команды: " + ", ".join(f"/{c}" for c in cfg.commands))
    if cfg.schedule:
        rep.info("Задачи по расписанию: " + ", ".join(f"{t.name} ({t.time})" for t in cfg.schedule))
    rep.line(bool(cfg.api_id and cfg.api_hash), "TG_API_ID и TG_API_HASH заданы", "возьми их на my.telegram.org")
    bot_ok = bool(cfg.bot_token and re.fullmatch(r"\d+:[\w-]{30,}", cfg.bot_token))
    rep.line(bot_ok, "BOT_TOKEN задан", "токен от @BotFather, вида 123456789:AAE…, без кавычек и пробелов")
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    rep.line(key.startswith("sk-ant-"), "ANTHROPIC_API_KEY задан", "ключ с console.anthropic.com → API Keys")
    try:
        proxy_settings(cfg.proxy)
        rep.info(f"Прокси для Telegram: {cfg.proxy or 'не используется'}")
    except NeedsUser as e:
        rep.line(False, "Прокси для Telegram", str(e))

    print("\nСвязь:")
    try:
        with InstanceLock(cfg.data_dir):
            owner = await check_telegram(cfg, rep)
            if bot_ok:
                await check_bot(cfg, rep, owner)
    except NeedsUser:
        where = "служба ассистента работает" if on_server() else "ассистент сейчас запущен в другом окне"
        rep.info(f"{where.capitalize()} — проверку Telegram и бота пропускаю "
                 "(они заняты им; раз он работает, связь есть)")
    if key:
        await check_claude(cfg, rep)
    await check_sheets(cfg, rep)

    print("\nДанные:")
    check_archive(cfg, rep)
    check_disk(cfg, rep)

    print()
    if rep.problems:
        print(f"Нашлось проблем: {len(rep.problems)}. Исправь отмеченные ❌ и запусти проверку снова.")
    else:
        print(f"Всё в порядке ✅ Запуск: {how_to('start')}")
    return not rep.problems
