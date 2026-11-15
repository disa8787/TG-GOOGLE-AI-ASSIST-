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
        "resync_minutes": 30,
        "proxy": "",
    },
    "assistant": {
        "about": "",
        "instructions": "",
    },
    "commands": {},
    "schedule": [],
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
        "max_tool_steps": 50,
        "web_search": True,
        "web_search_max_uses": 5,
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
    text = str(value).strip().replace(".", ":")
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if not m or int(m[1]) > 24 or int(m[2]) > 59:
        raise SystemExit(f"Неверное время в config.yaml: {value!r} (нужно ЧЧ:ММ, например \"08:00\")")
    return f"{int(m[1]):02d}:{m[2]}"


TRUE_WORDS = {"true", "yes", "on", "1", "да", "вкл", "включено", "y"}
FALSE_WORDS = {"false", "no", "off", "0", "нет", "выкл", "выключено", "n", ""}


def _bool(value, default: bool, where: str, warnings: list[str]) -> bool:
    """Да/нет из config.yaml. Важно: «нет» в YAML — это текст, и bool("нет") было бы True."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in TRUE_WORDS:
        return True
    if text in FALSE_WORDS:
        return False
    warnings.append(f"{where}: ожидается true/false (да/нет), а не «{value}» — оставляю {'да' if default else 'нет'}")
    return default


def _int(value, default: int, where: str, warnings: list[str], minimum: int | None = None) -> int:
    """Число из config.yaml; опечатка не роняет программу, а попадает в предупреждения."""
    if value is None or (isinstance(value, str) and not value.strip()):
        number = default
    else:
        try:
            number = int(str(value).strip())
        except ValueError:
            warnings.append(f"{where}: ожидается число, а не «{value}» — беру {default}")
            number = default
    return max(minimum, number) if minimum is not None else number


# пн, вт… и полные названия по-русски → mon, tue…; группы дней
_RU_DAYS = {"пн": "mon", "по": "mon", "вт": "tue", "ср": "wed", "чт": "thu", "че": "thu", "пт": "fri",
            "пя": "fri", "сб": "sat", "су": "sat", "вс": "sun", "во": "sun"}
_DAY_GROUPS = {
    "weekdays": ["mon", "tue", "wed", "thu", "fri"], "будни": ["mon", "tue", "wed", "thu", "fri"],
    "weekends": ["sat", "sun"], "weekend": ["sat", "sun"], "выходные": ["sat", "sun"],
    "all": None, "every": None, "daily": None, "все": None, "каждый": None, "ежедневно": None,
}


def _days(value, where: str, warnings: list[str]) -> list[str]:
    """days: [mon, tue] / "пн, вт, ср" / пн-пт / будни / выходные → ['mon', 'tue', …].
    Пусто или непонятно — все дни."""
    items = _as_list(value)
    if len(items) == 1 and isinstance(items[0], str):
        text = re.sub(r"\s*[-–—]\s*", "-", items[0])  # «пт - пн» → «пт-пн», чтобы не разорвать диапазон
        items = [x for x in re.split(r"[\s,;]+", text) if x]
    def one(text: str) -> str | None:
        text = text.strip().rstrip(".")
        return text[:3] if text[:3] in WEEKDAY_KEYS else _RU_DAYS.get(text[:2])

    days: set[str] = set()
    for item in items:
        key = str(item).strip().lower().rstrip(".")
        if key in _DAY_GROUPS:
            days.update(_DAY_GROUPS[key] or WEEKDAY_KEYS)
            continue
        ends = re.split(r"\s*[-–—]\s*", key)
        if len(ends) == 2 and one(ends[0]) and one(ends[1]):  # диапазон: пн-пт, fri-mon
            i, j = WEEKDAY_KEYS.index(one(ends[0])), WEEKDAY_KEYS.index(one(ends[1]))
            days.update(WEEKDAY_KEYS[(i + k) % 7] for k in range((j - i) % 7 + 1))
            continue
        day = one(key)
        if day:
            days.add(day)
        else:
            warnings.append(f"{where}: непонятный день «{item}» — пропущен (пиши mon…sun или пн…вс)")
    return [d for d in WEEKDAY_KEYS if d in days] or list(WEEKDAY_KEYS)


def _legacy(section: str, key: str, value: int) -> int:
    old, new = LEGACY_DEFAULTS[(section, key)]
    return new if value == old else value


ALL_WORDS = {"all", "все", "всё", "*"}
# заглушка из первой версии config.example.yaml — если её не заменили, значит берём все чаты
PLACEHOLDER_CHATS = {"название рабочего чата"}


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
    items = [v for v in _as_list(value)
             if v is not None and str(v).strip() and str(v).strip().lower() not in PLACEHOLDER_CHATS]
    if not items or any(str(v).strip().lower() in ALL_WORDS for v in items):
        return True, []
    return False, items


def _text_without_comments(value) -> str:
    """Многострочный текст из config.yaml: строки, начинающиеся с #, — это примеры/комментарии, не текст."""
    lines = [line for line in str(value or "").splitlines() if not line.lstrip().startswith("#")]
    return "\n".join(lines).strip()


def _local_tz_name() -> str:
    tz = datetime.now().astimezone().tzinfo
    return getattr(tz, "key", None) or "UTC"


@dataclass
class ScheduledTask:
    """Своя задача по расписанию: в time по дням days ассистент выполняет prompt и присылает результат."""
    name: str
    time: str
    days: list[str]
    prompt: str


# Встроенные команды бота — свои команды с такими именами не переопределяют их
BUILTIN_COMMANDS = {
    "start", "help", "report", "check", "reminders", "memory", "new", "download", "sync", "learn",
    "learn_yes", "learn_no", "status", "usage", "advice",
}

# Значения из старых версий config.example.yaml, которые теперь шире. Если владелец их не менял
# (стоит ровно старое значение по умолчанию) — берём новое.
LEGACY_DEFAULTS = {("claude", "max_tool_steps"): (30, 50)}


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
    resync_minutes: int

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
    web_search: bool
    web_search_max_uses: int

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

    # Гибкая настройка под работу
    proxy: str = ""
    about: str = ""
    instructions: str = ""
    commands: dict[str, str] = field(default_factory=dict)
    schedule: list[ScheduledTask] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

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

    warnings: list[str] = []
    for section, default in DEFAULTS.items():
        if isinstance(default, dict) and not isinstance(raw.get(section), dict):
            # например «report: false» вместо «report: {enabled: false}» — не падаем, а подсказываем
            warnings.append(f"{section}: ожидаются вложенные настройки (с отступом ниже) — взяты стандартные")
            raw[section] = _merge(default, {})
    tg, gg, cl = raw["telegram"], raw["google"], raw["claude"]
    rep, watch, mem, lrn = raw["report"], raw["watch"], raw["memory"], raw["learn"]
    all_chats, chat_specs = _chat_selection(tg.get("chats"))
    # ignore_chats — старое имя настройки, понимаем и его
    exclude = _as_list(tg.get("exclude_chats")) + _as_list(tg.get("ignore_chats"))

    api_id = os.getenv("TG_API_ID", "").strip()
    owner = os.getenv("OWNER_ID", "").strip()

    sheets = []
    for s in _as_list(gg.get("sheets")):
        if isinstance(s, str):
            s = {"url": s}  # можно писать просто ссылку
        if not isinstance(s, dict) or not s.get("url") or "ВСТАВЬ" in str(s.get("url")).upper():
            continue  # пустая запись или заглушка из примера — таблица не настроена
        sheets.append(SheetSource(
            name=str(s.get("name") or s["url"]),
            url=str(s["url"]).strip(),
            worksheets=[str(w) for w in _as_list(s.get("worksheets"))],
            description=str(s.get("description") or "").strip(),
        ))

    commands: dict[str, str] = {}
    raw_commands = raw.get("commands") or {}
    if not isinstance(raw_commands, dict):
        warnings.append("commands: ожидается список вида «имя: что сделать» — пропущено")
        raw_commands = {}
    for name, prompt in raw_commands.items():
        key = str(name).strip().lstrip("/").lower()
        if not re.fullmatch(r"[a-z0-9_]{1,32}", key):
            warnings.append(f"commands: «{name}» — имя команды только латиницей/цифрами/_, например free_drivers")
            continue
        if key in BUILTIN_COMMANDS:
            warnings.append(f"commands: /{key} — встроенная команда, выбери другое имя")
            continue
        if not str(prompt or "").strip():
            continue
        commands[key] = str(prompt).strip()

    schedule: list[ScheduledTask] = []
    for i, item in enumerate(_as_list(raw.get("schedule")), 1):
        if not isinstance(item, dict) or not str(item.get("prompt") or "").strip():
            warnings.append(f"schedule: задача №{i} без prompt — пропущена")
            continue
        if not _bool(item.get("enabled"), True, f"schedule №{i}: enabled", warnings):
            continue
        schedule.append(ScheduledTask(
            name=str(item.get("name") or f"Задача {i}").strip(),
            time=_hm(item.get("time"), "09:00"),
            days=_days(item.get("days"), f"schedule №{i}: days", warnings),
            prompt=str(item["prompt"]).strip(),
        ))
    assistant_cfg = raw.get("assistant") or {}
    w = warnings

    hours = re.sub(r"\s*[–—]\s*", "-", str(watch.get("active_hours") or "00:00-24:00")).split("-")
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
        include_channels=_bool(tg.get("include_channels"), False, "telegram.include_channels", w),
        include_bots=_bool(tg.get("include_bots"), False, "telegram.include_bots", w),
        include_saved=_bool(tg.get("include_saved_messages"), True, "telegram.include_saved_messages", w),
        exclude_chats=exclude,
        download_media=_bool(tg.get("download_media"), False, "telegram.download_media", w),
        media_max_mb=_int(tg.get("media_max_mb") or 20, 20, "telegram.media_max_mb", w, minimum=1),
        reconcile_days=_int(tg.get("reconcile_days"), 3, "telegram.reconcile_days", w, minimum=0),
        resync_minutes=_int(tg.get("resync_minutes"), 30, "telegram.resync_minutes", w, minimum=0),
        mask_cards=_bool(raw["privacy"].get("mask_card_numbers"), True, "privacy.mask_card_numbers", w),
        google_auth=str(gg.get("auth") or "service_account"),
        google_credentials=cred,
        sheets=sheets,
        model=str(cl.get("model") or DEFAULTS["claude"]["model"]).strip(),
        watch_model=str(cl.get("watch_model") or cl.get("model") or DEFAULTS["claude"]["model"]).strip(),
        effort_chat=str(cl["effort_chat"]),
        effort_report=str(cl["effort_report"]),
        effort_learn=str(cl["effort_learn"]),
        effort_watch=str(cl["effort_watch"]),
        fallbacks=_bool(cl.get("fallbacks"), True, "claude.fallbacks", w),
        max_tool_steps=_legacy("claude", "max_tool_steps",
                               _int(cl.get("max_tool_steps") or 50, 50, "claude.max_tool_steps", w, minimum=2)),
        web_search=_bool(cl.get("web_search"), True, "claude.web_search", w),
        web_search_max_uses=_int(cl.get("web_search_max_uses") or 5, 5, "claude.web_search_max_uses", w,
                                 minimum=1),
        report_enabled=_bool(rep.get("enabled"), True, "report.enabled", w),
        report_time=_hm(rep.get("time"), "08:00"),
        report_days=_days(rep.get("days"), "report.days", w),
        report_instructions=str(rep.get("instructions") or DEFAULT_REPORT_INSTRUCTIONS),
        watch_enabled=_bool(watch.get("enabled"), True, "watch.enabled", w),
        watch_interval=_int(watch.get("interval_minutes") or 60, 60, "watch.interval_minutes", w, minimum=5),
        watch_hours=(hours[0].strip(), hours[1].strip()),
        watch_notify=_bool(watch.get("notify"), True, "watch.notify", w),
        memory_max_chars=_int(mem.get("max_prompt_chars") or 40000, 40000, "memory.max_prompt_chars", w,
                              minimum=2000),
        dialog_messages=_int(mem.get("dialog_messages") or 20, 20, "memory.dialog_messages", w, minimum=2),
        learn_chunk_chars=_int(mem.get("learn_chunk_chars") or 120000, 120000, "memory.learn_chunk_chars", w,
                               minimum=1000),
        nightly_learn=_bool(lrn.get("nightly"), True, "learn.nightly", w),
        nightly_time=_hm(lrn.get("time"), "03:30"),
        profile_rebuild_days=_int(lrn.get("profile_rebuild_days") or 7, 7, "learn.profile_rebuild_days", w,
                                  minimum=1),
        nightly_max_chunks=_int(lrn.get("nightly_max_chunks") or 20, 20, "learn.nightly_max_chunks", w,
                                minimum=1),
        proxy=str(tg.get("proxy") or "").strip(),
        about=_text_without_comments(assistant_cfg.get("about")),
        instructions=_text_without_comments(assistant_cfg.get("instructions")),
        commands=commands,
        schedule=schedule,
        warnings=warnings,
    )
