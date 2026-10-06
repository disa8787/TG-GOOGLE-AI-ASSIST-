"""Фоновые задачи: напоминания, ежедневный отчёт, наблюдение за чатами и таблицами."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, time, timedelta

from croniter import croniter

from .brain import Assistant
from .config import WEEKDAY_KEYS
from .learn import build_profile, learn_history, nightly_scope
from .llm import LLMError, estimate_cost
from .telegram_archive import ChatFilter
from .util import fmt_local, from_db, to_db, utcnow

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
            asyncio.create_task(self.nightly_loop(), name="nightly-learn"),
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

    # ------------------------------------------------------------------ ночное дообучение
    def nightly_due(self) -> bool:
        if not self.cfg.nightly_learn:
            return False
        now = datetime.now(self.cfg.tz)
        if now.time() < _parse_hm(self.cfg.nightly_time):
            return False
        return self.db.get_kv("last_nightly_date") != now.date().isoformat()

    def profile_due(self) -> bool:
        built = self.db.get_kv("profile_built_at")
        if not built:
            return True
        return utcnow() - from_db(built) >= timedelta(days=self.cfg.profile_rebuild_days)

    async def nightly_once(self) -> None:
        """Изучает новое за день (в память) и раз в несколько дней обновляет профиль.

        Без подтверждения изучаются только чаты, уже одобренные ручным learn, и небольшие новые чаты.
        О крупной неизученной истории (например, добавили в группу с годами переписки) — сообщаем владельцу.
        """
        say = log.info
        if not self.db.learning_started():
            say("Ночное изучение пропущено: сначала запусти «assistant learn» (там видна стоимость).")
            return
        ChatFilter(self.cfg, self.db).apply_to_db(self.db)
        eligible, deferred = nightly_scope(self.asst)
        ok = await learn_history(self.asst, None, self.cfg.model, yes=True, say=say,
                                 max_chunks=self.cfg.nightly_max_chunks, only_chats=eligible, approve=False)
        if ok and self.profile_due():
            await build_profile(self.asst, self.cfg.model, say=say)
        await self._report_backlog(deferred)

    async def _report_backlog(self, deferred: list[tuple[str, int, int]]) -> None:
        key = ";".join(sorted(t for t, _, _ in deferred))
        if not deferred or self.db.get_kv("backlog_reported") == key:
            return
        messages = sum(n for _, n, _ in deferred)
        chars = sum(c for _, _, c in deferred)
        cost = estimate_cost(self.cfg.model, int(chars / 2.5), int(chars / 2.5 / 10))
        names = ", ".join(f"«{t}» ({n})" for t, n, _ in deferred[:10])
        more = f" и ещё {len(deferred) - 10}" if len(deferred) > 10 else ""
        sent = await self._safe_notify(
            f"📚 Есть неизученная история: {messages} сообщений в чатах {names}{more}. "
            f"Без твоего согласия я её не изучаю (≈ ${cost:.2f}). Изучить: assistant learn"
        )
        if sent:
            self.db.set_kv("backlog_reported", key)

    async def nightly_loop(self) -> None:
        while True:
            try:
                if self.nightly_due():
                    self.db.set_kv("last_nightly_date", datetime.now(self.cfg.tz).date().isoformat())
                    log.info("Ночное изучение новых сообщений…")
                    await self.nightly_once()
            except Exception:  # noqa: BLE001
                log.exception("Ошибка ночного изучения")
            await asyncio.sleep(60)

    async def _safe_notify(self, text: str) -> bool:
        try:
            await self.notify(text)
            return True
        except Exception:  # noqa: BLE001
            log.exception("Не удалось отправить уведомление")
            return False

    # ------------------------------------------------------------------ «программа в сети»
    async def heartbeat_loop(self) -> None:
        """Раз в минуту отмечает, что программа работает. При следующем запуске по этой отметке
        видно, с какого момента компьютер был выключен — пришедшее после неё считается новым.
        Запускается только после успешной стартовой докачки, иначе отметка ушла бы вперёд раньше времени."""
        while True:
            try:
                self.db.set_kv("last_online", to_db(utcnow()))
            except Exception:  # noqa: BLE001
                log.exception("Не удалось сохранить отметку «в сети»")
            await asyncio.sleep(60)
