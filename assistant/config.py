"""Загрузка настроек: секреты из .env, всё остальное из config.yaml."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_REPORT_INSTRUCTIONS = """\
Сделай ежедневный отчёт по водителям на сегодня.
1. По каждому водителю: какой сейчас груз, где и когда он закончит (место и время выгрузки), что у него дальше.
2. Кто освобождается сегодня и завтра — где именно (город/штат) и во сколько, чтобы планировать следующий груз.
3. Проблемы и риски: опоздания, нет апдейта, проблемы с документами, поломки, жалобы (из переписки за последние сутки).
4. Что изменилось в таблицах со вчерашнего дня (коротко, только важное).
5. Что нужно сделать сегодня: какие апдейты запросить, какие документы отправить и т.д.
Если у водителей сегодня есть погрузки/выгрузки с конкретным временем — создай напоминания
запросить апдейт примерно за час (сначала проверь list_reminders, чтобы не дублировать).
В конце — короткий чек-лист действий на день."""

DEFAULTS: dict = {
    "timezone": "",
    "telegram": {
        "chats": "all",
        "include_channels": False,
        "include_bots": False,
        "include_saved_messages": True,
        "exclude_chats": [],
        "download_media": False,
        "media_max_mb": 20,
        "reconcile_days": 3,
    },
    "privacy": {
        "mask_card_numbers": True,
    },
    "learn": {
        "nightly": True,
        "time": "03:30",
        "profile_rebuild_days": 7,
        "nightly_max_chunks": 20,
    },
    "google": {
        "auth": "service_account",
        "credentials_file": "credentials/google-service-account.json",
        "sheets": [],
    },
    "claude": {
        "model": "claude-opus-5-5",
        "watch_model": "",
        "effort_chat": "medium",
        "effort_report": "high",
        "effort_learn": "high",
        "effort_watch": "low",
        "fallbacks": True,
        "max_tool_steps": 30,
    },
    "report": {
        "enabled": True,
        "time": "08:00",
        "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        "instructions": DEFAULT_REPORT_INSTRUCTIONS,
    },
    "watch": {
        "enabled": True,
        "interval_minutes": 60,
        "active_hours": "07:00-23:00",
        "notify": True,
    },
    "memory": {
        "max_prompt_chars": 40000,
        "dialog_messages": 20,
        "learn_chunk_chars": 120000,
    },
}

WEEKDAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        elif value is not None:
            out[key] = value
    return out


def _hm(value, default: str) -> str:
    """'08:00' → '08:00'. YAML читает 8:00 без кавычек как число минут (480) — возвращаем обратно."""
    if value is None or value == "":
        return default
    if isinstance(value, int):
        return f"{value // 60:02d}:{value % 60:02d}"
    text = str(value).strip()
    if not re.fullmatch(r"\d{1,2}:\d{2}", text):
        raise SystemExit(f"Неверное время в config.yaml: {value!r} (нужно ЧЧ:ММ, например \"08:00\")")
    return text


ALL_WORDS = {"all", "все", "всё", "*"}


def _as_list(value) -> list:
    """Одиночное значение (строка/число) → список из одного элемента."""
    if value is None:
        return []
    if isinstance(value, (str, int, float)):
        return [value]
    return list(value)


def _chat_selection(value) -> tuple[bool, list]:
    """chats: all → (True, []); chats: [список] → (False, список). Пустой список тоже значит «все»."""
    if value is None:
        return True, []
    if isinstance(value, str):
        return (True, []) if value.strip().lower() in ALL_WORDS else (False, [value])
    items = [v for v in _as_list(value) if v is not None and str(v).strip()]
    if not items or any(str(v).strip().lower() in ALL_WORDS for v in items):
        return True, []
    return False, items


def _local_tz_name() -> str:
    tz = datetime.now().astimezone().tzinfo
    return getattr(tz, "key", None) or "UTC"


@dataclass
class SheetSource:
    name: str
    url: str
    worksheets: list[str] = field(default_factory=list)
    description: str = ""


@dataclass
class Config:
    root: Path
    data_dir: Path
    tz: ZoneInfo
    tz_name: str
    raw: dict

    # Telegram
    api_id: int | None
    api_hash: str | None
    phone: str | None
    bot_token: str | None
    owner_id: int | None
    all_chats: bool
    chats: list
    include_channels: bool
    include_bots: bool
    include_saved: bool
    exclude_chats: list
    download_media: bool
    media_max_mb: int
    reconcile_days: int
    mask_cards: bool

    # Google
    google_auth: str
    google_credentials: Path
    sheets: list[SheetSource]

    # Claude
    model: str
    watch_model: str
    effort_chat: str
    effort_report: str
    effort_learn: str
    effort_watch: str
    fallbacks: bool
    max_tool_steps: int

    # Расписание
    report_enabled: bool
    report_time: str
    report_days: list[str]
    report_instructions: str
    watch_enabled: bool
    watch_interval: int
    watch_hours: tuple[str, str]
    watch_notify: bool

    # Память
    memory_max_chars: int
    dialog_messages: int
    learn_chunk_chars: int
    nightly_learn: bool
    nightly_time: str
    profile_rebuild_days: int
    nightly_max_chunks: int

    @property
    def bot_id(self) -> int | None:
        """ID бота (первая часть токена) — чтобы не архивировать переписку с самим ботом."""
        if self.bot_token and ":" in self.bot_token:
            head = self.bot_token.split(":", 1)[0]
            if head.isdigit():
                return int(head)
        return None


def load_config(root: Path = ROOT) -> Config:
    load_dotenv(root / ".env", encoding="utf-8-sig")

    cfg_path = root / "config.yaml"
    user_cfg: dict = {}
    if cfg_path.exists():
        try:
            try:
                text = cfg_path.read_text(encoding="utf-8-sig")
            except UnicodeDecodeError:
                text = cfg_path.read_text(encoding="cp1251")  # старый Блокнот сохраняет в кодировке Windows
            user_cfg = yaml.safe_load(text) or {}
        except yaml.YAMLError as e:
            mark = getattr(e, "problem_mark", None)
            where = f" (строка {mark.line + 1})" if mark is not None else ""
            raise SystemExit(
                f"Ошибка в config.yaml{where}: {getattr(e, 'problem', e)}.\n"
                "Проверь отступы (пробелы, не табы) и кавычки вокруг текста с двоеточием."
            ) from e
        if not isinstance(user_cfg, dict):
            raise SystemExit("config.yaml должен состоять из настроек вида «ключ: значение».")
    raw = _merge(DEFAULTS, user_cfg)

    tz_name = (raw.get("timezone") or "").strip() or _local_tz_name()
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as e:
        raise SystemExit(f"Неизвестный часовой пояс в config.yaml: {tz_name!r} (пример: Europe/Moscow)") from e

    data_dir = root / "data"
    data_dir.mkdir(exist_ok=True)

    tg, gg, cl = raw["telegram"], raw["google"], raw["claude"]
    rep, watch, mem, lrn = raw["report"], raw["watch"], raw["memory"], raw["learn"]
    all_chats, chat_specs = _chat_selection(tg.get("chats"))
    # ignore_chats — старое имя настройки, понимаем и его
    exclude = _as_list(tg.get("exclude_chats")) + _as_list(tg.get("ignore_chats"))

    api_id = os.getenv("TG_API_ID", "").strip()
    owner = os.getenv("OWNER_ID", "").strip()

    sheets = []
    for s in gg.get("sheets") or []:
        if not s or not s.get("url"):
            continue
        sheets.append(SheetSource(
            name=str(s.get("name") or s["url"]),
            url=str(s["url"]).strip(),
            worksheets=[str(w) for w in (s.get("worksheets") or [])],
            description=str(s.get("description") or "").strip(),
        ))

    hours = str(watch.get("active_hours") or "00:00-24:00").split("-")
    if len(hours) != 2:
        hours = ["00:00", "24:00"]
    hours = [_hm(h.strip(), "00:00") for h in hours]

    cred = Path(gg["credentials_file"])
    if not cred.is_absolute():
        cred = root / cred

    return Config(
        root=root,
        data_dir=data_dir,
        tz=tz,
        tz_name=tz_name,
        raw=raw,
        api_id=int(api_id) if api_id.isdigit() else None,
        api_hash=os.getenv("TG_API_HASH", "").strip() or None,
        phone=os.getenv("TG_PHONE", "").strip() or None,
        bot_token=os.getenv("BOT_TOKEN", "").strip() or None,
        owner_id=int(owner) if owner.lstrip("-").isdigit() else None,
        all_chats=all_chats,
        chats=chat_specs,
        include_channels=bool(tg.get("include_channels")),
        include_bots=bool(tg.get("include_bots")),
        include_saved=bool(tg.get("include_saved_messages", True)),
        exclude_chats=exclude,
        download_media=bool(tg.get("download_media")),
        media_max_mb=int(tg.get("media_max_mb") or 20),
        reconcile_days=max(0, int(tg.get("reconcile_days", 3) or 0)),
        mask_cards=bool((raw.get("privacy") or {}).get("mask_card_numbers", True)),
        google_auth=str(gg.get("auth") or "service_account"),
        google_credentials=cred,
        sheets=sheets,
        model=str(cl["model"]),
        watch_model=str(cl.get("watch_model") or cl["model"]),
        effort_chat=str(cl["effort_chat"]),
        effort_report=str(cl["effort_report"]),
        effort_learn=str(cl["effort_learn"]),
        effort_watch=str(cl["effort_watch"]),
        fallbacks=bool(cl.get("fallbacks", True)),
        max_tool_steps=int(cl.get("max_tool_steps") or 30),
        report_enabled=bool(rep.get("enabled")),
        report_time=_hm(rep.get("time"), "08:00"),
        report_days=[str(d).lower()[:3] for d in (rep.get("days") or WEEKDAY_KEYS)],
        report_instructions=str(rep.get("instructions") or DEFAULT_REPORT_INSTRUCTIONS),
        watch_enabled=bool(watch.get("enabled")),
        watch_interval=max(5, int(watch.get("interval_minutes") or 60)),
        watch_hours=(hours[0].strip(), hours[1].strip()),
        watch_notify=bool(watch.get("notify", True)),
        memory_max_chars=int(mem.get("max_prompt_chars") or 40000),
        dialog_messages=int(mem.get("dialog_messages") or 20),
        learn_chunk_chars=int(mem.get("learn_chunk_chars") or 120000),
        nightly_learn=bool(lrn.get("nightly", True)),
        nightly_time=_hm(lrn.get("time"), "03:30"),
        profile_rebuild_days=max(1, int(lrn.get("profile_rebuild_days") or 7)),
        nightly_max_chunks=max(1, int(lrn.get("nightly_max_chunks") or 20)),
    )
