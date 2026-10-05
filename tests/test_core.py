"""Тесты основной логики без сети: архив и поиск, таблицы, инструменты, напоминания, цикл агента.

Запуск:  python -m unittest discover -s tests
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from assistant.brain import Assistant
from assistant.config import load_config
from assistant.jobs import Scheduler
from assistant.learn import build_profile, learn_history
from assistant.sheets import diff_tables, format_table
from assistant.util import split_text, to_db, utcnow

CONFIG = """
timezone: Europe/Moscow
claude:
  model: claude-opus-5-5
"""


def make_cfg(tmp: Path):
    (tmp / "config.yaml").write_text(CONFIG, encoding="utf-8")
    return load_config(tmp)


def msg(chat_id, msg_id, text, sender="Иван", minutes_ago=0):
    return {
        "chat_id": chat_id, "msg_id": msg_id, "date": to_db(utcnow() - timedelta(minutes=minutes_ago)),
        "sender_id": 1, "sender_name": sender, "text": text,
    }


# ---------------------------------------------------------------------- фейковый Claude
class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(json.loads(json.dumps(kwargs, default=lambda o: o.__dict__)))
        return self.responses.pop(0)

    def stream(self, **kwargs):
        outer = self

        class _Stream:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get_final_message(self):
                return await outer.create(**kwargs)

        return _Stream()


def fake_client(responses):
    messages = FakeMessages(responses)
    return SimpleNamespace(beta=SimpleNamespace(messages=messages)), messages


def usage():
    return SimpleNamespace(input_tokens=100, output_tokens=50, cache_read_input_tokens=0,
                           cache_creation_input_tokens=0)


def text_resp(text):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn",
                           usage=usage(), model="claude-opus-5-5")


def tool_resp(name, args, tid="t1"):
    return SimpleNamespace(content=[SimpleNamespace(type="tool_use", id=tid, name=name, input=args)],
                           stop_reason="tool_use", usage=usage(), model="claude-opus-5-5")


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cfg = make_cfg(self.tmp)
        self.asst = Assistant(self.cfg)
        self.db = self.asst.db
        self.db.upsert_chat(-100, "Диспетчерская", None, "группа")

    def tearDown(self):
        self.db.conn.close()
        self._tmp.cleanup()

    def run_async(self, coro):
        return asyncio.run(coro)


# ---------------------------------------------------------------------- архив
class ArchiveTests(Base):
    def test_search_matches_russian_word_forms(self):
        self.db.upsert_messages([
            msg(-100, 1, "Выгрузился в Чикаго, жду документы", minutes_ago=30),
            msg(-100, 2, "Завтра погрузка в Далласе", sender="Пётр", minutes_ago=20),
        ])
        found = self.db.search_messages("выгруз")
        self.assertEqual([r["msg_id"] for r in found], [1])
        self.assertEqual(len(self.db.search_messages("пётр")), 1, "ищет и по имени автора")
        self.assertEqual(len(self.db.search_messages("чикаго даллас", mode="any")), 2)
        self.assertEqual(len(self.db.search_messages("чикаго даллас", mode="all")), 0)

    def test_edit_reindexes_text(self):
        self.db.upsert_messages([msg(-100, 1, "старый текст")])
        self.db.upsert_messages([msg(-100, 1, "новый вариант")])
        self.assertEqual(len(self.db.search_messages("старый")), 0)
        self.assertEqual(len(self.db.search_messages("вариант")), 1)
        self.assertEqual(self.db.last_msg_id(-100), 1)

    def test_read_around_and_find_chat(self):
        self.db.upsert_messages([msg(-100, i, f"сообщение {i}", minutes_ago=100 - i) for i in range(1, 21)])
        rows = self.db.read_messages([-100], around=10, limit=6)
        ids = [r["msg_id"] for r in rows]
        self.assertIn(10, ids)
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(self.db.find_chat_ids("диспетч"), [-100])
        self.assertEqual(self.db.find_chat_ids("нет такого"), [])
        self.assertIsNone(self.db.find_chat_ids(None))


# ---------------------------------------------------------------------- таблицы
class SheetTests(unittest.TestCase):
    TABLE = [
        ["Водитель", "Трак", "Выгрузка", "Дата"],
        ["Иван", "101", "Chicago, IL", "10/05"],
        ["Пётр", "202", "Dallas, TX", "10/06"],
        ["", "", "", ""],
    ]

    def test_format_and_filter(self):
        text = format_table(self.TABLE)
        self.assertIn("1: Водитель | Трак | Выгрузка | Дата", text)
        self.assertIn("3: Пётр | 202 | Dallas, TX | 10/06", text)
        filtered = format_table(self.TABLE, query="пётр")
        self.assertIn("← заголовок", filtered)
        self.assertNotIn("Иван", filtered)

    def test_pagination_hint(self):
        rows = [["Водитель"]] + [[f"В{i}"] for i in range(10)]
        text = format_table(rows, max_rows=3)
        self.assertIn("Продолжение: start_row=4", text)

    def test_diff(self):
        new = [row[:] for row in self.TABLE]
        new[1][2] = "Detroit, MI"
        new.insert(3, ["Сергей", "303", "Miami, FL", "10/07"])
        diff = diff_tables(self.TABLE, new)
        self.assertTrue(any("C: «Chicago, IL» → «Detroit, MI»" in d for d in diff), diff)
        self.assertTrue(any(d.startswith("добавлена строка 4") for d in diff), diff)


# ---------------------------------------------------------------------- инструменты
class ToolTests(Base):
    def call(self, name, args):
        return self.run_async(self.asst.tools.execute(name, args, "id1"))

    def test_reminders(self):
        r = self.call("create_reminder", {"text": "Запросить апдейт у Ивана", "in_minutes": 30})
        self.assertNotIn("is_error", r)
        r = self.call("create_reminder", {"text": "Планёрка", "cron": "0 9 * * 1-5"})
        self.assertIn("повтор", r["content"])
        r = self.call("create_reminder", {"text": "x", "at": "2001-01-01 10:00"})
        self.assertTrue(r.get("is_error"))
        r = self.call("create_reminder", {"text": "x", "at": "2099-01-01 10:00", "in_minutes": 5})
        self.assertTrue(r.get("is_error"))
        listing = self.call("list_reminders", {})["content"]
        self.assertIn("Запросить апдейт", listing)
        self.assertIn("Отменено".lower(), self.call("cancel_reminder", {"reminder_id": 1})["content"].lower())

    def test_memory(self):
        self.call("remember", {"category": "водитель", "subject": "Водитель: Иван", "content": "Трак 101"})
        self.call("remember", {"category": "водитель", "subject": "водитель:  иван", "content": "Трак 105"})
        self.assertEqual(self.db.memory_count(), 1, "тот же subject обновляет заметку")
        self.assertIn("Трак 105", self.asst.memory_text())
        self.assertIn("Трак 105", self.call("search_memory", {"query": "иван"})["content"])

    def test_errors_are_reported_to_model(self):
        self.assertTrue(self.call("nope", {}).get("is_error"))
        self.assertTrue(self.call("search_messages", {}).get("is_error"))
        self.assertTrue(self.call("read_chat", {"chat": "нет такого"}).get("is_error"))

    def test_search_tool_formats_messages(self):
        self.db.upsert_messages([msg(-100, 7, "Выгрузка задерживается на 2 часа")])
        out = self.call("search_messages", {"query": "задерж"})["content"]
        self.assertIn("«Диспетчерская» #7 Иван: Выгрузка задерживается", out)


# ---------------------------------------------------------------------- напоминания в планировщике
class SchedulerTests(Base):
    def test_due_reminders_fire_once_and_cron_reschedules(self):
        sent = []

        async def notify(text):
            sent.append(text)

        past = datetime.now(self.cfg.tz) - timedelta(minutes=1)
        self.db.add_reminder("разовое", past, None, "test")
        self.db.add_reminder("по расписанию", past, "*/5 * * * *", "test")
        sched = Scheduler(self.asst, notify, set())
        self.run_async(sched.fire_due_reminders())
        self.run_async(sched.fire_due_reminders())
        self.assertEqual(len(sent), 2)
        active = self.db.active_reminders()
        self.assertEqual([r["text"] for r in active], ["по расписанию"])
        self.assertGreater(active[0]["due_at"], to_db(utcnow()))


# ---------------------------------------------------------------------- агент
class AgentTests(Base):
    def test_tool_loop_and_request_shape(self):
        self.db.upsert_messages([msg(-100, 3, "Иван выгрузится в 15:00 в Чикаго")])
        client, fake = fake_client([
            tool_resp("search_messages", {"query": "выгруз"}),
            text_resp("Иван выгрузится в 15:00 в Чикаго."),
        ])
        self.asst.llm.client = client
        answer = self.run_async(self.asst.chat("Когда Иван выгружается?"))
        self.assertEqual(answer, "Иван выгрузится в 15:00 в Чикаго.")

        first, second = fake.calls
        self.assertEqual(first["model"], "claude-opus-5-5")
        self.assertEqual(first["thinking"], {"type": "adaptive"})
        self.assertEqual(first["output_config"], {"effort": "medium"})
        self.assertEqual(first["fallbacks"], "default")
        self.assertEqual(first["betas"], ["server-side-fallback-2026-07-01"])
        self.assertEqual(first["system"][1]["cache_control"], {"type": "ephemeral"})
        # история внутри запуска только дописывается
        self.assertEqual(second["messages"][: len(first["messages"])], first["messages"])
        result = second["messages"][-1]["content"][0]
        self.assertEqual(result["type"], "tool_result")
        self.assertIn("Чикаго", result["content"])

        # следующий вопрос видит прошлый диалог
        client2, fake2 = fake_client([text_resp("Пожалуйста.")])
        self.asst.llm.client = client2
        self.run_async(self.asst.chat("Спасибо"))
        roles = [m["role"] for m in fake2.calls[0]["messages"]]
        self.assertEqual(roles, ["user", "assistant", "user"])
        self.assertGreater(self.db.usage_since(utcnow() - timedelta(minutes=5))[0]["calls"], 0)

    def test_step_limit_forces_final_answer(self):
        self.cfg.max_tool_steps = 2
        client, fake = fake_client([
            tool_resp("list_chats", {}, "a"), tool_resp("list_chats", {}, "b"), text_resp("итог"),
        ])
        self.asst.llm.client = client
        self.assertEqual(self.run_async(self.asst.chat("?")), "итог")
        self.assertEqual(fake.calls[-1]["tool_choice"], {"type": "none"})

    def test_watch_check(self):
        client, fake = fake_client([text_resp("NOTHING"), text_resp("Иван сообщил о поломке.")])
        self.asst.llm.client = client
        self.assertIsNone(self.run_async(self.asst.watch_check()), "первый запуск только ставит отметку")
        self.assertEqual(len(fake.calls), 0)
        self.db.upsert_messages([msg(-100, 10, "Привет")])
        self.assertIsNone(self.run_async(self.asst.watch_check()))
        self.db.upsert_messages([msg(-100, 11, "Сломался трак")])
        self.assertEqual(self.run_async(self.asst.watch_check()), "Иван сообщил о поломке.")
        self.assertIn("Сломался трак", fake.calls[-1]["messages"][0]["content"])
        self.assertIsNone(self.run_async(self.asst.watch_check()), "без новых данных Claude не вызывается")
        self.assertEqual(len(fake.calls), 2)

    def test_learn_and_profile(self):
        self.db.upsert_messages([msg(-100, i, f"Груз {i} для брокера ABC", minutes_ago=50 - i) for i in range(5)])
        facts = {"summary": "Обсуждали грузы брокера ABC", "facts": [
            {"category": "брокер", "subject": "Брокер: ABC", "content": "Основной брокер"}]}
        client, fake = fake_client([text_resp(json.dumps(facts, ensure_ascii=False)), text_resp("# Профиль")])
        self.asst.llm.client = client
        self.assertTrue(self.run_async(learn_history(self.asst, None, "claude-opus-5-5", yes=True)))
        self.assertEqual(self.db.memory_count(), 1)
        self.assertEqual(fake.calls[0]["output_config"]["format"]["type"], "json_schema")
        self.run_async(build_profile(self.asst, "claude-opus-5-5"))
        self.assertEqual(self.asst.profile(), "# Профиль")
        self.assertIn("# Профиль", self.asst.knowledge_text())
        # повторный запуск не изучает то же самое
        self.assertTrue(self.run_async(learn_history(self.asst, None, "claude-opus-5-5", yes=True)))
        self.assertEqual(len(fake.calls), 2)


class ConfigTests(unittest.TestCase):
    def test_unquoted_time_and_bom(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            text = "timezone: Europe/Moscow\nreport:\n  time: 8:30\nwatch:\n  active_hours: 7:00-22:00\n"
            (root / "config.yaml").write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
            cfg = load_config(root)
            self.assertEqual(cfg.report_time, "08:30")
            self.assertEqual(cfg.watch_hours, ("7:00", "22:00"))
            self.assertEqual(str(cfg.tz), "Europe/Moscow")


class TruncationTests(Base):
    def test_truncated_tool_call_is_not_executed(self):
        resp = tool_resp("remember", {"category": "x"})
        resp.stop_reason = "max_tokens"
        client, fake = fake_client([resp])
        self.asst.llm.client = client
        answer = self.run_async(self.asst.chat("?"))
        self.assertIn("не поместился", answer)
        self.assertEqual(self.db.memory_count(), 0)
        self.assertEqual(len(fake.calls), 1)


class UtilTests(unittest.TestCase):
    def test_split_text(self):
        text = "\n\n".join("абзац " * 200 for _ in range(10))
        parts = split_text(text, 4000)
        self.assertTrue(all(len(p) <= 4000 for p in parts))
        self.assertEqual("".join(parts).replace("\n", "").replace(" ", ""), text.replace("\n", "").replace(" ", ""))


if __name__ == "__main__":
    unittest.main()
