"""Telegram (аккаунт пользователя через Telethon): вход, список чатов, выгрузка истории, живое прослушивание."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from telethon import TelegramClient, events, utils
from telethon.tl.custom.message import Message
from telethon.tl.types import Channel, Chat, MessageService, User

from .config import Config
from .db import DB
from .util import to_db

log = logging.getLogger(__name__)


def make_user_client(cfg: Config) -> TelegramClient:
    if not cfg.api_id or not cfg.api_hash:
        raise SystemExit(
            "Не заданы TG_API_ID и TG_API_HASH в файле .env.\n"
            "Получи их на https://my.telegram.org → API development tools."
        )
    client = TelegramClient(
        str(cfg.data_dir / "user"),
        cfg.api_id,
        cfg.api_hash,
        device_model="TG AI Assistant",
        system_version="local",
        app_version="0.1",
    )
    # при выгрузке длинной истории Telegram иногда просит подождать — ждём сами, не падаем
    client.flood_sleep_threshold = 24 * 3600
    return client


async def connect_user(cfg: Config) -> TelegramClient:
    client = make_user_client(cfg)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise SystemExit("Telegram-аккаунт не подключён. Сначала выполни: assistant login")
    return client


async def login(cfg: Config) -> None:
    client = make_user_client(cfg)
    if cfg.phone:
        await client.start(phone=cfg.phone)
    else:
        await client.start()
    me = await client.get_me()
    print(f"\n✅ Вход выполнен: {utils.get_display_name(me)} (id {me.id})")
    print("Сессия сохранена в data/user.session — никому не передавай этот файл!")
    await client.disconnect()


def chat_kind(entity) -> str:
    if isinstance(entity, User):
        return "бот" if entity.bot else "личный"
    if isinstance(entity, Chat):
        return "группа"
    if isinstance(entity, Channel):
        return "группа" if entity.megagroup else "канал"
    return "?"


def display_name(entity) -> str | None:
    if entity is None:
        return None
    name = utils.get_display_name(entity)
    return name or getattr(entity, "username", None) or str(getattr(entity, "id", ""))


def describe_media(msg: Message) -> str | None:
    """Короткое текстовое описание вложения — чтобы ассистент понимал, что было в сообщении."""
    try:
        if msg.photo:
            return "[фото]"
        if msg.voice:
            dur = getattr(msg.file, "duration", None)
            return f"[голосовое{f' {dur}с' if dur else ''}]"
        if msg.video_note:
            return "[видеосообщение]"
        if msg.sticker:
            emoji = getattr(msg.file, "emoji", None) or ""
            return f"[стикер {emoji}]".replace(" ]", "]")
        if msg.gif:
            return "[GIF]"
        if msg.video:
            return "[видео]"
        if msg.audio:
            return "[аудио]"
        if msg.venue:
            v = msg.venue
            return f"[место: {v.title}, {v.address}; {v.geo.lat:.5f},{v.geo.long:.5f}]"
        if msg.geo:
            return f"[геопозиция: {msg.geo.lat:.5f},{msg.geo.long:.5f}]"
        if msg.contact:
            c = msg.contact
            name = " ".join(x for x in (c.first_name, c.last_name) if x)
            return f"[контакт: {name} {c.phone_number or ''}]".strip()
        if msg.poll:
            q = msg.poll.poll.question
            return f"[опрос: {getattr(q, 'text', q)}]"
        if msg.document:
            name = getattr(msg.file, "name", None) or "без имени"
            return f"[файл: {name}]"
        if msg.media is not None and not msg.web_preview:
            return "[вложение]"
    except Exception:  # описание вложения не должно ломать выгрузку
        return "[вложение]"
    return None


def forward_name(msg: Message) -> str | None:
    fwd = msg.forward
    if fwd is None:
        return None
    try:
        ent = fwd.sender or fwd.chat
        if ent is not None:
            return display_name(ent)
    except Exception:
        pass
    return getattr(fwd.original_fwd, "from_name", None) or "неизвестно"


async def message_to_row(
    msg: Message,
    chat_id: int,
    *,
    fetch_sender: bool = False,
    media_dir: Path | None = None,
    media_max_bytes: int = 0,
) -> dict:
    sender = msg.sender
    if sender is None and fetch_sender:
        try:
            sender = await msg.get_sender()
        except Exception:
            sender = None
    sender_name = display_name(sender) if sender is not None else None
    if not sender_name and msg.post_author:
        sender_name = msg.post_author

    media = describe_media(msg)
    media_path = None
    if media_dir is not None and (msg.photo or msg.document) and not msg.sticker:
        size = getattr(msg.file, "size", 0) or 0
        if size <= media_max_bytes:
            try:
                target = media_dir / str(chat_id)
                target.mkdir(parents=True, exist_ok=True)
                saved = await msg.download_media(file=str(target / f"{msg.id}_"))
                if saved:
                    media_path = str(saved)
            except Exception as e:  # noqa: BLE001
                log.warning("Не удалось скачать вложение %s/%s: %s", chat_id, msg.id, e)

    return {
        "chat_id": chat_id,
        "msg_id": msg.id,
        "date": to_db(msg.date),
        "sender_id": msg.sender_id,
        "sender_name": sender_name,
        "text": msg.message or "",
        "reply_to": msg.reply_to_msg_id,
        "fwd_from": forward_name(msg),
        "media": media,
        "media_path": media_path,
        "edited_at": to_db(msg.edit_date) if msg.edit_date else None,
    }


async def resolve_chats(client: TelegramClient, specs: list):
    """Находит чаты по id / @username / части названия. Возвращает (entities, not_found)."""
    dialogs = [d async for d in client.iter_dialogs()]
    found, missing = {}, []
    for spec in specs:
        text = str(spec).strip()
        matches = []
        if text.lstrip("-").isdigit():
            num = int(text)
            matches = [d for d in dialogs if d.id == num or d.entity.id == abs(num)]
        elif text.startswith("@"):
            uname = text[1:].lower()
            matches = [d for d in dialogs if (getattr(d.entity, "username", None) or "").lower() == uname]
            if not matches:
                try:
                    ent = await client.get_entity(text)
                    found[utils.get_peer_id(ent)] = ent
                    continue
                except Exception:
                    matches = []
        else:
            needle = text.lower()
            exact = [d for d in dialogs if (d.name or "").lower() == needle]
            matches = exact or [d for d in dialogs if needle in (d.name or "").lower()]
        if not matches:
            missing.append(text)
        for d in matches:
            found[d.id] = d.entity
    return list(found.values()), missing


async def all_dialog_entities(client: TelegramClient, include_channels: bool, exclude_ids: set[int]):
    result = []
    async for d in client.iter_dialogs():
        if d.id in exclude_ids:
            continue
        ent = d.entity
        if isinstance(ent, User) and (ent.bot or ent.is_self):
            continue
        if isinstance(ent, Channel) and not ent.megagroup and not include_channels:
            continue
        result.append(ent)
    return result


async def download_chat(
    client: TelegramClient,
    db: DB,
    entity,
    *,
    since: datetime | None = None,
    media_dir: Path | None = None,
    media_max_bytes: int = 0,
    progress: Callable[[str], None] = print,
) -> int:
    """Скачивает историю чата в базу. Повторный запуск докачивает только новые сообщения."""
    chat_id = utils.get_peer_id(entity)
    title = display_name(entity) or str(chat_id)
    db.upsert_chat(chat_id, title, getattr(entity, "username", None), chat_kind(entity))

    last_id = db.last_msg_id(chat_id)
    kwargs: dict = {"reverse": True, "wait_time": 0.5}
    if last_id:
        kwargs["min_id"] = last_id
    elif since:
        kwargs["offset_date"] = since

    batch: list[dict] = []
    total = 0
    async for msg in client.iter_messages(entity, **kwargs):
        if isinstance(msg, MessageService):
            continue
        batch.append(await message_to_row(msg, chat_id, media_dir=media_dir, media_max_bytes=media_max_bytes))
        if len(batch) >= 500:
            total += db.upsert_messages(batch)
            batch.clear()
            progress(f"  {title}: {total} сообщений…")
    total += db.upsert_messages(batch)
    db.mark_synced(chat_id)
    return total


async def sync_monitored(client: TelegramClient, db: DB, cfg: Config) -> int:
    """Докачивает новые сообщения во всех отслеживаемых чатах (например, после выключения компьютера)."""
    await client.get_dialogs()  # прогреваем кеш, чтобы находить чаты по id
    total = 0
    media_dir = cfg.data_dir / "media" if cfg.download_media else None
    for chat_id in sorted(db.monitored_chat_ids()):
        try:
            entity = await client.get_entity(chat_id)
        except Exception as e:  # noqa: BLE001
            log.warning("Чат %s недоступен: %s", chat_id, e)
            continue
        n = await download_chat(
            client, db, entity, media_dir=media_dir,
            media_max_bytes=cfg.media_max_mb * 1024 * 1024, progress=lambda s: log.info(s),
        )
        if n:
            log.info("Докачано %s новых сообщений из «%s»", n, display_name(entity))
        total += n
    return total


def attach_live_listener(client: TelegramClient, db: DB, cfg: Config, ignored: set[int]) -> None:
    """Сохраняет новые и отредактированные сообщения отслеживаемых чатов в реальном времени."""
    media_dir = cfg.data_dir / "media" if cfg.download_media else None
    max_bytes = cfg.media_max_mb * 1024 * 1024

    async def _store(event) -> None:
        chat_id = event.chat_id
        if chat_id is None or chat_id in ignored or chat_id not in db.monitored_chat_ids():
            return
        msg = event.message
        if isinstance(msg, MessageService):
            return
        try:
            row = await message_to_row(
                msg, chat_id, fetch_sender=True, media_dir=media_dir, media_max_bytes=max_bytes
            )
            db.upsert_messages([row])
        except Exception:  # noqa: BLE001
            log.exception("Не удалось сохранить сообщение %s/%s", chat_id, getattr(msg, "id", "?"))

    client.add_event_handler(_store, events.NewMessage())
    client.add_event_handler(_store, events.MessageEdited())
