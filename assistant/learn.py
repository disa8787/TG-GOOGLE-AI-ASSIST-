"""Изучение истории: ассистент читает весь архив переписки и таблицы и составляет память и профиль."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from .brain import Assistant
from .llm import LLMError, estimate_cost
from .prompts import (
    LEARN_CHUNK_PROMPT,
    LEARN_SCHEMA,
    LEARN_SYSTEM,
    PROFILE_SYSTEM,
    SHEETS_SYSTEM,
)
from .sheets import format_table, normalize
from .util import fmt_local, mask_cards, to_db, truncate, utcnow

log = logging.getLogger(__name__)

PROFILE_INPUT_CHARS = 600_000
MAX_MESSAGE_CHARS = 4000

Say = Callable[[str], None]


def _line(row, tz, mask: bool) -> str:
    who = "Я" if row["outgoing"] else (row["sender_name"] or row["sender_id"] or "?")
    extra = []
    if row["fwd_from"]:
        extra.append(f"(переслано от {row['fwd_from']})")
    if row["media"]:
        extra.append(row["media"])
    text = (row["text"] or "").strip()
    if len(text) > MAX_MESSAGE_CHARS:
        text = text[:MAX_MESSAGE_CHARS] + "…"
    if mask:
        text = mask_cards(text)
    if row["deleted_at"]:
        text += " (удалено)"
    return f"[{fmt_local(row['date'], tz)}] {who}: " + " ".join(extra + [text]).strip()


@dataclass
class Chunk:
    """Кусок истории для одного запроса. Мелкие чаты упаковываются вместе — так дешевле."""

    lines: list[str] = field(default_factory=list)
    size: int = 0
    rows: dict[int, list] = field(default_factory=dict)  # chat_id → сообщения
    titles: dict[int, str] = field(default_factory=dict)
    current_chat: int | None = None

    def add(self, chat_id: int, title: str, kind: str, row, line: str) -> None:
        if self.current_chat != chat_id:
            header = f"\n=== Чат «{title}» ({kind}) ==="
            self.lines.append(header)
            self.size += len(header) + 1
            self.current_chat = chat_id
            self.titles[chat_id] = title
        self.lines.append(line)
        self.size += len(line) + 1
        self.rows.setdefault(chat_id, []).append(row)

    @property
    def count(self) -> int:
        return sum(len(r) for r in self.rows.values())

    def period(self, tz) -> str:
        dates = [r["date"] for rows in self.rows.values() for r in rows]
        return f"{fmt_local(min(dates), tz)[:10]} — {fmt_local(max(dates), tz)[:10]}"

    def label(self) -> str:
        names = [f"«{t}»" for t in self.titles.values()]
        if len(names) <= 3:
            return ", ".join(names)
        return ", ".join(names[:3]) + f" и ещё {len(names) - 3} чатов"


def build_plan(asst: Assistant, selected: set[int] | None) -> list[Chunk]:
    cfg, db = asst.cfg, asst.db
    chunks: list[Chunk] = []
    cur = Chunk()
    for chat in sorted(db.chats(), key=lambda c: (c["first_date"] or "", c["chat_id"])):
        if not chat["monitored"]:
            continue  # чат исключён из анализа
        if selected is not None and chat["chat_id"] not in selected:
            continue
        cursor = int(db.get_kv(f"learn_cursor:{chat['chat_id']}") or 0)
        rows = [r for r in db.chat_messages_for_learning(chat["chat_id"], cursor) if r["text"] or r["media"]]
        for r in rows:
            line = _line(r, cfg.tz, cfg.mask_cards)
            if cur.lines and cur.size + len(line) > cfg.learn_chunk_chars:
                chunks.append(cur)
                cur = Chunk()
            cur.add(chat["chat_id"], chat["title"] or str(chat["chat_id"]), chat["kind"] or "?", r, line)
    if cur.lines:
        chunks.append(cur)
    return chunks


def _confirm(question: str) -> bool:
    try:
        return input(question).strip().lower() in ("y", "yes", "д", "да")
    except EOFError:
        return False


async def learn_history(asst: Assistant, chat_filter: list[str] | None, model: str, yes: bool,
                        say: Say = print, max_chunks: int | None = None) -> bool:
    cfg, db = asst.cfg, asst.db
    selected = None
    if chat_filter:
        selected = set()
        for spec in chat_filter:
            selected.update(db.find_chat_ids(spec) or [])

    plan = build_plan(asst, selected)
    if not plan:
        say("Новых сообщений для изучения нет.")
        return True
    if max_chunks is not None and len(plan) > max_chunks:
        say(f"Новых данных много ({len(plan)} частей) — сейчас изучаю {max_chunks}, остальное в следующие разы.")
        plan = plan[:max_chunks]

    chars = sum(c.size for c in plan)
    messages = sum(c.count for c in plan)
    chats = len({cid for c in plan for cid in c.rows})
    # кириллица ≈ 2.5 символа на токен; плюс заметки памяти в каждом запросе и размышления модели
    est_in = chars / 2.5 + len(plan) * 12000
    est_out = len(plan) * 5000
    cost = estimate_cost(model, int(est_in), int(est_out))
    say(f"К изучению: {messages} сообщений из {chats} чатов, {len(plan)} частей, модель {model}.")
    say(f"Примерная стоимость: ≈ ${cost:.2f} (оценка грубая, реальная обычно ниже).")
    if not yes and not _confirm("Продолжить? [y/N] "):
        say("Отменено.")
        return False

    for idx, chunk in enumerate(plan, 1):
        period = chunk.period(cfg.tz)
        say(f"[{idx}/{len(plan)}] {chunk.label()} {period} ({chunk.count} сообщ.)…")
        prompt = LEARN_CHUNK_PROMPT.format(
            period=period, notes=asst.memory_text(), chunk="\n".join(chunk.lines).strip(),
        )
        try:
            data = await asst.llm.ask_json(
                purpose="learn", system=LEARN_SYSTEM, prompt=prompt, schema=LEARN_SCHEMA,
                effort=cfg.effort_learn, model=model,
            )
        except LLMError as e:
            say(f"⚠️ {e}\nПрогресс сохранён — запусти learn ещё раз, чтобы продолжить с этого места.")
            return False
        facts = [f for f in data.get("facts", []) if f.get("subject") and f.get("content")]
        for fact in facts:
            db.upsert_memory(fact.get("category") or "прочее", fact["subject"], fact["content"], source="learn")
        if data.get("summary"):
            only = next(iter(chunk.rows)) if len(chunk.rows) == 1 else None
            db.add_learn_note(only, period, data["summary"])
        for chat_id, rows in chunk.rows.items():
            db.set_kv(f"learn_cursor:{chat_id}", str(max(r["id"] for r in rows)))
        say(f"    + {len(facts)} заметок, всего в памяти {db.memory_count()}")
    return True


async def learn_sheets(asst: Assistant, model: str, say: Say = print) -> None:
    cfg = asst.cfg
    if not cfg.sheets:
        return
    for source in cfg.sheets:
        say(f"Разбираю таблицу «{source.name}»…")
        try:
            titles = await asst.sheets.aworksheet_titles(source)
            parts = []
            for title in titles[:12]:
                _, values = await asst.sheets.aread(source, title)
                values = normalize(values)
                head = format_table(values[:40], max_rows=40)
                tail = ""
                if len(values) > 55:
                    tail = "\n…\n" + format_table(values, start_row=len(values) - 14, max_rows=15)
                parts.append(f"## Лист «{title}» (строк: {len(values)})\n{head}{tail}")
        except Exception as e:  # noqa: BLE001
            say(f"⚠️ Не удалось прочитать «{source.name}»: {e}")
            continue
        prompt = (
            f"Таблица «{source.name}»." + (f" Пояснение владельца: {source.description}" if source.description else "")
            + "\n\n" + truncate("\n\n".join(parts), 150000)
        )
        try:
            text = await asst.llm.ask_text(purpose="learn", system=SHEETS_SYSTEM, prompt=prompt,
                                           effort=cfg.effort_learn, model=model, max_tokens=16000)
        except LLMError as e:
            say(f"⚠️ {e}")
            continue
        asst.db.upsert_memory("таблица", f"Таблица: {source.name}", text, source="learn")
        say("    структура таблицы сохранена в память")


async def build_profile(asst: Assistant, model: str, say: Say = print) -> bool:
    cfg, db = asst.cfg, asst.db
    titles = db.chat_titles()
    notes = db.learn_notes()
    if not notes and not db.memory_count():
        say("Нечего обобщать — сначала скачай историю (download).")
        return False
    by_chat: dict = {}
    for n in notes:
        by_chat.setdefault(n["chat_id"], []).append(f"[{n['period']}] {n['summary']}")
    summaries = "\n\n".join(
        (f"### Чат «{titles.get(cid, cid)}»" if cid is not None else "### Несколько небольших чатов")
        + "\n" + "\n".join(items)
        for cid, items in by_chat.items()
    )
    if len(summaries) > PROFILE_INPUT_CHARS:
        summaries = "…(самые ранние периоды пропущены)\n" + summaries[-PROFILE_INPUT_CHARS:]
    prompt = (
        f"# Владелец\n{db.get_kv('owner_name') or 'неизвестно'} (в переписке — «Я»)\n\n"
        f"# Изложение переписки по чатам и периодам\n{summaries or '(нет)'}\n\n"
        f"# Заметки памяти\n{asst.memory_text()}\n\n"
        "# Таблицы\n" + ("\n".join(f"• {s.name}: {s.description}" for s in cfg.sheets) or "(не настроены)")
    )
    say("Составляю профиль…")
    try:
        profile = await asst.llm.ask_text(purpose="learn", system=PROFILE_SYSTEM, prompt=prompt,
                                          effort=cfg.effort_learn, model=model)
    except LLMError as e:
        say(f"⚠️ {e}")
        return False
    asst.save_profile(profile)
    db.set_kv("profile_built_at", to_db(utcnow()))
    say(f"✅ Профиль сохранён: {asst.profile_path}")
    return True


async def run_learn(asst: Assistant, *, chats: list[str] | None, reset: bool, yes: bool,
                    sheets_only: bool, model: str | None) -> None:
    model = model or asst.cfg.model
    if reset:
        asst.db.reset_learning()
        print("Прогресс изучения сброшен — история будет изучена заново (память сохраняется).")
    if not sheets_only:
        ok = await learn_history(asst, chats, model, yes)
        if not ok:
            return
    await learn_sheets(asst, model)
    await build_profile(asst, model)
    print(f"\nГотово. В памяти {asst.db.memory_count()} заметок.")
    if asst.profile():
        print(f"Профиль (можно читать и править в блокноте): {asst.profile_path}")
