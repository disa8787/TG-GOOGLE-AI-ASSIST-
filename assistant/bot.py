"""Основной режим: слушаем рабочие чаты, общаемся с владельцем через бота, работают отчёты и напоминания."""

from __future__ import annotations

import asyncio
import base64
import logging

from telethon import TelegramClient, events

from .brain import Assistant
from .config import Config
from .jobs import Scheduler
from .llm import LLMError
from .telegram_archive import (
    ChatFilter,
    attach_live_listener,
    connect_user,
    forward_name,
    sync_chats,
)
from .util import fmt_local, split_text, utcnow

log = logging.getLogger(__name__)

HELP = """\
Я — твой «второй я»: знаю всё из твоего Telegram, таблиц и памяти и ничего не забываю. \
От твоего имени я ничего не пишу — твой Telegram для меня только на чтение.

Пиши вопросы обычным текстом, например:
• Где сейчас Иван и когда он выгружается?
• Кто освобождается завтра и где?
• Что я обещал Марии на прошлой неделе?
• Кто ждёт от меня ответа?
• Напомни в 15:00 запросить апдейт у Сергея
• Набросай ответ брокеру по грузу 12345 — как бы написал я
• Запомни: у Петра трак 512, он не ездит в Нью-Йорк

Можно присылать фото и PDF (рейт-коны, BOL, скриншоты) и пересылать сообщения из чатов.

Команды:
/report — сделать отчёт прямо сейчас
/check — проверить новые сообщения и таблицы сейчас
/reminders — активные напоминания
/memory [текст] — что я помню (или поиск по памяти)
/new — начать новую тему (контекст разговора сбросится, память останется)
/sync — докачать новые сообщения из чатов
/status — состояние ассистента
/usage — расходы на Claude"""

IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


async def send(client: TelegramClient, to, text: str) -> None:
    for part in split_text(text) or ["(пусто)"]:
        try:
            await client.send_message(to, part, parse_mode="md", link_preview=False)
        except Exception:  # noqa: BLE001 — если разметка не понравилась Telegram, шлём простым текстом
            await client.send_message(to, part, parse_mode=None, link_preview=False)


async def read_attachments(bot: TelegramClient, msg) -> tuple[list[dict], list[str]]:
    """Фото и PDF от владельца → блоки для Claude. Возвращает (блоки, текстовые пометки)."""
    blocks, notes = [], []
    if msg.photo:
        data = await bot.download_media(msg, file=bytes)
        blocks.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/jpeg", "data": base64.standard_b64encode(data).decode()}})
        notes.append("[фото]")
    elif msg.document and not msg.sticker:
        mime = (msg.file.mime_type or "").lower()
        size = msg.file.size or 0
        name = msg.file.name or "файл"
        if mime in IMAGE_TYPES and size <= 5 * 1024 * 1024:
            data = await bot.download_media(msg, file=bytes)
            blocks.append({"type": "image", "source": {
                "type": "base64", "media_type": mime, "data": base64.standard_b64encode(data).decode()}})
            notes.append(f"[изображение {name}]")
        elif mime == "application/pdf" and size <= 25 * 1024 * 1024:
            data = await bot.download_media(msg, file=bytes)
            blocks.append({"type": "document", "source": {
                "type": "base64", "media_type": "application/pdf", "data": base64.standard_b64encode(data).decode()}})
            notes.append(f"[PDF {name}]")
        elif (mime.startswith("text/") or name.lower().endswith((".txt", ".csv"))) and size <= 1024 * 1024:
            data = await bot.download_media(msg, file=bytes)
            notes.append(f"[файл {name}]:\n{data.decode('utf-8', errors='replace')}")
        else:
            notes.append(f"[файл {name} — такой тип я пока не умею читать]")
    elif msg.voice or msg.video_note:
        notes.append("[голосовое сообщение — слушать пока не умею]")
    return blocks, notes


def register_bot_handlers(bot: TelegramClient, user: TelegramClient, asst: Assistant, sched: Scheduler,
                          owner_id: int, chat_filter: ChatFilter, busy: set[int]) -> None:
    cfg = asst.cfg

    async def run_with_typing(chat_id, coro):
        async with bot.action(chat_id, "typing"):
            return await coro

    @bot.on(events.NewMessage(incoming=True))
    async def on_message(event) -> None:
        if not event.is_private:
            return
        if event.sender_id != owner_id:
            await event.reply("Это личный ассистент, он отвечает только своему владельцу.")
            return
        msg = event.message
        text = (msg.message or "").strip()
        cmd = text.split()[0].lower().split("@")[0] if text.startswith("/") else ""
        arg = text.split(maxsplit=1)[1] if cmd and len(text.split(maxsplit=1)) > 1 else ""
        chat_id = event.chat_id
        try:
            if cmd in ("/start", "/help"):
                await send(bot, chat_id, HELP)
            elif cmd == "/report":
                await send(bot, chat_id, "Готовлю отчёт, это займёт пару минут…")
                answer = await run_with_typing(chat_id, asst.daily_report())
                await send(bot, chat_id, "📋 **Отчёт**\n\n" + answer)
            elif cmd == "/check":
                note = await run_with_typing(chat_id, asst.watch_check(sched.ignored))
                await send(bot, chat_id, note or "Ничего важного с прошлой проверки ✅")
            elif cmd == "/reminders":
                await send(bot, chat_id, asst.tools.t_list_reminders())
            elif cmd == "/memory":
                rows = asst.db.search_memory(arg) if arg else sorted(
                    asst.db.all_memory(), key=lambda r: r["updated_at"], reverse=True)[:30]
                if not rows:
                    await send(bot, chat_id, "Ничего не найдено.")
                else:
                    head = f"В памяти {asst.db.memory_count()} заметок." + ("" if arg else " Последние обновлённые:")
                    body = "\n\n".join(
                        f"#{r['id']} **{r['subject']}** ({r['category']}, {fmt_local(r['updated_at'], cfg.tz)[:10]})\n"
                        f"{r['content']}" for r in rows)
                    await send(bot, chat_id, f"{head}\n\n{body}")
            elif cmd == "/new":
                asst.reset_dialog()
                await send(bot, chat_id, "Ок, начинаем с чистого листа. Память и архив на месте.")
            elif cmd == "/sync":
                n = await run_with_typing(chat_id, sync_chats(user, asst.db, cfg, chat_filter, busy))
                await send(bot, chat_id, f"Готово, новых сообщений: {n}")
            elif cmd == "/status":
                await send(bot, chat_id, asst.status_text())
            elif cmd == "/usage":
                await send(bot, chat_id, asst.usage_text())
            else:
                blocks, notes = await read_attachments(bot, msg)
                if msg.fwd_from:
                    notes.insert(0, f"(переслано от {forward_name(msg)})")
                full = "\n".join(notes + ([text] if text else []))
                if not full and not blocks:
                    return
                answer = await run_with_typing(chat_id, asst.chat(full or "(вложение)", blocks))
                await send(bot, chat_id, answer)
        except LLMError as e:
            await send(bot, chat_id, f"⚠️ {e}")
        except Exception as e:  # noqa: BLE001
            log.exception("Ошибка обработки сообщения")
            await send(bot, chat_id, f"⚠️ Что-то пошло не так: {type(e).__name__}: {e}")


def _append_notification(cfg: Config, text: str) -> None:
    with open(cfg.data_dir / "notifications.log", "a", encoding="utf-8") as f:
        f.write(f"\n----- {fmt_local(utcnow(), cfg.tz)}\n{text}\n")


async def run_app(cfg: Config) -> None:
    asst = Assistant(cfg)
    user = await connect_user(cfg, asst.db)  # аккаунт владельца — только чтение
    me = await user.get_me()
    owner_id = cfg.owner_id or me.id
    chat_filter = ChatFilter(cfg, asst.db)
    chat_filter.apply_to_db(asst.db)
    busy: set[int] = set()  # чаты, история которых сейчас скачивается

    bot: TelegramClient | None = None
    if cfg.bot_token:
        bot = TelegramClient(str(cfg.data_dir / "bot"), cfg.api_id, cfg.api_hash)
        await bot.start(bot_token=cfg.bot_token)
    else:
        log.warning("BOT_TOKEN не задан — уведомления будут только в этом окне и в data/notifications.log. "
                    "От имени твоего аккаунта ассистент не пишет никогда.")

    async def notify(text: str) -> None:
        if bot is not None:
            await send(bot, owner_id, text)
            return
        print("\n" + text + "\n", flush=True)
        _append_notification(cfg, text)

    # слушаем сразу, чтобы не пропустить сообщения, пока докачивается история
    attach_live_listener(user, asst.db, cfg, chat_filter, busy)
    sched = Scheduler(asst, notify, set())
    if bot is not None:
        register_bot_handlers(bot, user, asst, sched, owner_id, chat_filter, busy)

    async def initial_sync() -> None:
        try:
            new = await sync_chats(user, asst.db, cfg, chat_filter, busy)
            log.info("Синхронизация завершена: новых сообщений %s", new)
            if new:
                await notify(f"🔄 Докачал пропущенное: {new} новых сообщений.")
        except Exception:  # noqa: BLE001
            log.exception("Ошибка синхронизации чатов")

    tasks = sched.tasks() + [asyncio.create_task(initial_sync(), name="initial-sync")]

    mode = "все чаты" if cfg.all_chats else f"{len(cfg.chats)} выбранных чатов"
    print(f"✅ Ассистент запущен ({mode}, Telegram — только чтение). Докачиваю пропущенное в фоне…")
    print("Пиши боту в Telegram. Для остановки закрой это окно или нажми Ctrl+C.")
    try:
        await notify("✅ Ассистент запущен. /help — что я умею.")
    except Exception:  # noqa: BLE001
        log.warning("Не удалось отправить приветствие. Владелец должен сначала нажать /start у бота.")

    waiters = [user.run_until_disconnected()]
    if bot is not None:
        waiters.append(bot.run_until_disconnected())
    try:
        await asyncio.gather(*waiters)
    finally:
        for t in tasks:
            t.cancel()
