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
from .telegram_archive import ChatFilter
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


def _unlearned(db, chat_id: int) -> list:
    return [r for r in db.unlearned_messages(chat_id) if r["text"] or r["media"]]


def build_plan(asst: Assistant, selected: set[int] | None) -> list[Chunk]:
    cfg, db = asst.cfg, asst.db
    chunks: list[Chunk] = []
    cur = Chunk()
    for chat in sorted(db.chats(), key=lambda c: (c["first_date"] or "", c["chat_id"])):
        if not chat["monitored"]:
            continue  # чат исключён из анализа
        if selected is not None and chat["chat_id"] not in selected:
            continue
        for r in _unlearned(db, chat["chat_id"]):
            line = _line(r, cfg.tz, cfg.mask_cards)
            if cur.lines and cur.size + len(line) > cfg.learn_chunk_chars:
                chunks.append(cur)
                cur = Chunk()
            cur.add(chat["chat_id"], chat["title"] or str(chat["chat_id"]), chat["kind"] or "?", r, line)
    if cur.lines:
        chunks.append(cur)
    return chunks


def nightly_scope(asst: Assistant) -> tuple[set[int], list[tuple[str, int, int]]]:
    """Что можно изучать ночью без подтверждения.

    • Чаты, которые владелец одобрил ручным learn, — всё новое в них.
    • Небольшие чаты, появившиеся в архиве ПОСЛЕ первого ручного learn (кто-то новый написал),
      если их неизученное помещается в одну часть; суммарно на такие чаты — не больше одной части за ночь.
    Всё остальное (чаты, которые владелец не выбирал, крупная история) откладывается до ручного learn и
    возвращается списком (название, сообщений, символов) — чтобы сообщить владельцу.
    """
    cfg, db = asst.cfg, asst.db
    eligible: set[int] = set()
    deferred: list[tuple[str, int, int]] = []
    consent_at = db.get_kv("learn_started_at")
    budget = cfg.learn_chunk_chars
    for chat in db.chats():
        if not chat["monitored"]:
            continue
        if db.get_kv(f"learn_cursor:{chat['chat_id']}") is not None:
            eligible.add(chat["chat_id"])
            continue
        rows = _unlearned(db, chat["chat_id"])
        if not rows:
            continue
        chars = sum(len(_line(r, cfg.tz, False)) + 1 for r in rows)
        # «новый» — переписка началась после согласия (а не просто попала в архив позже)
        new_since_consent = bool(consent_at and chat["first_date"] and chat["first_date"] > consent_at)
        downloaded = chat["last_sync"] is not None
        if new_since_consent and downloaded and chars <= budget:
            eligible.add(chat["chat_id"])
            budget -= chars
        else:
            deferred.append((chat["title"] or str(chat["chat_id"]), len(rows), chars))
    return eligible, deferred


def _confirm(question: str) -> bool:
    try:
        return input(question).strip().lower() in ("y", "yes", "д", "да")
    except EOFError:
        return False


async def learn_history(asst: Assistant, chat_filter: list[str] | None, model: str, yes: bool,
                        say: Say = print, max_chunks: int | None = None,
                        only_chats: set[int] | None = None, approve: bool = True) -> bool:
    """approve=True — это ручной learn: изученные чаты считаются одобренными владельцем для ночного изучения.
    Ночной запуск передаёт approve=False и одобрений не выдаёт."""
    cfg, db = asst.cfg, asst.db
    selected = None
    if chat_filter:
        selected = set()
        for spec in chat_filter:
            selected.update(db.find_chat_ids(spec) or [])
    if only_chats is not None:
        selected = only_chats if selected is None else selected & only_chats

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
    if approve and db.get_kv("learn_started_at") is None:
        db.set_kv("learn_started_at", to_db(utcnow()))  # с этого момента новые чаты — «новые»

    for idx, chunk in enumerate(plan, 1):
        period = chunk.period(cfg.tz)
        say(f"[{idx}/{len(plan)}] {chunk.label()} {period} ({chunk.count} сообщ.)…")
        chunk_text = "\n".join(chunk.lines).strip()
        # заметки о людях и темах из этого куска — в первую очередь, чтобы их дополнять, а не перезаписывать
        prompt = LEARN_CHUNK_PROMPT.format(period=period, notes=asst.memory_text(focus=chunk_text), chunk=chunk_text)
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
            db.add_learn_note(only, period, data["summary"], chat_ids=list(chunk.rows))
        for chat_id, rows in chunk.rows.items():
            db.mark_learned([r["id"] for r in rows])
            if approve:
                db.set_kv(f"learn_cursor:{chat_id}", "1")  # одобрен владельцем — ночью изучается без вопросов
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
    monitored = db.monitored_chat_ids()
    any_hidden = any(not c["monitored"] for c in db.chats())

    def visible(n) -> bool:
        # изложения исключённых чатов в профиль не попадают
        if n["chat_id"] is not None:
            return n["chat_id"] in monitored
        if not n["chat_ids"]:
            return not any_hidden  # старое изложение нескольких чатов без списка — если есть исключения, пропускаем
        ids = [int(x) for x in n["chat_ids"].split(",") if x.strip().lstrip("-").isdigit()]
        return all(i in monitored for i in ids)

    notes = [n for n in db.learn_notes() if visible(n)]
    if not notes and not db.memory_count():
        say("Нечего обобщать — сначала скачай историю (download).")
        return False
    # если всё не помещается — отбрасываем самые старые изложения (по дате конца периода), а не целые чаты
    def period_end(n) -> str:
        return (n["period"] or "").split("—")[-1].strip()

    notes = sorted(notes, key=lambda n: (period_end(n), n["id"]))
    dropped, size = 0, sum(len(n["summary"]) + 40 for n in notes)
    while notes and size > PROFILE_INPUT_CHARS:
        size -= len(notes[0]["summary"]) + 40
        notes = notes[1:]
        dropped += 1
    by_chat: dict = {}
    for n in notes:
        by_chat.setdefault(n["chat_id"], []).append(f"[{n['period']}] {n['summary']}")
    summaries = "\n\n".join(
        (f"### Чат «{titles.get(cid, cid)}»" if cid is not None else "### Несколько небольших чатов")
        + "\n" + "\n".join(items)
        for cid, items in by_chat.items()
    )
    if dropped:
        summaries = f"…(пропущено {dropped} самых старых изложений — не поместились)\n\n" + summaries
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
    # исключения из config.yaml применяем и к уже скачанному — исключённое не уходит в Claude
    ChatFilter(asst.cfg, asst.db).apply_to_db(asst.db)
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
