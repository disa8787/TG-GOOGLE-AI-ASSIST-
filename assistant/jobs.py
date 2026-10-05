"""Фоновые задачи: напоминания, ежедневный отчёт, наблюдение за чатами и таблицами."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, time, timedelta

from croniter import croniter

from .brain import Assistant
from .config import WEEKDAY_KEYS
from .llm import LLMError
from .util import fmt_local, from_db, utcnow

log = logging.getLogger(__name__)

Notify = Callable[[str], Awaitable[None]]


def _parse_hm(value: str) -> time:
    hh, mm = value.strip().split(":")
    if int(hh) >= 24:
        return time(23, 59, 59)
    return time(int(hh), int(mm))


class Scheduler:
    def __init__(self, asst: Assistant, notify: Notify, ignored: set[int]):
        self.asst = asst
        self.cfg = asst.cfg
        self.db = asst.db
        self.notify = notify
        self.ignored = ignored

    def tasks(self) -> list[asyncio.Task]:
        return [
            asyncio.create_task(self.reminders_loop(), name="reminders"),
            asyncio.create_task(self.report_loop(), name="report"),
            asyncio.create_task(self.watch_loop(), name="watch"),
        ]

    # ------------------------------------------------------------------ напоминания
    async def fire_due_reminders(self) -> None:
        now = utcnow()
        for r in self.db.due_reminders(now):
            text = f"⏰ **Напоминание:** {r['text']}"
            if now - from_db(r["due_at"]) > timedelta(minutes=10):
                text += f"\n(должно было сработать {fmt_local(r['due_at'], self.cfg.tz)})"
            await self.notify(text)  # если отправка не удалась — попробуем снова на следующем круге
            next_due = None
            if r["cron"]:
                next_due = croniter(r["cron"], datetime.now(self.cfg.tz)).get_next(datetime)
            self.db.reminder_fired(r["id"], next_due)

    async def reminders_loop(self) -> None:
        while True:
            try:
                await self.fire_due_reminders()
            except Exception:  # noqa: BLE001
                log.exception("Ошибка в цикле напоминаний")
            await asyncio.sleep(20)

    # ------------------------------------------------------------------ ежедневный отчёт
    def report_due(self) -> bool:
        if not self.cfg.report_enabled:
            return False
        now = datetime.now(self.cfg.tz)
        if WEEKDAY_KEYS[now.weekday()] not in self.cfg.report_days:
            return False
        if now.time() < _parse_hm(self.cfg.report_time):
            return False
        # если компьютер был выключен в момент отчёта — отчёт придёт сразу после запуска
        return self.db.get_kv("last_report_date") != now.date().isoformat()

    async def report_loop(self) -> None:
        while True:
            try:
                if self.report_due():
                    self.db.set_kv("last_report_date", datetime.now(self.cfg.tz).date().isoformat())
                    log.info("Готовлю ежедневный отчёт…")
                    text = await self.asst.daily_report()
                    await self.notify("📋 **Ежедневный отчёт**\n\n" + text)
            except LLMError as e:
                await self._safe_notify(f"⚠️ Не удалось сделать ежедневный отчёт: {e}")
            except Exception:  # noqa: BLE001
                log.exception("Ошибка при подготовке отчёта")
            await asyncio.sleep(30)

    # ------------------------------------------------------------------ наблюдение
    def in_active_hours(self) -> bool:
        start, end = (_parse_hm(x) for x in self.cfg.watch_hours)
        now = datetime.now(self.cfg.tz).time()
        if start <= end:
            return start <= now <= end
        return now >= start or now <= end  # окно через полночь, напр. 20:00-06:00

    async def watch_once(self) -> str | None:
        note = await self.asst.watch_check(self.ignored)
        if note and self.cfg.watch_notify:
            await self.notify("👀 " + note)
        return note

    async def watch_loop(self) -> None:
        await asyncio.sleep(90)  # даём докачаться истории после запуска
        while True:
            try:
                if self.cfg.watch_enabled and self.in_active_hours():
                    await self.watch_once()
            except LLMError as e:
                log.warning("Фоновая проверка не удалась: %s", e)
            except Exception:  # noqa: BLE001
                log.exception("Ошибка фоновой проверки")
            await asyncio.sleep(self.cfg.watch_interval * 60)

    async def _safe_notify(self, text: str) -> None:
        try:
            await self.notify(text)
        except Exception:  # noqa: BLE001
            log.exception("Не удалось отправить уведомление")
