"""Командная строка: python -m assistant <команда>."""

from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import sys
from logging.handlers import RotatingFileHandler

from .config import ROOT, load_config


def setup_logging(data_dir, verbose: bool) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = RotatingFileHandler(data_dir / "assistant.log", maxBytes=5_000_000, backupCount=3,
                                       encoding="utf-8")
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console)
    for noisy in ("telethon", "httpx", "httpx2", "httpcore", "anthropic", "urllib3", "google"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ---------------------------------------------------------------------- команды
def cmd_init(_args) -> None:
    created = []
    for src, dst in (("config.example.yaml", "config.yaml"), (".env.example", ".env")):
        if not (ROOT / dst).exists():
            shutil.copy(ROOT / src, ROOT / dst)
            created.append(dst)
    (ROOT / "credentials").mkdir(exist_ok=True)
    (ROOT / "data").mkdir(exist_ok=True)
    if created:
        print("Созданы файлы: " + ", ".join(created) + ". Заполни их по инструкции из README.md")
    else:
        print("config.yaml и .env уже есть.")


async def cmd_login(cfg, _args) -> None:
    from .db import DB
    from .telegram_archive import login
    await login(cfg, DB(cfg.data_dir / "assistant.db"))


async def cmd_chats(cfg, args) -> None:
    from .db import DB
    from .telegram_archive import FORBIDDEN_REASON, ChatFilter, chat_kind, connect_user, is_forbidden

    db = DB(cfg.data_dir / "assistant.db")
    archived = {r["chat_id"]: r["n"] for r in db.chats()}
    chat_filter = ChatFilter(cfg, db)
    client = await connect_user(cfg, db)
    needle = (args.search or "").lower()
    taken = skipped = 0
    print(f"{'ID':>16}  {'тип':<9} {'в архиве':>9}  название  →  решение")
    async for d in client.iter_dialogs(limit=args.limit):
        if needle and needle not in (d.name or "").lower():
            continue
        uname = getattr(d.entity, "username", None)
        n = archived.get(d.id)
        if cfg.all_chats:
            reason = (FORBIDDEN_REASON if is_forbidden(d.entity)
                      else chat_filter.reason_excluded(d.entity, d.name, db.is_explicit(d.id)))
            verdict = "✓ берём" if reason is None else f"✗ {reason}"
            taken += reason is None
            skipped += reason is not None
        else:
            verdict = "✓ в архиве" if n is not None else ""
        print(f"{d.id:>16}  {chat_kind(d.entity):<9} {(str(n) if n is not None else '-'):>9}  "
              f"{d.name}{f'  @{uname}' if uname else ''}  →  {verdict}")
    await client.disconnect()
    if cfg.all_chats:
        print(f"\nРежим «все чаты»: берём {taken}, пропускаем {skipped}. "
              "Настройки: include_channels / include_bots / exclude_chats в config.yaml.")
    else:
        print("\nВыбраны отдельные чаты (telegram.chats). Чтобы брать все — напиши chats: all")


async def cmd_download(cfg, args) -> None:
    from telethon import utils

    from .db import DB
    from .telegram_archive import (
        FORBIDDEN_REASON,
        ChatFilter,
        all_dialog_entities,
        chat_kind,
        connect_user,
        display_name,
        download_chat,
        is_forbidden,
        resolve_chats,
    )
    from .util import from_db, parse_local

    try:
        since = parse_local(args.since, cfg.tz) if args.since else None
    except ValueError:
        raise SystemExit(f"Неверная дата --since «{args.since}». Нужно ГГГГ-ММ-ДД, например 2026-09-01") from None
    db = DB(cfg.data_dir / "assistant.db")
    chat_filter = ChatFilter(cfg, db)
    chat_filter.apply_to_db(db)
    last_online = db.get_kv("last_online")
    online_since = from_db(last_online) if last_online else None
    client = await connect_user(cfg, db)
    skipped: dict[str, int] = {}
    explicit_ids: set[int] = set()  # выбраны явно — потом не отключаются флагами include_*
    if args.chat:
        entities, missing = await resolve_chats(client, args.chat)
        for m in missing:
            print(f"⚠️ Чат «{m}» не найден (посмотри точное название/ID: assistant chats)")
        kept = []
        for e in entities:
            reason = FORBIDDEN_REASON if is_forbidden(e) else chat_filter.reason_excluded(e, explicit=True)
            if reason:
                print(f"⚠️ «{display_name(e)}» пропущен: {reason}")
            else:
                kept.append(e)
        entities = kept
        explicit_ids = {utils.get_peer_id(e) for e in entities}
    elif args.all or cfg.all_chats:
        entities, skipped = await all_dialog_entities(client, chat_filter, db)
        if args.include_channels:
            # каналы, явно запрошенные флагом, запоминаем — они останутся в архиве и дальше
            async for d in client.iter_dialogs():
                if chat_kind(d.entity) == "канал" and chat_filter.allows(d.entity, d.name, explicit=True):
                    if all(utils.get_peer_id(e) != d.id for e in entities):
                        entities.append(d.entity)
                    explicit_ids.add(d.id)
            skipped = {k: v for k, v in skipped.items() if not k.startswith("канал")}
    else:
        entities, missing = await resolve_chats(client, cfg.chats)
        for m in missing:
            print(f"⚠️ Чат «{m}» не найден (посмотри точное название/ID: assistant chats)")
        kept = []
        for e in entities:
            reason = FORBIDDEN_REASON if is_forbidden(e) else chat_filter.reason_excluded(e, explicit=True)
            if reason:
                print(f"⚠️ «{display_name(e)}» пропущен: {reason}")
            else:
                kept.append(e)
        entities = kept
        explicit_ids = {utils.get_peer_id(e) for e in entities}

    media = args.media or cfg.download_media
    media_dir = cfg.data_dir / "media" if media else None
    total = 0
    print(f"Чатов к скачиванию: {len(entities)}. Telegram — только чтение, ничего не отправляется "
          "и не отмечается прочитанным.")
    for reason, count in sorted(skipped.items(), key=lambda x: -x[1]):
        print(f"  пропущено {count}: {reason}")
    print("Можно прервать (Ctrl+C) и запустить снова — продолжит с места остановки.\n")
    for i, ent in enumerate(entities, 1):
        name = display_name(ent)
        print(f"[{i}/{len(entities)}] {name}", flush=True)
        try:
            n = await download_chat(client, db, ent, since=since, media_dir=media_dir,
                                    explicit=utils.get_peer_id(ent) in explicit_ids, online_since=online_since,
                                    media_max_bytes=cfg.media_max_mb * 1024 * 1024)
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️ ошибка: {e}")
            continue
        total += n
        if n:
            print(f"  ✓ +{n} сообщений")
    await client.disconnect()
    st = db.message_stats()
    print(f"\nГотово. Новых сообщений: {total}. Всего в архиве: {st['n']} в {len(db.chats())} чатах.")
    print("Дальше: assistant learn — ассистент изучит историю (покажет стоимость и спросит).")


async def cmd_sheets(cfg, _args) -> None:
    from .sheets import Sheets, format_table

    sheets = Sheets(cfg)
    if not cfg.google_credentials.exists():
        print(f"Нет файла ключа Google: {cfg.google_credentials}\nСм. раздел «Google Таблицы» в README.md")
        return
    email = sheets.service_email() if cfg.google_auth != "oauth" else None
    if email:
        print(f"Сервисный аккаунт: {email}\n(каждую таблицу нужно открыть для этого адреса: «Настройки доступа» → "
              f"добавить как «Читатель»)\n")
    if not cfg.sheets:
        print("В config.yaml нет таблиц (google.sheets).")
        return
    for source in cfg.sheets:
        print(f"=== {source.name} ===")
        try:
            titles = await sheets.aworksheet_titles(source)
            print("Листы: " + ", ".join(titles))
            title, values = await sheets.aread(source, titles[0])
            print(f"Первые строки листа «{title}»:")
            print(format_table(values, max_rows=8))
        except Exception as e:  # noqa: BLE001
            text = str(e)
            if "403" in text or "PERMISSION" in text.upper() or "SpreadsheetNotFound" in type(e).__name__:
                print(f"⚠️ Нет доступа. Открой таблицу → «Настройки доступа» → добавь {email or 'свой аккаунт'}.")
            else:
                print(f"⚠️ Ошибка: {type(e).__name__}: {e}")
        print()


async def cmd_learn(cfg, args) -> None:
    from .brain import Assistant
    from .learn import run_learn

    await run_learn(Assistant(cfg), chats=args.chat, reset=args.reset, yes=args.yes,
                    sheets_only=args.sheets_only, model=args.model)


async def cmd_report(cfg, _args) -> None:
    from .brain import Assistant

    asst = Assistant(cfg)
    print("Готовлю отчёт…", flush=True)
    print("\n" + await asst.daily_report())


async def cmd_chat(cfg, _args) -> None:
    from .brain import Assistant
    from .llm import LLMError

    asst = Assistant(cfg)
    print("Чат с ассистентом. Выход — пустая строка или «выход».\n")
    while True:
        try:
            text = await asyncio.to_thread(input, "Ты: ")
        except (EOFError, KeyboardInterrupt):
            break
        if not text.strip() or text.strip().lower() in ("выход", "exit", "quit"):
            break
        try:
            answer = await asst.chat(text.strip())
        except LLMError as e:
            answer = f"⚠️ {e}"
        print(f"\nАссистент: {answer}\n")


async def cmd_run(cfg, _args) -> None:
    from .bot import run_app
    await run_app(cfg)


async def cmd_status(cfg, _args) -> None:
    from .brain import Assistant

    asst = Assistant(cfg)
    print(asst.status_text())
    print()
    print(asst.usage_text().replace("**", ""))


# ---------------------------------------------------------------------- разбор аргументов
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="assistant", description="ИИ-ассистент: Telegram + Google Таблицы + Claude")
    p.add_argument("-v", "--verbose", action="store_true", help="подробный вывод")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="создать config.yaml и .env из примеров")
    sub.add_parser("login", help="войти в свой Telegram-аккаунт (один раз)")

    s = sub.add_parser("chats", help="показать список чатов Telegram")
    s.add_argument("--search", help="фильтр по названию")
    s.add_argument("--limit", type=int, default=None, help="сколько чатов показать")

    s = sub.add_parser("download", help="скачать историю чатов в локальный архив")
    s.add_argument("--chat", action="append", help="только этот чат (ID, @username или название); можно повторять")
    s.add_argument("--all", action="store_true", help="все чаты (как chats: all в config.yaml)")
    s.add_argument("--include-channels", action="store_true",
                   help="ещё и каналы (запоминаются; чтобы брать каналы всегда — include_channels: true)")
    s.add_argument("--since", help="только начиная с даты YYYY-MM-DD (для первой выгрузки)")
    s.add_argument("--media", action="store_true", help="скачивать фото и файлы")

    sub.add_parser("sheets", help="проверить доступ к Google-таблицам")

    s = sub.add_parser("learn", help="изучить историю и таблицы → память и профиль работы")
    s.add_argument("--chat", action="append", help="изучить только этот чат")
    s.add_argument("--reset", action="store_true", help="изучить всю историю заново")
    s.add_argument("--yes", "-y", action="store_true", help="не спрашивать подтверждение стоимости")
    s.add_argument("--sheets-only", action="store_true", help="только разобрать таблицы и обновить профиль")
    s.add_argument("--model", help="модель Claude для изучения (по умолчанию из config.yaml)")

    sub.add_parser("report", help="сделать ежедневный отчёт сейчас (в консоль)")
    sub.add_parser("chat", help="поговорить с ассистентом в консоли")
    sub.add_parser("run", help="основной режим: бот, отчёты, напоминания, наблюдение")
    sub.add_parser("status", help="состояние архива, памяти и расходов")
    return p


COMMANDS = {
    "login": cmd_login, "chats": cmd_chats, "download": cmd_download, "sheets": cmd_sheets,
    "learn": cmd_learn, "report": cmd_report, "chat": cmd_chat, "run": cmd_run, "status": cmd_status,
}


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.command == "init":
        cmd_init(args)
        return
    if not (ROOT / "config.yaml").exists():
        print("Нет config.yaml — сначала выполни: assistant init")
        sys.exit(1)
    cfg = load_config()
    setup_logging(cfg.data_dir, args.verbose)
    from .llm import LLMError

    try:
        asyncio.run(COMMANDS[args.command](cfg, args))
    except KeyboardInterrupt:
        print("\nОстановлено.")
    except LLMError as e:
        print(f"⚠️ {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
