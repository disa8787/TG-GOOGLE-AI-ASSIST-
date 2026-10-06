"""Telegram (аккаунт владельца через Telethon): вход, выбор чатов, выгрузка истории, живое прослушивание.

Аккаунт владельца используется ТОЛЬКО НА ЧТЕНИЕ: клиент технически не может отправлять, редактировать,
удалять, пересылать сообщения или отмечать их прочитанными (см. ReadOnlyClient).
"""

from __future__ import annotations

import asyncio
import getpass
import logging
import os
import sys
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from telethon import TelegramClient, events, utils
from telethon.tl.custom.message import Message
from telethon.tl.types import (
    Channel,
    ChannelForbidden,
    Chat,
    ChatForbidden,
    MessageService,
    User,
)

from .config import Config
from .db import DB
from .util import from_db, to_db, utcnow

log = logging.getLogger(__name__)

# Служебный чат «Telegram» (коды входа) и @BotFather (токены ботов) — никогда не архивируем
TELEGRAM_SERVICE_ID = 777000
ALWAYS_EXCLUDED_USERNAMES = {"botfather"}

# Разрешённые запросы к Telegram — только чтение. Всё остальное (Send*, Edit*, Delete*, Forward*,
# Read* — «прочитано», Set* — «печатает», Update* — «в сети», платежи, звонки, истории, настройки
# аккаунта и т.д.) блокируется до отправки в сеть.
READ_ONLY_PREFIXES = ("Get", "Search", "Resolve", "Check")
READ_ONLY_NAMESPACES = {"messages", "channels", "users", "contacts", "updates", "upload", "help", "photos"}
SERVICE_REQUESTS = {  # служебные запросы соединения и скачивания файлов с других дата-центров
    "InvokeWithLayerRequest", "InitConnectionRequest", "InvokeWithoutUpdatesRequest",
    "PingRequest", "PingDelayDisconnectRequest",
    "ExportAuthorizationRequest", "ImportAuthorizationRequest", "ReuploadCdnFileRequest",
}
# «Читающие» по названию запросы, у которых есть побочные эффекты — тоже запрещены
SIDE_EFFECT_REQUESTS = {
    "GetBotCallbackAnswerRequest",   # нажатие инлайн-кнопки от имени владельца
    "GetInlineBotResultsRequest",    # запрос к инлайн-боту (бот видит запрос)
    "GetLocatedRequest",             # может опубликовать геопозицию в «Люди рядом»
    "GetMessagesViewsRequest",       # может увеличивать счётчик просмотров
    "SearchPostsRequest",            # глобальный поиск постов может списывать звёзды
}
# Поля запросов, которые делают «чтение» действием: оплата звёздами, реферальный переход, +просмотр
SIDE_EFFECT_FIELDS = ("allow_paid_stars", "referer", "increment")


class ReadOnlyViolation(PermissionError):
    """Попытка что-то изменить в Telegram от имени владельца — запрещено."""


def _request_allowed(r) -> bool:
    name = type(r).__name__
    namespace = type(r).__module__.rsplit(".", 1)[-1]
    if name in SIDE_EFFECT_REQUESTS or any(getattr(r, f, None) for f in SIDE_EFFECT_FIELDS):
        return False
    if name in SERVICE_REQUESTS:
        return namespace in ("functions", "auth", "upload")
    return namespace in READ_ONLY_NAMESPACES and name.startswith(READ_ONLY_PREFIXES)


def _assert_read_only(request) -> None:
    requests = list(request) if utils.is_list_like(request) else [request]
    for r in requests:
        if not _request_allowed(r):
            raise ReadOnlyViolation(
                f"Запрос {type(r).__name__} заблокирован: ассистент работает с твоим Telegram только на чтение"
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
    db.set_kv("owner_name", utils.get_display_name(me) or getattr(me, "username", None) or str(me.id))
    # сообщения владельца, сохранённые прошлыми версиями без пометки «Я»
    if db.get_kv("outgoing_backfilled") != str(me.id):
        db.backfill_outgoing(me.id)
        db.set_kv("outgoing_backfilled", str(me.id))


def login_hint() -> str:
    if os.name == "nt":
        return ("Telegram-аккаунт не подключён. Открой папку программы, в адресной строке проводника "
                "напиши cmd и нажми Enter, затем выполни:\n    assistant.bat login")
    return "Telegram-аккаунт не подключён. Выполни:  ./assistant.sh login"


async def connect_user(cfg: Config, db: DB | None = None, interactive: bool = False) -> TelegramClient:
    """Подключает аккаунт владельца (только чтение). interactive=True — если не вошли, предложит войти."""
    client = make_user_client(cfg)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        if not (interactive and sys.stdin is not None and sys.stdin.isatty()):
            raise SystemExit(login_hint())
        print("Telegram-аккаунт ещё не подключён — давай войдём (это нужно один раз).\n")
        await login(cfg, db)
        client = make_user_client(cfg)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise SystemExit(login_hint())
    if db is not None:
        remember_owner(db, await client.get_me())
    return client


def _ask(prompt: str) -> str:
    return input(prompt).strip()


async def login(cfg: Config, db: DB | None) -> None:
    # Вход — единственный момент, когда нужны «пишущие» запросы (отправка кода подтверждения)
    client = make_user_client(cfg, read_only=False)
    print("Вход в твой Telegram (один раз). Код подтверждения придёт в приложение Telegram, в чат «Telegram».")
    await client.start(
        phone=cfg.phone or (lambda: _ask("Номер телефона в международном формате (например +79991234567): ")),
        code_callback=lambda: _ask("Код из Telegram: "),
        password=lambda: getpass.getpass(
            "Пароль двухэтапной проверки (символы не отображаются при вводе, это нормально): "),
    )
    me = await client.get_me()
    if db is not None:
        remember_owner(db, me)
    print(f"\n✅ Вход выполнен: {utils.get_display_name(me)} (id {me.id})")
    print("Дальше ассистент работает с аккаунтом только на чтение — ничего не пишет от твоего имени.")
    print("Сессия сохранена в data/user.session — никому не передавай этот файл!")
    await client.disconnect()


# ---------------------------------------------------------------------- описание чатов и сообщений
def chat_kind(entity) -> str:
    if isinstance(entity, (ChannelForbidden, ChatForbidden)):
        return "нет доступа"
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
SERVICE_REASON = "служебный (коды входа/токены) — не архивируется никогда"
FORBIDDEN_REASON = "нет доступа (тебя удалили/вышел) — скачанное остаётся в памяти"
KIND_FLAGS = {"канал": "include_channels", "бот": "include_bots", "избранное": "include_saved"}
FLAG_NAMES = {"include_channels": "include_channels", "include_bots": "include_bots",
              "include_saved": "include_saved_messages"}


def is_forbidden(entity) -> bool:
    """Чат, к которому больше нет доступа: скачать нельзя, но уже скачанное остаётся в архиве и памяти."""
    return isinstance(entity, (ChannelForbidden, ChatForbidden))


def raw_chat_id(chat_id: int) -> int:
    return utils.resolve_id(chat_id)[0]


class ChatFilter:
    """Решает, какие чаты архивировать и анализировать.

    Явно выбранные чаты (--chat, --include-channels, список в telegram.chats) берутся всегда, кроме служебных
    и exclude_chats; для остальных действуют и флаги include_channels / include_bots / include_saved_messages.
    """

    def __init__(self, cfg: Config, db: DB | None = None):
        self.cfg = cfg
        self.excluded_ids: set[int] = set()
        self.excluded_names: set[str] = set()
        self.excluded_usernames: set[str] = set()
        self.own_bot_id = cfg.bot_id
        for spec in cfg.exclude_chats:
            text = str(spec).strip()
            if text.lstrip("-").isdigit():
                self.excluded_ids.add(int(text))
            elif text.startswith("@"):
                self.excluded_usernames.add(text[1:].lower())
            elif text:
                self.excluded_names.add(text.lower())
        # в режиме списка — какие уже скачанные чаты соответствуют telegram.chats
        self.listed_ids: set[int] = set()
        if db is not None:
            for chat_id, title in db.chat_titles().items():
                if (title or "").lower() in self.excluded_names:
                    self.excluded_ids.add(chat_id)
            if not cfg.all_chats:
                for spec in cfg.chats:
                    self.listed_ids.update(db.find_chat_ids(spec) or [])

    def _listed(self, peer_id: int, raw_id: int | None, username: str, title: str) -> bool:
        return (
            peer_id in self.excluded_ids or (raw_id is not None and raw_id in self.excluded_ids)
            or (bool(username) and username in self.excluded_usernames)
            or (bool(title) and title in self.excluded_names)
        )

    def _service(self, raw_id: int | None, username: str) -> bool:
        return raw_id == TELEGRAM_SERVICE_ID or username in ALWAYS_EXCLUDED_USERNAMES or (
            self.own_bot_id is not None and raw_id == self.own_bot_id
        )

    def _kind_reason(self, kind: str) -> str | None:
        flag = KIND_FLAGS.get(kind)
        if flag and not getattr(self.cfg, flag):
            return f"{kind} ({FLAG_NAMES[flag]}: false)"
        return None

    def reason_excluded(self, entity, name: str | None = None, explicit: bool = False) -> str | None:
        """None — чат подходит; иначе — причина, почему он пропущен (настройками).
        Недоступные чаты (is_forbidden) сюда не относятся — их просто нельзя скачать."""
        raw_id = getattr(entity, "id", None)
        username = (getattr(entity, "username", None) or "").lower()
        if self._service(raw_id, username):
            return "это бот ассистента" if raw_id == self.own_bot_id else SERVICE_REASON
        try:
            peer_id = utils.get_peer_id(entity)
        except Exception:  # noqa: BLE001
            return "неизвестный тип чата"
        title = (name or display_name(entity) or "").lower()
        if self._listed(peer_id, raw_id, username, title):
            return "в списке exclude_chats"
        return None if explicit else self._kind_reason(chat_kind(entity))

    def allows(self, entity, name: str | None = None, explicit: bool = False) -> bool:
        return self.reason_excluded(entity, name, explicit) is None

    def db_excluded(self, c) -> bool:
        """Исключён ли уже скачанный чат — по данным из базы (без связи с Telegram)."""
        raw = raw_chat_id(c["chat_id"])
        username = (c["username"] or "").lower()
        explicit = bool(c["explicit"]) or not self.cfg.all_chats
        return (
            self._service(raw, username)
            or self._listed(c["chat_id"], raw, username, (c["title"] or "").lower())
            or (not explicit and self._kind_reason(c["kind"] or "") is not None)
        )

    def apply_to_db(self, db: DB) -> int:
        """Приводит отметку «отслеживается» у скачанных чатов в соответствие с настройками: исключённые
        перестают быть видны ассистенту, убранные из исключений возвращаются. В режиме списка
        отслеживаются только чаты из telegram.chats и явно скачанные (--chat). Возвращает число изменений."""
        changed = 0
        for c in db.chats():
            want = not self.db_excluded(c)
            if want and not self.cfg.all_chats:
                want = bool(c["explicit"]) or c["chat_id"] in self.listed_ids
            if bool(c["monitored"]) != want:
                db.set_monitored(c["chat_id"], want)
                changed += 1
                if not want:
                    db.set_kv("profile_built_at", None)  # профиль пересоберётся без исключённого
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
            ent = d.entity
            migrated = getattr(ent, "migrated_to", None)
            if isinstance(ent, Chat) and migrated is not None:
                # обычная группа превратилась в супергруппу — дальше переписка идёт там
                try:
                    new = await client.get_entity(migrated)
                    found[utils.get_peer_id(new)] = new
                    log.info("Группа «%s» стала супергруппой — беру новую", d.name)
                except Exception as e:  # noqa: BLE001
                    log.warning("Группа «%s» перенесена, но новая недоступна: %s", d.name, e)
            found[d.id] = ent
    return list(found.values()), missing


def _refresh_chat(db: DB, chat_id: int, entity, name: str | None = None) -> None:
    """Обновляет название/username/тип известного чата — чтобы исключения по названию работали и без связи."""
    if db.chat_row(chat_id) is not None and not is_forbidden(entity):
        db.upsert_chat(chat_id, name or display_name(entity) or str(chat_id),
                       getattr(entity, "username", None), chat_kind(entity))


async def all_dialog_entities(client: TelegramClient, chat_filter: ChatFilter,
                              db: DB | None = None) -> tuple[list, dict[str, int]]:
    """Все подходящие чаты аккаунта (включая архивную папку) + статистика пропущенных по причинам."""
    result, skipped = [], {}
    async for d in client.iter_dialogs():
        if is_forbidden(d.entity):
            skipped[FORBIDDEN_REASON] = skipped.get(FORBIDDEN_REASON, 0) + 1
            continue
        explicit = db.is_explicit(d.id) if db is not None else False
        reason = chat_filter.reason_excluded(d.entity, d.name, explicit)
        if reason:
            skipped[reason] = skipped.get(reason, 0) + 1
            if db is not None:
                _refresh_chat(db, d.id, d.entity, d.name)
                if db.chat_row(d.id) is not None:
                    db.set_monitored(d.id, False)
            continue
        result.append(d.entity)
    return result, skipped


# ---------------------------------------------------------------------- выгрузка
BACKFILL_GRACE = timedelta(minutes=30)


def _backfill_cutoff(db: DB, chat_id: int, online_since: datetime | None) -> datetime | None:
    """До какого момента сообщения считаются старой историей (а не «новым» для фоновой проверки).

    Пока первая полная выгрузка чата не завершена (в т.ч. если её прерывали) — граница одна и та же,
    сохранённая в базе. Сообщения, пришедшие после того, как программа выключилась (online_since),
    остаются «новыми» даже в только что найденном чате.
    """
    row = db.chat_row(chat_id)
    if row is not None and row["backfill_before"]:
        return from_db(row["backfill_before"])
    if row is None or row["last_sync"] is None:
        cutoff = utcnow() - BACKFILL_GRACE
        if online_since is not None:
            cutoff = min(cutoff, online_since)
        db.set_backfill_before(chat_id, to_db(cutoff))
        return cutoff
    return None


async def download_chat(
    client: TelegramClient,
    db: DB,
    entity,
    *,
    since: datetime | None = None,
    explicit: bool = False,
    online_since: datetime | None = None,
    media_dir: Path | None = None,
    media_max_bytes: int = 0,
    progress: Callable[[str], None] = print,
) -> int:
    """Скачивает историю чата в базу.

    Повторный запуск докачивает с отметки synced_msg_id — её двигает только эта функция, поэтому сообщения,
    сохранённые «вживую» во время выгрузки, не приводят к пропуску истории. Если выгрузка прервалась,
    следующий запуск продолжит с последней сохранённой пачки.
    """
    chat_id = utils.get_peer_id(entity)
    title = display_name(entity) or str(chat_id)
    db.upsert_chat(chat_id, title, getattr(entity, "username", None), chat_kind(entity))
    db.set_monitored(chat_id, True)
    if explicit:
        db.set_explicit(chat_id, True)

    synced = db.synced_msg_id(chat_id)
    if synced == 0:
        row = db.chat_row(chat_id)
        if since is not None:
            db.set_since(chat_id, to_db(since))
        elif row and row["since_date"]:
            since = from_db(row["since_date"])  # граница из прошлого download --since

    kwargs: dict = {"reverse": True, "wait_time": 0.5}
    if synced:
        kwargs["min_id"] = synced
    elif since:
        kwargs["offset_date"] = since

    backfill_before = _backfill_cutoff(db, chat_id, online_since)

    batch: list[dict] = []
    total = 0

    def flush() -> int:
        if not batch:
            return 0
        n = db.upsert_messages(batch)
        db.advance_synced(chat_id, max(r["msg_id"] for r in batch))
        batch.clear()
        return n

    async for msg in client.iter_messages(entity, **kwargs):
        if isinstance(msg, MessageService):
            continue
        row = await message_to_row(msg, chat_id, media_dir=media_dir, media_max_bytes=media_max_bytes)
        if backfill_before is not None and msg.date < backfill_before:
            row["backfill"] = 1
        batch.append(row)
        if len(batch) >= 500:
            total += flush()
            progress(f"  {title}: {total} сообщений…")
    total += flush()
    db.mark_synced(chat_id)
    return total


async def reconcile_recent(client: TelegramClient, db: DB, entity, days: int) -> tuple[int, int]:
    """Перечитывает последние N дней чата: подтягивает правки и отмечает удалённые сообщения,
    сделанные, пока программа была выключена. Возвращает (обновлено, отмечено удалённых)."""
    chat_id = utils.get_peer_id(entity)
    since = utcnow() - timedelta(days=days)
    row = db.chat_row(chat_id)
    if row is not None and row["since_date"]:
        since = max(since, from_db(row["since_date"]))  # не залезаем раньше границы download --since
    # верхняя граница — то, что уже лежало в архиве ДО чтения: пришедшее во время чтения не трогаем
    upper = db.last_msg_id(chat_id)
    rows, seen = [], set()
    async for msg in client.iter_messages(entity, offset_date=since, reverse=True, wait_time=0.5):
        seen.add(msg.id)
        if isinstance(msg, MessageService):
            continue
        rows.append(await message_to_row(msg, chat_id))
    # отметку synced здесь НЕ двигаем: она означает «скачано подряд», а тут только последние дни
    updated = db.upsert_messages(rows)
    margin = to_db(since + timedelta(minutes=5))
    missing = db.recent_ids(chat_id, margin, upper) - seen
    deleted = db.mark_deleted(chat_id, sorted(missing)) if missing else 0
    return updated, deleted


def _media_args(cfg: Config) -> dict:
    return {
        "media_dir": cfg.data_dir / "media" if cfg.download_media else None,
        "media_max_bytes": cfg.media_max_mb * 1024 * 1024,
    }


async def sync_chats(client: TelegramClient, db: DB, cfg: Config, chat_filter: ChatFilter,
                     busy: set[int] | None = None, reconcile: bool = True,
                     online_since: datetime | None = None) -> int:
    """Докачивает всё новое (например, после выключения компьютера) и подтягивает правки/удаления
    за последние дни. В режиме «все чаты» заодно находит новые чаты и скачивает их историю целиком."""
    total = 0
    targets: dict[int, object] = {}
    entities: dict[int, object] = {}
    window_start = utcnow() - timedelta(days=cfg.reconcile_days)
    explicit_specs: set[int] = set()

    if cfg.all_chats:
        async for d in client.iter_dialogs():
            if is_forbidden(d.entity):
                continue  # скачать нельзя; уже скачанное остаётся в памяти
            explicit = db.is_explicit(d.id)
            if chat_filter.reason_excluded(d.entity, d.name, explicit):
                _refresh_chat(db, d.id, d.entity, d.name)
                if d.id in db.monitored_chat_ids():
                    db.set_monitored(d.id, False)  # чат добавили в исключения
                continue
            entities[d.id] = d.entity
            known = db.chat_row(d.id)
            if known is not None and not known["monitored"]:
                db.set_monitored(d.id, True)  # чат убрали из исключений
                # всё, что пришло, пока чат был исключён, — не «новое» для фоновой проверки
                db.set_backfill_before(d.id, to_db(utcnow() - BACKFILL_GRACE))
            top = d.message.id if d.message else 0
            if not (top and top <= db.synced_msg_id(d.id)):
                targets[d.id] = d.entity
    else:
        await client.get_dialogs()  # прогреваем кеш, чтобы находить чаты по id
        resolved, missing = await resolve_chats(client, cfg.chats) if cfg.chats else ([], [])
        for m in missing:
            log.warning("Чат «%s» из telegram.chats не найден", m)
        for ent in resolved:
            if not is_forbidden(ent) and chat_filter.allows(ent, explicit=True):
                cid = utils.get_peer_id(ent)
                targets[cid] = ent
                explicit_specs.add(cid)
        for c in db.chats():
            if c["chat_id"] in targets or not c["explicit"]:
                continue
            try:
                entity = await client.get_entity(c["chat_id"])
            except Exception as e:  # noqa: BLE001
                log.warning("Чат %s недоступен: %s", c["chat_id"], e)
                continue
            if not is_forbidden(entity) and chat_filter.allows(entity, explicit=True):
                targets[c["chat_id"]] = entity
        # в режиме списка отслеживаются только выбранные чаты (временно недоступные не трогаем)
        for chat_id in db.monitored_chat_ids() - set(targets):
            row = db.chat_row(chat_id)
            if row is not None and not row["explicit"] and chat_id not in chat_filter.listed_ids:
                db.set_monitored(chat_id, False)
        entities = dict(targets)

    busy = busy if busy is not None else set()
    for chat_id, entity in targets.items():
        if chat_id in busy:
            continue  # этот чат уже скачивается
        is_new = db.synced_msg_id(chat_id) == 0
        busy.add(chat_id)
        try:
            n = await download_chat(client, db, entity, explicit=chat_id in explicit_specs,
                                    online_since=online_since, progress=lambda s: log.info(s),
                                    **_media_args(cfg))
        except Exception as e:  # noqa: BLE001
            log.warning("Не удалось обновить чат %s: %s", chat_id, e)
            continue
        finally:
            busy.discard(chat_id)
        if n:
            log.info("%s «%s»: +%s сообщений", "Новый чат" if is_new else "Докачано", display_name(entity), n)
        total += n

    if reconcile and cfg.reconcile_days > 0:
        # сверяем чаты, где в архиве есть сообщения за последние дни (в т.ч. если новых с тех пор не было)
        recent = db.chats_with_recent_rows(to_db(window_start)) & db.monitored_chat_ids() & set(entities)
        for chat_id in sorted(recent):
            if chat_id in busy:
                continue
            try:
                _, deleted = await reconcile_recent(client, db, entities[chat_id], cfg.reconcile_days)
            except Exception as e:  # noqa: BLE001
                log.warning("Не удалось сверить чат %s: %s", chat_id, e)
                continue
            if deleted:
                log.info("«%s»: отмечено удалённых сообщений: %s", display_name(entities[chat_id]), deleted)
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
            known = db.chat_row(chat_id)
            if known is not None and not known["monitored"]:
                return  # чат исключён настройками
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
