"""Проактивный помощник: предлагает решения, разбирает проблемы (/advice), ищет в интернете —
и при этом по-прежнему ничего не отправляет от имени владельца."""

from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest import mock

import assistant.bot as bot_mod
from assistant import prompts
from assistant.brain import Assistant
from assistant.config import load_config
from assistant.llm import LLMError, usage_cost
from tests.test_core import fake_client, text_resp
from tests.test_review_fixes import FakeTG, group, tl_msg
from tests.test_robustness import make_root
from tests.test_startup_flow import FakeBot

QUIET = "timezone: UTC\nwatch:\n  enabled: false\nreport:\n  enabled: false\nlearn:\n  nightly: false\n"


def tool_names(call: dict) -> list[str]:
    return [t.get("name") for t in call.get("tools") or []]


class PromptTests(unittest.TestCase):
    def test_assistant_solves_problems_but_never_sends(self):
        text = prompts.SYSTEM_BASE
        for phrase in ("Решай проблемы", "свою рекомендацию", "Будь проактивным", "web_search",
                       "собственные знания"):
            self.assertIn(phrase, text)
        self.assertIn("ничего не отправляешь от имени владельца", text, "Telegram — по-прежнему только чтение")
        self.assertIn("Никогда не утверждай, что что-то отправил", text)
        self.assertIn("решение", prompts.REPORT_REQUEST)
        self.assertIn("рекомендация", prompts.WATCH_REQUEST)
        self.assertIn("NOTHING", prompts.WATCH_REQUEST, "без повода фоновая проверка по-прежнему молчит")


class ConfigTests(unittest.TestCase):
    def test_old_default_step_limit_is_raised_but_own_value_kept(self):
        self.assertEqual(load_config(make_root("timezone: UTC\nclaude:\n  max_tool_steps: 30\n")).max_tool_steps, 50)
        self.assertEqual(load_config(make_root("timezone: UTC\nclaude:\n  max_tool_steps: 40\n")).max_tool_steps, 40)
        cfg = load_config(make_root("timezone: UTC\n"))
        self.assertTrue(cfg.web_search)
        self.assertEqual(cfg.web_search_max_uses, 5)
        self.assertFalse(load_config(make_root("timezone: UTC\nclaude:\n  web_search: нет\n")).web_search)

    def test_web_search_is_counted_in_costs(self):
        base = SimpleNamespace(input_tokens=0, output_tokens=0, cache_read_input_tokens=0,
                               cache_creation_input_tokens=0)
        with_search = SimpleNamespace(**vars(base), server_tool_use=SimpleNamespace(web_search_requests=3))
        self.assertAlmostEqual(usage_cost("claude-opus-5-5", with_search) - usage_cost("claude-opus-5-5", base), 0.03)


class WebSearchTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(make_root(QUIET))
        self.asst = Assistant(self.cfg)
        self.addCleanup(self.asst.db.conn.close)

    def test_chat_can_search_the_web_background_check_cannot(self):
        client, fake = fake_client([text_resp("Снег на I-80, лучше через I-70.")])
        self.asst.llm.client = client
        answer = asyncio.run(self.asst.chat("Как лучше ехать из Денвера в Чикаго?"))
        self.assertIn("I-70", answer)
        web = [t for t in fake.calls[0]["tools"] if t.get("name") == "web_search"]
        self.assertEqual(web, [{"type": "web_search_20260209", "name": "web_search", "max_uses": 5}])
        self.assertIn("search_messages", tool_names(fake.calls[0]), "свои инструменты тоже на месте")

        self.asst.db.upsert_chat(1, "Диспетчерская", None, "группа")
        self.asst.db.upsert_messages([{"chat_id": 1, "msg_id": 1, "date": "2026-11-15 10:00:00", "sender_id": 5,
                                       "sender_name": "Иван", "text": "колесо пробил"}])
        self.asst.db.set_kv("watch_last_rowid", "0")
        client, fake = fake_client([text_resp("NOTHING")])
        self.asst.llm.client = client
        asyncio.run(self.asst.watch_check())
        self.assertNotIn("web_search", tool_names(fake.calls[0]), "фоновая проверка раз в час — без платного поиска")

    def test_switched_off_in_config(self):
        self.cfg.web_search = False
        asst = Assistant(self.cfg)
        self.addCleanup(asst.db.conn.close)
        client, fake = fake_client([text_resp("ок")])
        asst.llm.client = client
        asyncio.run(asst.chat("привет"))
        self.assertNotIn("web_search", tool_names(fake.calls[0]))

    def test_unavailable_search_falls_back_to_working_without_it(self):
        llm = self.asst.llm
        real_call = llm.call
        seen = []

        async def call(**kwargs):
            names = [t.get("name") for t in kwargs.get("tools") or []]
            seen.append(names)
            if "web_search" in names:
                raise LLMError("Claude: ошибка запроса — web search is not enabled for this organization")
            return await real_call(**kwargs)

        client, _ = fake_client([text_resp("Отвечаю без интернета"), text_resp("И снова без интернета")])
        llm.client = client
        with mock.patch.object(llm, "call", call):
            self.assertEqual(asyncio.run(self.asst.chat("погода в Далласе?")), "Отвечаю без интернета")
            self.assertEqual(asyncio.run(self.asst.chat("а завтра?")), "И снова без интернета")
        self.assertFalse(llm.web_available, "второй раз недоступный поиск уже не пробуем")
        self.assertEqual(sum("web_search" in s for s in seen), 1)
        self.assertIn("выключен", self.asst.status_text())


class AdviceCommandTests(unittest.TestCase):
    def test_advice_in_bot(self):
        cfg = load_config(make_root(QUIET))
        cfg.bot_token = "999:abc"
        tg = FakeTG()
        tg.add(group(1, "Диспетчерская"), [tl_msg(1, 1, "груз")])

        async def get_me():
            from telethon.tl import types
            return types.User(id=42, first_name="Денис", is_self=True)
        tg.get_me = get_me
        tg.run_until_disconnected = lambda: asyncio.Event().wait()

        async def fake_connect(cfg_, db, interactive=False):
            return tg
        fbot = FakeBot()
        client, fake = fake_client([text_resp("1. Иван простаивает — предлагаю взять груз из Далласа.")])

        async def scenario():
            with mock.patch.object(bot_mod, "connect_user", fake_connect), \
                    mock.patch.object(bot_mod, "make_bot_client", lambda c: fbot):
                app = bot_mod.App(cfg)
                app.db.set_kv("initial_download_done", "2026-01-01 00:00:00")
                app.db.set_kv("learn_cursor:-1", "1")
                app.asst.llm.client = client
                run = asyncio.create_task(app.run())
                for _ in range(100):
                    await asyncio.sleep(0.01)
                    if app.phase == "ready":
                        break
                await fbot.incoming("/advice простои")
                app.stopping = True
                fbot.stop.set()
                await asyncio.wait_for(run, 5)
                history = app.db.recent_dialog(10)
                app.db.conn.close()
                return history
        history = asyncio.run(scenario())
        prompt = json.dumps(fake.calls[0]["messages"], ensure_ascii=False)
        self.assertIn("предложи решения", prompt)
        self.assertIn("Особое внимание: простои", prompt)
        self.assertIn("web_search", tool_names(fake.calls[0]))
        self.assertEqual(fake.calls[0]["output_config"]["effort"], cfg.effort_report, "разбор — с глубоким анализом")
        self.assertTrue(any("Проблемы и решения" in m and "Даллас" in m for m in fbot.sent))
        self.assertEqual(history[-1]["role"], "assistant", "разбор попадает в диалог — его можно продолжить")


if __name__ == "__main__":
    unittest.main()
