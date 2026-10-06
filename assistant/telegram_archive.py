"""Telegram (аккаунт владельца через Telethon): вход, выбор чатов, выгрузка истории, живое прослушивание.

Аккаунт владельца используется ТОЛЬКО НА ЧТЕНИЕ: клиент технически не может отправлять, редактировать,
удалять, пересылать сообщения или отмечать их прочитанными (см. ReadOnlyClient).
"""

from __future__ import annotations

import asyncio
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

# Служебный чат «Telegram» (коды входа) и @BotFather (токены ботов) — никогда не архивируем
TELEGRAM_SERVICE_ID = 777000
ALWAYS_EXCLUDED_USERNAMES = {"botfather"}

# Разрешённые запросы к Telegram: только чтение. Всё остальное (Send*, Edit*, Delete*, Forward*,
# Read* — «прочитано», Set* — «печатает», Update* — «в сети» и т.д.) блокируется до отправки в сеть.
READ_ONLY_PREFIXES = (
    "Get", "Search", "Resolve", "Check", "Ping", "InvokeWith", "InitConnection",
    "ExportAuthorization", "ImportAuthorization", "ReuploadCdnFile",
)


class ReadOnlyViolation(PermissionError):
    """Попытка что-то изменить в Telegram от имени владельца — запрещено."""


def _assert_read_only(request) -> None:
    requests = list(request) if utils.is_list_like(request) else [request]
    for r in requests:
        name = type(r).__name__
        if not name.startswith(READ_ONLY_PREFIXES):
            raise ReadOnlyViolation(
                f"Запрос {name} заблокирован: ассистент работает с твоим Telegram только на чтение"
            )
        inner = getattr(r, "query", None)  # InvokeWithLayer / InitConnection оборачивают другой запрос
        if inner is not None and not isinstance(inner, (str, bytes)):
            _assert_read_only(inner)


class ReadOnlyClient(TelegramClient):
    """Telethon-клиент, который физически не может ничего написать или изменить от имени владельца."""

    async def _call(self, sender, request, ordered=False, flood_sleep_threshold=None):
        _assert_read_only(request)
        return await super()._call(sender, request, ordered=ordered, flood_sleep_threshold=flood_sleep_threshold)


def make_user_client(cfg: Config, read_only: bool = True) -> TelegramClient:
    if not cfg.api_id or not cfg.api_hash:
        raise SystemExit(
            "Не заданы TG_API_ID и TG_API_HASH в файле .env.\n"
            "Получи их на https://my.telegram.org → API development tools."
        )
    cls = ReadOnlyClient if read_only else TelegramClient
    client = cls(
        str(cfg.data_dir / "user"),
        cfg.api_id,
        cfg.api_hash,
        device_model="TG AI Assistant (read-only)",
        system_version="local",
        app_version="0.2",
    )
    # при выгрузке длинной истории Telegram иногда просит подождать — ждём сами, не падаем
    client.flood_sleep_threshold = 24 * 3600
    return client


def remember_owner(db: DB, me) -> None:
    db.set_kv("owner_tg_id", str(me.id))
    db.set_kv("owner_name", display_name(me) or "")


async def connect_user(cfg: Config, db: DB | None = None) -> TelegramClient:
    client = make_user_client(cfg)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise SystemExit("Telegram-аккаунт не подключён. Сначала выполни: assistant login")
    if db is not None:
        remember_owner(db, await client.get_me())
    return client


async def login(cfg: Config, db: DB) -> None:
    # Вход — единственный момент, когда нужны «пишущие» запросы (отправка кода подтверждения)
    client = make_user_client(cfg, read_only=False)
    if cfg.phone:
        await client.start(phone=cfg.phone)
    else:
        await client.start()
    me = await client.get_me()
    remember_owner(db, me)
    print(f"\n✅ Вход выполнен: {utils.get_display_name(me)} (id {me.id})")
    print("Дальше ассистент работает с аккаунтом только на чтение — ничего не пишет от твоего имени.")
    print("Сессия сохранена в data/user.session — никому не передавай этот файл!")
    await client.disconnect()


# ---------------------------------------------------------------------- описание чатов и сообщений
def chat_kind(entity) -> str:
    if isinstance(entity, User):
        if entity.is_self:
            return "избранное"
        return "бот" if entity.bot else "личный"
    if isinstance(entity, Chat):
        return "группа"
    if isinstance(entity, Channel):
        return "группа" if entity.megagroup else "канал"
    return "?"


def display_name(entity) -> str | None:
    if entity is None:
        return None
    if isinstance(entity, User) and entity.is_self:
        return "Избранное (мои заметки)"
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
    sender_name = utils.get_display_name(sender) if sender is not None else None
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
        "outgoing": 1 if msg.out else 0,
    }


# ---------------------------------------------------------------------- какие чаты брать
class ChatFilter:
    """Решает, какие чаты архивировать и анализировать."""

    def __init__(self, cfg: Config, db: DB | None = None):
        self.cfg = cfg
        self.excluded_ids: set[int] = set()
        self.excluded_names: set[str] = set()
        self.excluded_usernames: set[str] = set(ALWAYS_EXCLUDED_USERNAMES)
        if cfg.bot_id:
            self.excluded_ids.add(cfg.bot_id)
        for spec in cfg.exclude_chats:
            text = str(spec).strip()
            if text.lstrip("-").isdigit():
                self.excluded_ids.add(int(text))
            elif text.startswith("@"):
                self.excluded_usernames.add(text[1:].lower())
            elif text:
                self.excluded_names.add(text.lower())
        if db is not None:
            # исключения по названию переводим в id для уже известных чатов
            for chat_id, title in db.chat_titles().items():
                if (title or "").lower() in self.excluded_names:
                    self.excluded_ids.add(chat_id)

    def reason_excluded(self, entity, name: str | None = None) -> str | None:
        """None — чат подходит; иначе — причина, почему он пропущен."""
        peer_id = utils.get_peer_id(entity)
        raw_id = getattr(entity, "id", None)
        username = (getattr(entity, "username", None) or "").lower()
        title = (name or display_name(entity) or "").lower()
        if raw_id == TELEGRAM_SERVICE_ID or username in ALWAYS_EXCLUDED_USERNAMES:
            return "служебный (коды входа/токены) — не архивируется никогда"
        if peer_id in self.excluded_ids or raw_id in self.excluded_ids:
            return "в списке exclude_chats" if raw_id != self.cfg.bot_id else "это бот ассистента"
        if username and username in self.excluded_usernames:
            return "в списке exclude_chats"
        if title and title in self.excluded_names:
            return "в списке exclude_chats"
        if isinstance(entity, User):
            if entity.is_self:
                return None if self.cfg.include_saved else "Избранное выключено (include_saved_messages)"
            if entity.bot:
                return None if self.cfg.include_bots else "бот (include_bots: false)"
        if isinstance(entity, Channel) and not entity.megagroup:
            return None if self.cfg.include_channels else "канал (include_channels: false)"
        return None

    def allows(self, entity, name: str | None = None) -> bool:
        return self.reason_excluded(entity, name) is None

    def apply_to_db(self, db: DB) -> int:
        """Снимает с отслеживания уже скачанные чаты, которые теперь исключены настройками."""
        kinds_off = set()
        if not self.cfg.include_channels:
            kinds_off.add("канал")
        if not self.cfg.include_bots:
            kinds_off.add("бот")
        if not self.cfg.include_saved:
            kinds_off.add("избранное")
        changed = 0
        for c in db.chats():
            if not c["monitored"]:
                continue
            raw = abs(c["chat_id"])
            if (
                c["chat_id"] in self.excluded_ids or raw == TELEGRAM_SERVICE_ID
                or (c["username"] or "").lower() in self.excluded_usernames
                or (c["title"] or "").lower() in self.excluded_names
                or c["kind"] in kinds_off
            ):
                db.set_monitored(c["chat_id"], False)
                changed += 1
        return changed


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


async def all_dialog_entities(client: TelegramClient, chat_filter: ChatFilter) -> tuple[list, dict[str, int]]:
    """Все подходящие чаты аккаунта (включая архивную папку) + статистика пропущенных по причинам."""
    result, skipped = [], {}
    async for d in client.iter_dialogs():
        reason = chat_filter.reason_excluded(d.entity, d.name)
        if reason:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        result.append(d.entity)
    return result, skipped


# ---------------------------------------------------------------------- выгрузка
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
    db.set_monitored(chat_id, True)

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


def _media_args(cfg: Config) -> dict:
    return {
        "media_dir": cfg.data_dir / "media" if cfg.download_media else None,
        "media_max_bytes": cfg.media_max_mb * 1024 * 1024,
    }


async def sync_chats(client: TelegramClient, db: DB, cfg: Config, chat_filter: ChatFilter,
                     busy: set[int] | None = None) -> int:
    """Докачивает всё новое (например, после выключения компьютера).

    В режиме «все чаты» заодно находит новые чаты и скачивает их историю целиком.
    """
    total = 0
    targets: dict[int, object] = {}
    if cfg.all_chats:
        async for d in client.iter_dialogs():
            if not chat_filter.allows(d.entity, d.name):
                if d.id in db.monitored_chat_ids():
                    db.set_monitored(d.id, False)  # чат добавили в исключения
                continue
            top = d.message.id if d.message else 0
            if top and top <= db.last_msg_id(d.id):
                continue  # ничего нового
            targets[d.id] = d.entity
    else:
        await client.get_dialogs()  # прогреваем кеш, чтобы находить чаты по id
        entities, _ = await resolve_chats(client, cfg.chats) if cfg.chats else ([], [])
        for ent in entities:
            if chat_filter.allows(ent):
                targets[utils.get_peer_id(ent)] = ent
        for chat_id in db.monitored_chat_ids():
            if chat_id in targets:
                continue
            try:
                entity = await client.get_entity(chat_id)
            except Exception as e:  # noqa: BLE001
                log.warning("Чат %s недоступен: %s", chat_id, e)
                continue
            if chat_filter.allows(entity):
                targets[chat_id] = entity
            else:
                db.set_monitored(chat_id, False)

    busy = busy if busy is not None else set()
    for chat_id, entity in targets.items():
        if chat_id in busy:
            continue  # этот чат уже скачивается
        is_new = db.last_msg_id(chat_id) == 0
        busy.add(chat_id)
        try:
            n = await download_chat(client, db, entity, progress=lambda s: log.info(s), **_media_args(cfg))
        except Exception as e:  # noqa: BLE001
            log.warning("Не удалось обновить чат %s: %s", chat_id, e)
            continue
        finally:
            busy.discard(chat_id)
        if n:
            log.info("%s «%s»: +%s сообщений", "Новый чат" if is_new else "Докачано", display_name(entity), n)
        total += n
    return total


# ---------------------------------------------------------------------- живое прослушивание
def attach_live_listener(client: TelegramClient, db: DB, cfg: Config, chat_filter: ChatFilter,
                         busy: set[int] | None = None) -> None:
    """Сохраняет новые/изменённые сообщения в реальном времени, отмечает удалённые.

    В режиме «все чаты» новый чат (кто-то впервые написал, добавили в группу) подхватывается
    автоматически: сразу скачивается вся его история.
    """
    downloading = busy if busy is not None else set()

    async def _download_new_chat(chat_id: int, entity) -> None:
        try:
            n = await download_chat(client, db, entity, progress=lambda s: log.info(s), **_media_args(cfg))
            # ещё один проход — на случай сообщений, пришедших во время выгрузки
            n += await download_chat(client, db, entity, progress=lambda s: log.info(s), **_media_args(cfg))
            log.info("Новый чат «%s» добавлен в архив: %s сообщений", display_name(entity), n)
        except Exception:  # noqa: BLE001
            log.exception("Не удалось скачать новый чат %s", chat_id)
        finally:
            downloading.discard(chat_id)

    async def _store(event) -> None:
        chat_id = event.chat_id
        msg = event.message
        if chat_id is None or isinstance(msg, MessageService):
            return
        if chat_id not in db.monitored_chat_ids():
            if not cfg.all_chats or chat_id in downloading:
                return
            try:
                entity = await event.get_chat()
            except Exception:  # noqa: BLE001
                return
            if entity is None or not chat_filter.allows(entity):
                return
            downloading.add(chat_id)
            asyncio.create_task(_download_new_chat(chat_id, entity))
            return
        try:
            row = await message_to_row(msg, chat_id, fetch_sender=True, **_media_args(cfg))
            db.upsert_messages([row])
        except Exception:  # noqa: BLE001
            log.exception("Не удалось сохранить сообщение %s/%s", chat_id, getattr(msg, "id", "?"))

    async def _deleted(event) -> None:
        try:
            db.mark_deleted(event.chat_id, list(event.deleted_ids or []))
        except Exception:  # noqa: BLE001
            log.exception("Не удалось отметить удалённые сообщения")

    client.add_event_handler(_store, events.NewMessage())
    client.add_event_handler(_store, events.MessageEdited())
    client.add_event_handler(_deleted, events.MessageDeleted())
