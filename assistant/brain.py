"""Ядро ассистента: собирает контекст (профиль, память, диалог) и запускает Claude с инструментами."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta

from .config import Config
from .db import DB
from .llm import LLM
from .prompts import (
    KNOWLEDGE_TEMPLATE,
    NO_PROFILE,
    REPORT_REQUEST,
    SYSTEM_BASE,
    WATCH_REQUEST,
)
from .sheets import Sheets
from .tools import ToolBox
from .util import fmt_local, now_line, truncate, utcnow

log = logging.getLogger(__name__)

WATCH_PAYLOAD_CHARS = 60000
WATCH_MAX_PASSES = 3


PURPOSES = {"chat": "чат", "report": "отчёты", "watch": "фоновая проверка", "learn": "изучение истории"}


def _num(value) -> str:
    return f"{int(value or 0):,}".replace(",", " ")


class Assistant:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.db = DB(cfg.data_dir / "assistant.db")
        self.sheets = Sheets(cfg)
        self.llm = LLM(cfg, self.db)
        self.tools = ToolBox(cfg, self.db, self.sheets, source="assistant")
        # Claude обрабатывает по одной задаче за раз — так проще и дешевле
        self.lock = asyncio.Lock()

    # ------------------------------------------------------------------ контекст
    @property
    def profile_path(self):
        return self.cfg.data_dir / "profile.md"

    def profile(self) -> str | None:
        """Профиль работы лежит в data/profile.md — его можно поправить вручную в блокноте."""
        try:
            text = self.profile_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        return text or None

    def save_profile(self, text: str) -> None:
        self.profile_path.write_text(text.strip() + "\n", encoding="utf-8")

    def memory_text(self, focus: str | None = None) -> str:
        """Заметки памяти для модели. Если все не помещаются — сначала те, что относятся к focus
        (например, к изучаемому куску переписки), затем самые свежие; остальные — через search_memory."""
        rows = self.db.all_memory()
        if not rows:
            return "(пусто)"
        limit = self.cfg.memory_max_chars
        lines = {r["id"]: f"#{r['id']} [{r['category']}] {r['subject']}: {r['content']}" for r in rows}
        total = sum(len(x) + 1 for x in lines.values())
        keep = set(lines)
        if total > limit:
            focus_low = (focus or "").lower()

            def relevant(r) -> bool:
                words = [w for w in re.findall(r"\w+", r["subject"].lower()) if len(w) >= 4]
                return any(w in focus_low for w in words)

            ordered = sorted(rows, key=lambda r: (relevant(r) if focus_low else False, r["updated_at"]),
                             reverse=True)
            keep, size = set(), 0
            for r in ordered:
                if size + len(lines[r["id"]]) > limit:
                    continue
                keep.add(r["id"])
                size += len(lines[r["id"]]) + 1
        text = "\n".join(lines[r["id"]] for r in rows if r["id"] in keep)
        hidden = len(rows) - len(keep)
        if hidden:
            text += f"\n(ещё {hidden} старых заметок не показаны — ищи через search_memory)"
        return text

    def knowledge_text(self) -> str:
        sheets = "\n".join(
            f"• {s.name}" + (f" — {s.description}" if s.description else "") for s in self.cfg.sheets
        ) or "(не настроены)"
        name = self.db.get_kv("owner_name") or "имя пока неизвестно (выполни login/download)"
        owner = f"{name}. В архиве его сообщения подписаны «Я»."
        return KNOWLEDGE_TEMPLATE.format(
            owner=owner,
            profile=self.profile() or NO_PROFILE,
            sheets=sheets,
            count=self.db.memory_count(),
            memory=self.memory_text(),
        )

    def system(self) -> list[dict]:
        # Неизменная часть + знания. Текущее время идёт в сообщение пользователя, чтобы не ломать кеш.
        return [
            {"type": "text", "text": SYSTEM_BASE},
            {"type": "text", "text": self.knowledge_text(), "cache_control": {"type": "ephemeral"}},
        ]

    def stamp(self) -> str:
        return f"[{now_line(self.cfg.tz, self.cfg.tz_name)}]"

    # ------------------------------------------------------------------ диалог с владельцем
    async def chat(self, text: str, extra_content: list[dict] | None = None) -> str:
        async with self.lock:
            reset_id = int(self.db.get_kv("dialog_reset_id") or 0)
            history = self.db.recent_dialog(self.cfg.dialog_messages, after_id=reset_id)
            messages = [{"role": r["role"], "content": r["content"]} for r in history]
            while messages and messages[0]["role"] != "user":
                messages.pop(0)
            stamped = f"{self.stamp()}\n{text}"
            messages.append({"role": "user", "content": [*(extra_content or []), {"type": "text", "text": stamped}]})
            answer = await self.llm.run_agent(
                purpose="chat", system=self.system(), messages=messages,
                toolbox=self.tools, effort=self.cfg.effort_chat,
            )
            self.db.add_dialog("user", stamped)
            self.db.add_dialog("assistant", answer)
            return answer

    def reset_dialog(self) -> None:
        self.db.set_kv("dialog_reset_id", str(self.db.last_dialog_id()))

    # ------------------------------------------------------------------ ежедневный отчёт
    async def daily_report(self) -> str:
        async with self.lock:
            if self.cfg.sheets:
                await self.sheets.snapshot_all(self.db)
            prompt = f"{self.stamp()}\n" + REPORT_REQUEST.format(instructions=self.cfg.report_instructions)
            answer = await self.llm.run_agent(
                purpose="report", system=self.system(), messages=[{"role": "user", "content": prompt}],
                toolbox=self.tools, effort=self.cfg.effort_report,
            )
            self.db.add_report("daily", answer)
            self.db.add_dialog("user", f"{self.stamp()}\n(автоматически) Сделай ежедневный отчёт.")
            self.db.add_dialog("assistant", answer)
        reports_dir = self.cfg.data_dir / "reports"
        reports_dir.mkdir(exist_ok=True)
        path = reports_dir / f"{datetime.now(self.cfg.tz):%Y-%m-%d_%H%M}.md"
        path.write_text(answer, encoding="utf-8")
        return answer

    # ------------------------------------------------------------------ фоновое наблюдение
    async def watch_check(self, ignored: set[int] | None = None) -> str | None:
        """Смотрит новые сообщения и изменения в таблицах. Возвращает уведомление или None.

        Старая история, скачанная задним числом (новый чат, первая выгрузка), сюда не попадает.
        Если новых сообщений много, они разбираются по порядку в несколько заходов — ничего не пропускается;
        что не поместилось сейчас — будет в следующей проверке.
        """
        changes = await self.sheets.snapshot_all(self.db) if self.cfg.sheets else []

        top = self.db.max_message_rowid()
        cursor = self.db.get_kv("watch_last_rowid")
        if cursor is None:
            # первый запуск — архив целиком изучается через learn, начинаем с текущего момента
            self.db.set_kv("watch_last_rowid", str(top))
            cursor = str(top)
        cursor_id = int(cursor)
        chats = self.db.monitored_chat_ids() - (ignored or set())

        notes: list[str] = []
        for _ in range(WATCH_MAX_PASSES):
            rows = [r for r in self.db.messages_after_rowid(cursor_id, chats, limit=5000, exclude_backfill=True)
                    if r["id"] <= top]
            page, size = [], 0
            for r in rows:
                line_len = len(r["text"] or "") + 120
                if page and size + line_len > WATCH_PAYLOAD_CHARS:
                    break
                page.append(r)
                size += line_len
            if not page and not changes:
                break
            parts = []
            if page:
                more = len(rows) - len(page)
                parts.append(f"Новые сообщения ({len(page)}"
                             + (f"; ещё {more} — в следующем заходе" if more else "") + "):\n"
                             + self.tools.format_messages(page))
            if changes:
                parts.append("Изменения в таблицах:\n" + truncate("\n\n".join(changes), 30000))
                changes = []
            prompt = f"{self.stamp()}\n" + WATCH_REQUEST.format(payload="\n\n".join(parts))
            async with self.lock:
                answer = await self.llm.run_agent(
                    purpose="watch", model=self.cfg.watch_model, system=self.system(),
                    messages=[{"role": "user", "content": prompt}], toolbox=self.tools, effort=self.cfg.effort_watch,
                )
            if page:
                cursor_id = page[-1]["id"]
                self.db.set_kv("watch_last_rowid", str(cursor_id))
            text = answer.strip()
            if text and not text.upper().startswith("NOTHING"):
                notes.append(text)
            if len(page) == len(rows):
                break  # всё новое разобрано

        # дальше «новых» нет (остались только строки старой истории) — сдвигаем отметку до конца
        remaining = self.db.messages_after_rowid(cursor_id, chats, limit=1, exclude_backfill=True)
        if not [r for r in remaining if r["id"] <= top]:
            self.db.set_kv("watch_last_rowid", str(top))

        if not notes:
            return None
        answer = "\n\n".join(notes)
        self.db.add_report("watch", answer)
        self.db.add_dialog("user", f"{self.stamp()}\n(автоматически) Фоновая проверка новых сообщений и таблиц.")
        self.db.add_dialog("assistant", answer)
        return answer

    # ------------------------------------------------------------------ статистика
    def usage_text(self) -> str:
        now_local = datetime.now(self.cfg.tz)
        day_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        month_start = day_start.replace(day=1)
        out = []
        for label, since in (("Сегодня", day_start), ("С начала месяца", month_start)):
            rows = self.db.usage_since(since)
            total = sum((r["cost"] or 0) for r in rows)
            out.append(f"**{label}: ≈ ${total:.2f}**")
            for r in rows:
                out.append(
                    f"• {PURPOSES.get(r['purpose'], r['purpose'])}: {r['calls']} запросов, ≈ ${r['cost'] or 0:.2f} "
                    f"(вход {_num(r['inp'])}, из кеша {_num(r['cr'])}, выход {_num(r['outp'])} токенов)"
                )
        return "\n".join(out)

    def status_text(self) -> str:
        st = self.db.message_stats()
        chats = self.db.chats()
        reminders = self.db.active_reminders()
        last_report = self.db.reports_since(utcnow() - timedelta(days=30), "daily")
        lines = [
            f"Архив: {st['n']} сообщений в {len(chats)} чатах "
            f"({fmt_local(st['first'], self.cfg.tz)[:10]} … {fmt_local(st['last'], self.cfg.tz)})",
            f"Отслеживаются: {sum(1 for c in chats if c['monitored'])} чатов"
            + (" (режим «все чаты»: новые подключаются сами)" if self.cfg.all_chats else ""),
            f"Память: {self.db.memory_count()} заметок; профиль работы: "
            + ("есть" if self.profile() else "нет (запусти learn)"),
            f"Таблиц настроено: {len(self.cfg.sheets)}",
            f"Активных напоминаний: {len(reminders)}",
            "Последний отчёт: " + (fmt_local(last_report[-1]["created_at"], self.cfg.tz) if last_report else "—"),
            f"Модель: {self.cfg.model}",
        ]
        return "\n".join(lines)
