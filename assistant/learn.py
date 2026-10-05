"""Изучение истории: ассистент читает весь архив переписки и таблицы и составляет память и профиль работы."""

from __future__ import annotations

import logging

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
from .util import fmt_local, truncate

log = logging.getLogger(__name__)

PROFILE_INPUT_CHARS = 600_000


def _line(row, tz) -> str:
    who = row["sender_name"] or row["sender_id"] or "?"
    extra = []
    if row["fwd_from"]:
        extra.append(f"(переслано от {row['fwd_from']})")
    if row["media"]:
        extra.append(row["media"])
    text = (row["text"] or "").strip()
    if len(text) > 4000:
        text = text[:4000] + "…"
    return f"[{fmt_local(row['date'], tz)}] {who}: " + " ".join(extra + [text]).strip()


def _chunks(rows, tz, limit: int):
    chunk, size = [], 0
    for r in rows:
        line = _line(r, tz)
        if chunk and size + len(line) > limit:
            yield chunk
            chunk, size = [], 0
        chunk.append((r, line))
        size += len(line) + 1
    if chunk:
        yield chunk


def _confirm(question: str) -> bool:
    try:
        return input(question).strip().lower() in ("y", "yes", "д", "да")
    except EOFError:
        return False


async def learn_history(asst: Assistant, chat_filter: list[str] | None, model: str, yes: bool) -> bool:
    cfg, db = asst.cfg, asst.db
    selected = None
    if chat_filter:
        selected = set()
        for spec in chat_filter:
            selected.update(db.find_chat_ids(spec) or [])

    plan = []
    for chat in db.chats():
        if selected is not None and chat["chat_id"] not in selected:
            continue
        cursor = int(db.get_kv(f"learn_cursor:{chat['chat_id']}") or 0)
        rows = [r for r in db.chat_messages_for_learning(chat["chat_id"], cursor) if r["text"] or r["media"]]
        for chunk in _chunks(rows, cfg.tz, cfg.learn_chunk_chars):
            plan.append((chat, chunk))

    if not plan:
        print("Новых сообщений для изучения нет.")
        return True

    chars = sum(len(line) for _, chunk in plan for _, line in chunk)
    messages = sum(len(chunk) for _, chunk in plan)
    # кириллица ≈ 2.5 символа на токен; плюс заметки памяти в каждом запросе и размышления модели
    est_in = chars / 2.5 + len(plan) * 12000
    est_out = len(plan) * 5000
    cost = estimate_cost(model, int(est_in), int(est_out))
    print(f"К изучению: {messages} сообщений, {len(plan)} частей, модель {model}.")
    print(f"Примерная стоимость: ≈ ${cost:.2f} (оценка грубая, реальная обычно ниже за счёт кеша).")
    if not yes and not _confirm("Продолжить? [y/N] "):
        print("Отменено.")
        return False

    for idx, (chat, chunk) in enumerate(plan, 1):
        rows = [r for r, _ in chunk]
        period = f"{fmt_local(rows[0]['date'], cfg.tz)[:10]} — {fmt_local(rows[-1]['date'], cfg.tz)[:10]}"
        print(f"[{idx}/{len(plan)}] «{chat['title']}» {period} ({len(rows)} сообщ.)…", flush=True)
        prompt = LEARN_CHUNK_PROMPT.format(
            chat=chat["title"], period=period, notes=asst.memory_text(),
            chunk="\n".join(line for _, line in chunk),
        )
        try:
            data = await asst.llm.ask_json(
                purpose="learn", system=LEARN_SYSTEM, prompt=prompt, schema=LEARN_SCHEMA,
                effort=cfg.effort_learn, model=model,
            )
        except LLMError as e:
            print(f"⚠️ {e}\nПрогресс сохранён — запусти learn ещё раз, чтобы продолжить с этого места.")
            return False
        for fact in data.get("facts", []):
            if fact.get("subject") and fact.get("content"):
                db.upsert_memory(fact.get("category") or "прочее", fact["subject"], fact["content"], source="learn")
        if data.get("summary"):
            db.add_learn_note(chat["chat_id"], period, data["summary"])
        db.set_kv(f"learn_cursor:{chat['chat_id']}", str(max(r["id"] for r in rows)))
        print(f"    + {len(data.get('facts', []))} заметок, всего в памяти {db.memory_count()}")
    return True


async def learn_sheets(asst: Assistant, model: str) -> None:
    cfg = asst.cfg
    if not cfg.sheets:
        return
    for source in cfg.sheets:
        print(f"Разбираю таблицу «{source.name}»…", flush=True)
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
            print(f"⚠️ Не удалось прочитать «{source.name}»: {e}")
            continue
        prompt = (
            f"Таблица «{source.name}»." + (f" Пояснение владельца: {source.description}" if source.description else "")
            + "\n\n" + truncate("\n\n".join(parts), 150000)
        )
        try:
            text = await asst.llm.ask_text(purpose="learn", system=SHEETS_SYSTEM, prompt=prompt,
                                           effort=cfg.effort_learn, model=model, max_tokens=16000)
        except LLMError as e:
            print(f"⚠️ {e}")
            continue
        asst.db.upsert_memory("таблица", f"Таблица: {source.name}", text, source="learn")
        print("    структура таблицы сохранена в память")


async def build_profile(asst: Assistant, model: str) -> None:
    cfg, db = asst.cfg, asst.db
    titles = db.chat_titles()
    notes = db.learn_notes()
    if not notes and not db.memory_count():
        print("Нечего обобщать — сначала скачай историю (download).")
        return
    by_chat: dict = {}
    for n in notes:
        by_chat.setdefault(n["chat_id"], []).append(f"[{n['period']}] {n['summary']}")
    summaries = "\n\n".join(
        f"### Чат «{titles.get(cid, cid)}»\n" + "\n".join(items) for cid, items in by_chat.items()
    )
    if len(summaries) > PROFILE_INPUT_CHARS:
        summaries = "…(самые ранние периоды пропущены)\n" + summaries[-PROFILE_INPUT_CHARS:]
    prompt = (
        f"# Изложение переписки по периодам\n{summaries or '(нет)'}\n\n"
        f"# Заметки памяти\n{asst.memory_text()}\n\n"
        "# Таблицы\n" + ("\n".join(f"• {s.name}: {s.description}" for s in cfg.sheets) or "(не настроены)")
    )
    print("Составляю профиль работы…", flush=True)
    try:
        profile = await asst.llm.ask_text(purpose="learn", system=PROFILE_SYSTEM, prompt=prompt,
                                          effort=cfg.effort_learn, model=model)
    except LLMError as e:
        print(f"⚠️ {e}")
        return
    asst.save_profile(profile)
    print(f"✅ Профиль работы сохранён: {asst.profile_path}")


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
        print(f"Профиль работы (можно читать и править в блокноте): {asst.profile_path}")
