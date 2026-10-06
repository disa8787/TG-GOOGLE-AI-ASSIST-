"""Инструменты, которые Claude может вызывать: архив Telegram, таблицы, память, напоминания, отчёты."""

from __future__ import annotations

import inspect
import json
import logging
from datetime import datetime, timedelta

from croniter import croniter

from .config import Config
from .db import DB
from .sheets import Sheets, diff_tables, format_table
from .util import fmt_local, mask_cards, parse_local, to_db, truncate, utcnow

log = logging.getLogger(__name__)

MAX_RESULT_CHARS = 40000

OWNER_WORDS = {"я", "me", "владелец", "owner"}

DATE_HINT = "Формат: 'YYYY-MM-DD' или 'YYYY-MM-DD HH:MM' (местное время владельца)."

TOOL_SCHEMAS: list[dict] = [
    {
        "name": "search_messages",
        "description": (
            "Полнотекстовый поиск по архиву переписки Telegram (все скачанные рабочие чаты). "
            "Возвращает сообщения, новые сверху: время, чат, автор, id сообщения, текст. "
            "Ищи по основе слова без окончания (например «выгруз» найдёт «выгрузка», «выгрузил»), "
            "по номерам грузов, именам, городам. "
            "Чтобы увидеть контекст найденного сообщения — read_chat с around_message_id."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Слова для поиска (минимум 3 буквы в слове)."},
                "mode": {"type": "string", "enum": ["all", "any"],
                         "description": "all — все слова (по умолчанию), any — хотя бы одно."},
                "chat": {"type": "string", "description": "Ограничить чатом: id или часть названия."},
                "sender": {"type": "string",
                           "description": "Часть имени автора. «Я» — только сообщения самого владельца."},
                "date_from": {"type": "string", "description": DATE_HINT},
                "date_to": {"type": "string", "description": DATE_HINT},
                "limit": {"type": "integer",
                          "description": "Сколько сообщений вернуть (по умолчанию 40, максимум 200)."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_chat",
        "description": (
            "Читает переписку подряд, по хронологии. Без дат — последние сообщения. "
            "С date_from — сообщения начиная с этой даты. С around_message_id — контекст вокруг сообщения."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "chat": {"type": "string", "description": "id или часть названия чата. Пусто — все чаты сразу."},
                "date_from": {"type": "string", "description": DATE_HINT},
                "date_to": {"type": "string", "description": DATE_HINT},
                "around_message_id": {"type": "integer",
                                      "description": "id сообщения, вокруг которого показать контекст."},
                "limit": {"type": "integer", "description": "Сколько сообщений (по умолчанию 100, максимум 400)."},
            },
        },
    },
    {
        "name": "list_chats",
        "description": "Список чатов в архиве: название, id, число сообщений, период переписки.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_spreadsheets",
        "description": "Настроенные Google-таблицы: название, описание и список листов.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "read_sheet",
        "description": (
            "Читает лист Google-таблицы (актуальные данные). Формат строк: «номер строки: ячейка | ячейка | …», "
            "колонки по порядку A, B, C… Можно отфильтровать строки по тексту (query), например по имени водителя."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "spreadsheet": {"type": "string", "description": "Название таблицы из list_spreadsheets (или ссылка)."},
                "worksheet": {"type": "string", "description": "Название листа. Пусто — первый лист."},
                "query": {"type": "string",
                          "description": "Показать только строки, содержащие этот текст. Несколько вариантов через |"},
                "start_row": {"type": "integer", "description": "С какой строки начать (по умолчанию 1)."},
                "max_rows": {"type": "integer", "description": "Максимум строк (по умолчанию 400)."},
            },
            "required": ["spreadsheet"],
        },
    },
    {
        "name": "sheet_changes",
        "description": (
            "Что изменилось в таблицах за последние N часов (по сохранённым снимкам): "
            "добавленные, удалённые, изменённые строки."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "spreadsheet": {"type": "string", "description": "Название таблицы. Пусто — все таблицы."},
                "since_hours": {"type": "number", "description": "За сколько часов (по умолчанию 24)."},
            },
        },
    },
    {
        "name": "remember",
        "description": (
            "Сохранить или обновить заметку в долговременной памяти. Заметка с тем же subject перезаписывается — "
            "поэтому при обновлении пиши полный актуальный текст (старое важное + новое). "
            "Сохраняй: водителей (трак, телефон, особенности), клиентов/брокеров, договорённости, правила работы, "
            "предпочтения владельца, повторяющиеся проблемы."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string",
                             "description": "Например: водитель, клиент, брокер, процесс, правило, контакт, "
                                            "таблица, владелец, прочее."},
                "subject": {"type": "string", "description": "Короткий заголовок, например «Водитель: Иван Петров»."},
                "content": {"type": "string", "description": "Полный текст заметки."},
            },
            "required": ["category", "subject", "content"],
        },
    },
    {
        "name": "forget",
        "description": "Удалить заметку из памяти по её номеру (#id) — если она устарела или ошибочна.",
        "input_schema": {
            "type": "object",
            "properties": {"note_id": {"type": "integer"}},
            "required": ["note_id"],
        },
    },
    {
        "name": "search_memory",
        "description": "Поиск по заметкам в памяти (если нужной заметки не видно в разделе «Память»).",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "create_reminder",
        "description": (
            "Создать напоминание владельцу (придёт сообщением в Telegram). Укажи ровно одно: "
            "at — разовое на конкретное время; in_minutes — разовое через N минут; "
            "cron — повторяющееся (cron-выражение в местном времени, напр. '0 9 * * 1-5' — по будням в 9:00, "
            "'0 */2 * * *' — каждые 2 часа)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Текст напоминания — понятный без контекста."},
                "at": {"type": "string", "description": "Когда: 'YYYY-MM-DD HH:MM' (местное время)."},
                "in_minutes": {"type": "integer", "description": "Через сколько минут."},
                "cron": {"type": "string", "description": "Расписание для повторяющегося напоминания."},
            },
            "required": ["text"],
        },
    },
    {
        "name": "list_reminders",
        "description": "Список активных напоминаний.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "cancel_reminder",
        "description": "Отменить напоминание по номеру.",
        "input_schema": {
            "type": "object",
            "properties": {"reminder_id": {"type": "integer"}},
            "required": ["reminder_id"],
        },
    },
    {
        "name": "get_reports",
        "description": "Прошлые ежедневные отчёты и уведомления ассистента — чтобы сравнить с тем, что было раньше.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "За сколько дней (по умолчанию 3)."},
                "kind": {"type": "string", "enum": ["daily", "watch", "all"],
                         "description": "daily — ежедневные отчёты (по умолчанию), watch — уведомления, all — всё."},
            },
        },
    },
    {
        "name": "search_conversations",
        "description": (
            "Поиск по прошлым разговорам владельца с тобой (ассистентом) и по твоим отчётам/уведомлениям — "
            "«что я тебе говорил про …», «что ты писал в отчёте про …»."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Слова для поиска."},
                "limit": {"type": "integer", "description": "Сколько записей (по умолчанию 20)."},
            },
            "required": ["query"],
        },
    },
]


class ToolError(Exception):
    pass


class ToolBox:
    def __init__(self, cfg: Config, db: DB, sheets: Sheets, source: str = "assistant"):
        self.cfg = cfg
        self.db = db
        self.sheets = sheets
        self.source = source
        self.schemas = TOOL_SCHEMAS

    # ------------------------------------------------------------------ диспетчер
    async def execute(self, name: str, args: dict, tool_use_id: str) -> dict:
        fn = getattr(self, f"t_{name}", None)
        try:
            if fn is None:
                raise ToolError(f"неизвестный инструмент {name}")
            if not isinstance(args, dict):
                raise ToolError("аргументы должны быть объектом")
            params = inspect.signature(fn).parameters
            clean = {k: v for k, v in args.items() if k in params and v is not None and v != ""}
            missing = [p for p, spec in params.items() if spec.default is inspect.Parameter.empty and p not in clean]
            if missing:
                raise ToolError(f"не хватает параметров: {', '.join(missing)}")
            result = fn(**clean)
            if inspect.isawaitable(result):
                result = await result
            return {"type": "tool_result", "tool_use_id": tool_use_id,
                    "content": truncate(str(result), MAX_RESULT_CHARS)}
        except ToolError as e:
            return {"type": "tool_result", "tool_use_id": tool_use_id, "content": f"Ошибка: {e}", "is_error": True}
        except Exception as e:  # noqa: BLE001
            log.exception("Инструмент %s упал", name)
            return {"type": "tool_result", "tool_use_id": tool_use_id,
                    "content": f"Ошибка: {type(e).__name__}: {e}", "is_error": True}

    # ------------------------------------------------------------------ помощники
    def _date(self, value: str | None, end: bool = False) -> str | None:
        if not value:
            return None
        try:
            dt = parse_local(value, self.cfg.tz)
        except ValueError as e:
            raise ToolError(f"не понял дату «{value}». {DATE_HINT}") from e
        if end and len(value.strip()) <= 10:
            dt = dt + timedelta(days=1) - timedelta(seconds=1)
        return to_db(dt)

    def _chat_ids(self, chat: str | None) -> list[int] | None:
        ids = self.db.find_chat_ids(chat)
        if ids is not None and not ids:
            raise ToolError(f"чат «{chat}» не найден в архиве. Список: list_chats")
        return ids

    def _clean(self, text: str) -> str:
        return mask_cards(text) if self.cfg.mask_cards else text

    def format_messages(self, rows) -> str:
        """Сообщения → текст для модели. Сообщения владельца подписаны «Я»."""
        titles = self.db.chat_titles()
        edits = self.db.edits_for([r["id"] for r in rows if r["edited_at"]])
        lines = []
        for r in rows:
            author = "Я" if r["outgoing"] else (r["sender_name"] or r["sender_id"] or "?")
            parts = [f"[{fmt_local(r['date'], self.cfg.tz)}]", f"«{titles.get(r['chat_id'], r['chat_id'])}»",
                     f"#{r['msg_id']}", f"{author}:"]
            body = []
            if r["fwd_from"]:
                body.append(f"(переслано от {r['fwd_from']})")
            if r["reply_to"]:
                body.append(f"(ответ на #{r['reply_to']})")
            if r["media"]:
                body.append(r["media"])
            if r["text"]:
                body.append(self._clean(r["text"]))
            old = edits.get(r["id"])
            if old:
                body.append("(изменено; было: " + " → ".join(f"«{self._clean(t)}»" for t in old) + ")")
            elif r["edited_at"]:
                body.append("(изм.)")
            if r["deleted_at"]:
                body.append(f"(УДАЛЕНО в Telegram {fmt_local(r['deleted_at'], self.cfg.tz)})")
            lines.append(" ".join(parts + body))
        return "\n".join(lines)

    # ------------------------------------------------------------------ Telegram-архив
    def t_search_messages(self, query: str, mode: str = "all", chat: str | None = None, sender: str | None = None,
                          date_from: str | None = None, date_to: str | None = None, limit: int = 40) -> str:
        outgoing = None
        if sender and sender.strip().lower() in OWNER_WORDS:
            sender, outgoing = None, True
        rows = self.db.search_messages(
            query, mode=mode, chat_ids=self._chat_ids(chat), sender=sender, outgoing=outgoing,
            date_from=self._date(date_from), date_to=self._date(date_to, end=True),
            limit=max(1, min(int(limit), 200)),
        )
        if not rows:
            return "Ничего не найдено. Попробуй другую форму слова, mode='any' или без фильтров."
        return f"Найдено {len(rows)} (новые сверху):\n" + self.format_messages(rows)

    def t_read_chat(self, chat: str | None = None, date_from: str | None = None, date_to: str | None = None,
                    around_message_id: int | None = None, limit: int = 100) -> str:
        rows = self.db.read_messages(
            self._chat_ids(chat), date_from=self._date(date_from), date_to=self._date(date_to, end=True),
            around=around_message_id, limit=max(1, min(int(limit), 400)),
        )
        if not rows:
            return "Сообщений за этот период нет."
        return self.format_messages(rows)

    def t_list_chats(self) -> str:
        rows = self.db.chats()
        if not rows:
            return "Архив пуст — история ещё не скачана (команда download)."
        lines = []
        for r in rows:
            mark = "" if r["monitored"] else " (не отслеживается)"
            lines.append(
                f"«{r['title']}» id={r['chat_id']} [{r['kind']}] — {r['n']} сообщ., "
                f"{fmt_local(r['first_date'], self.cfg.tz)[:10]} … {fmt_local(r['last_date'], self.cfg.tz)}{mark}"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------ Google Таблицы
    async def t_list_spreadsheets(self) -> str:
        if not self.cfg.sheets:
            return "Таблицы не настроены (раздел google.sheets в config.yaml)."
        out = []
        for s in self.cfg.sheets:
            line = f"• {s.name}" + (f" — {s.description}" if s.description else "")
            try:
                titles = await self.sheets.aworksheet_titles(s)
                line += "\n  листы: " + ", ".join(titles)
            except Exception as e:  # noqa: BLE001
                line += f"\n  (нет доступа: {e})"
            out.append(line)
        return "\n".join(out)

    async def t_read_sheet(self, spreadsheet: str, worksheet: str | None = None, query: str | None = None,
                           start_row: int = 1, max_rows: int = 400) -> str:
        try:
            source = self.sheets.find_source(spreadsheet)
        except ValueError as e:
            raise ToolError(str(e)) from e
        title, values = await self.sheets.aread(source, worksheet)
        table = format_table(values, start_row=max(1, int(start_row)), max_rows=max(1, min(int(max_rows), 2000)),
                             query=query)
        return f"Таблица «{source.name}», лист «{title}»:\n{table}"

    def t_sheet_changes(self, spreadsheet: str | None = None, since_hours: float = 24) -> str:
        sources = self.cfg.sheets
        if spreadsheet:
            try:
                sources = [self.sheets.find_source(spreadsheet)]
            except ValueError as e:
                raise ToolError(str(e)) from e
        since = utcnow() - timedelta(hours=float(since_hours))
        out = []
        rows = self.db.conn.execute(
            "SELECT DISTINCT spreadsheet, worksheet FROM sheet_snapshots"
        ).fetchall()
        for s in sources:
            for r in rows:
                if r["spreadsheet"] != s.url:
                    continue
                base = self.db.last_snapshot(s.url, r["worksheet"], before=since)
                last = self.db.last_snapshot(s.url, r["worksheet"])
                if base is None or last is None or base["id"] == last["id"]:
                    continue
                diff = diff_tables(json.loads(base["data"]), json.loads(last["data"]))
                if diff:
                    out.append(
                        f"«{s.name}» / «{r['worksheet']}» (снимки {fmt_local(base['taken_at'], self.cfg.tz)} → "
                        f"{fmt_local(last['taken_at'], self.cfg.tz)}):\n" + "\n".join(diff)
                    )
        if not out:
            return ("Изменений не найдено (или ещё нет снимков за этот период — снимки делаются "
                    "при фоновой проверке и перед отчётом).")
        return "\n\n".join(out)

    # ------------------------------------------------------------------ память
    def t_remember(self, category: str, subject: str, content: str) -> str:
        note_id = self.db.upsert_memory(category, subject, content, source=self.source)
        return f"Сохранено в память (#{note_id})."

    def t_forget(self, note_id: int) -> str:
        return "Заметка удалена." if self.db.delete_memory(int(note_id)) else "Заметка с таким номером не найдена."

    def t_search_memory(self, query: str) -> str:
        rows = self.db.search_memory(query)
        if not rows:
            return "В памяти ничего не найдено."
        return "\n".join(f"#{r['id']} [{r['category']}] {r['subject']}: {r['content']}" for r in rows)

    # ------------------------------------------------------------------ напоминания
    def t_create_reminder(self, text: str, at: str | None = None, in_minutes: int | None = None,
                          cron: str | None = None) -> str:
        given = [x for x in (at, in_minutes, cron) if x is not None]
        if len(given) != 1:
            raise ToolError("укажи ровно одно из: at, in_minutes, cron")
        now_local = datetime.now(self.cfg.tz)
        if cron:
            if not croniter.is_valid(cron):
                raise ToolError(f"неверное cron-выражение «{cron}»")
            due = croniter(cron, now_local).get_next(datetime)
        elif in_minutes is not None:
            due = now_local + timedelta(minutes=int(in_minutes))
        else:
            try:
                due = parse_local(str(at), self.cfg.tz)
            except ValueError as e:
                raise ToolError(f"не понял время «{at}». {DATE_HINT}") from e
            if due < now_local - timedelta(minutes=1):
                raise ToolError(f"время {due:%Y-%m-%d %H:%M} уже прошло (сейчас {now_local:%Y-%m-%d %H:%M})")
        rid = self.db.add_reminder(text.strip(), due, cron, self.source)
        kind = f"повтор по расписанию «{cron}», ближайшее" if cron else "сработает"
        return f"Напоминание #{rid} создано: {kind} {fmt_local(due, self.cfg.tz)}."

    def t_list_reminders(self) -> str:
        rows = self.db.active_reminders()
        if not rows:
            return "Активных напоминаний нет."
        lines = []
        for r in rows:
            rep = f" (повтор: {r['cron']})" if r["cron"] else ""
            lines.append(f"#{r['id']} {fmt_local(r['due_at'], self.cfg.tz)}{rep} — {r['text']}")
        return "\n".join(lines)

    def t_cancel_reminder(self, reminder_id: int) -> str:
        ok = self.db.cancel_reminder(int(reminder_id))
        return "Напоминание отменено." if ok else "Активного напоминания с таким номером нет."

    # ------------------------------------------------------------------ отчёты
    def t_get_reports(self, days: int = 3, kind: str = "daily") -> str:
        since = utcnow() - timedelta(days=max(1, int(days)))
        rows = self.db.reports_since(since, None if kind == "all" else kind)
        if not rows:
            return "Отчётов за этот период нет."
        return "\n\n".join(
            f"=== {r['kind']} {fmt_local(r['created_at'], self.cfg.tz)} ===\n{r['content']}" for r in rows
        )

    def t_search_conversations(self, query: str, limit: int = 20) -> str:
        rows = self.db.search_conversations(query, limit=max(1, min(int(limit), 100)))
        if not rows:
            return "В прошлых разговорах и отчётах ничего не найдено."
        names = {"user": "Владелец", "assistant": "Ассистент", "daily": "Отчёт", "watch": "Уведомление"}
        return "\n\n".join(
            f"[{fmt_local(r['created_at'], self.cfg.tz)}] {names.get(r['kind'], r['kind'])}: "
            f"{truncate(r['content'], 3000)}"
            for r in rows
        )
