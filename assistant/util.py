"""Мелкие помощники: время, разбиение длинного текста."""

from __future__ import annotations

import re
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


# Похожее на номер карты: 13–19 цифр подряд, группы по 4 с одинаковым разделителем (4-4-4-4…) или Amex 4-6-5.
# Номера грузов, телефоны, индексы через пробел сюда не подходят.
CARD_RE = re.compile(
    r"(?<![\d-])(?:\d{13,19}"
    r"|\d{4}([ -])\d{4}\1\d{4}\1\d{1,4}(?:\1\d{1,3})?"   # 4-4-4-4 (+ хвост 1–3 цифры у 17–19-значных)
    r"|\d{4}([ -])\d{6}\2\d{5})(?![\d-])"                 # Amex 4-6-5
)


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def mask_cards(text: str) -> str:
    """Прячет номера банковских карт (13–19 цифр, проходят проверку Луна): «[карта ****1234]»."""
    if not text or not any(ch.isdigit() for ch in text):
        return text

    def repl(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        grouped_4444 = m.group(1) is not None  # только для записи группами 4-4-4-4-хвост
        if 13 <= len(digits) <= 19 and (
            _luhn_ok(digits) or (grouped_4444 and len(digits) > 16 and _luhn_ok(digits[:16]))
        ):
            return f"[карта ****{digits[-4:]}]"
        return m.group(0)

    return CARD_RE.sub(repl, text)
