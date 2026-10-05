"""Мелкие помощники: время, разбиение длинного текста."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
DB_FMT = "%Y-%m-%d %H:%M:%S"
WEEKDAYS_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_db(dt: datetime) -> str:
    """Время для базы: всегда UTC, строка (сравнивается лексикографически)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime(DB_FMT)


def from_db(value: str) -> datetime:
    return datetime.strptime(value, DB_FMT).replace(tzinfo=UTC)


def fmt_local(value: str | datetime | None, tz: ZoneInfo, seconds: bool = False) -> str:
    if value is None:
        return "—"
    dt = from_db(value) if isinstance(value, str) else value
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S" if seconds else "%Y-%m-%d %H:%M")


def parse_local(value: str, tz: ZoneInfo) -> datetime:
    """'2025-10-05', '2025-10-05 14:30', '2025-10-05T14:30' → aware datetime (местное время)."""
    text = value.strip().replace("T", " ")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt


def now_line(tz: ZoneInfo, tz_name: str) -> str:
    now = datetime.now(tz)
    return f"Сейчас {now:%Y-%m-%d %H:%M}, {WEEKDAYS_RU[now.weekday()]} (часовой пояс {tz_name})"


def split_text(text: str, limit: int = 4000) -> list[str]:
    """Режет длинный текст на части для Telegram (лимит 4096), стараясь по абзацам/строкам."""
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    parts: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        parts.append(text)
    return parts


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[обрезано: показано {limit} из {len(text)} символов]"
