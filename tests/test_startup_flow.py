"""Сценарий первого запуска: вход → скачать ВСЮ историю → изучить (после согласия) → бот работает.
Telegram и Claude подменены, сеть не нужна."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import assistant.bot as bot_mod
from assistant.config import load_config
from tests.test_core import fake_client, text_resp
from tests.test_review_fixes import FakeTG, group, tl_msg


class FakeBot:
    """Бот: запоминает, что отправил владельцу; входящие сообщения подаём вручную."""

    def __init__(self):
        self.sent: list[str] = []
        self.handler = None
        self.stop = asyncio.Event()

    async def start(self, bot_token=None):
        return self

    def on(self, _event):
        def deco(fn):
            self.handler = fn
            return fn
        return deco

    async def send_message(self, to, text, parse_mode=None, link_preview=False):
        self.sent.append(text)

    def action(self, chat, kind):
        class _A:
            async def __aenter__(self_inner):
                return None

            async def __aexit__(self_inner, *exc):
                return False
        return _A()

    async def run_until_disconnected(self):
        await self.stop.wait()

    async def incoming(self, text):
        msg = SimpleNamespace(message=text, photo=None, document=None, voice=None, video_note=None, fwd_from=None,
                              sticker=None)
        await self.handler(SimpleNamespace(is_private=True, sender_id=42, message=msg, chat_id=42))


class StartupFlowTests(unittest.TestCase):
    def test_first_run_downloads_everything_then_learns_then_works(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "config.yaml").write_text(
                "timezone: UTC\ntelegram:\n  chats:\n    - \"Название рабочего чата\"\n"  # старый пример = все чаты
                "watch:\n  enabled: false\nreport:\n  enabled: false\nlearn:\n  nightly: false\n",
                encoding="utf-8")
            cfg = load_config(root)
            cfg.bot_token = "999:abc"
            self.assertTrue(cfg.all_chats, "заглушка из старого примера = все чаты")

            tg = FakeTG()
            tg.add(group(1, "Диспетчерская"), [tl_msg(1, i, f"груз {i} выгружен", minutes_ago=5000 - i)
                                               for i in range(1, 30)])
            tg.add(group(2, "Иван водитель"), [tl_msg(2, 1, "трак 101 сломался", minutes_ago=4000)])

            async def get_me():
                from telethon.tl import types
                return types.User(id=42, first_name="Денис", is_self=True)
            tg.get_me = get_me
            tg.run_until_disconnected = lambda: asyncio.Event().wait()

            async def fake_connect(cfg_, db, interactive=False):
                from assistant.telegram_archive import remember_owner
                remember_owner(db, await get_me())
                return tg

            fbot = FakeBot()
            bot_mod.connect_user = fake_connect
            bot_mod.TelegramClient = lambda *a, **k: fbot

            facts = {"summary": "Диспетчерская работа: грузы, водитель Иван", "facts": [
                {"category": "водитель", "subject": "Водитель: Иван", "content": "Трак 101"}]}
            client, fake = fake_client([text_resp(json.dumps(facts, ensure_ascii=False)),
                                        text_resp("Ты диспетчер грузоперевозок. Водитель Иван, трак 101."),
                                        text_resp("Иван — водитель, трак 101.")])

            async def scenario():
                app = bot_mod.App(cfg)
                app.asst.llm.client = client
                run = asyncio.create_task(app.run())
                # пока идёт первый этап, бот отвечает статусом, а не Claude
                for _ in range(200):
                    await asyncio.sleep(0.01)
                    if app.phase == "learn" and app.learn_decision is not None:
                        break
                self.assertEqual(app.db.message_stats()["n"], 30, "вся история скачана ДО работы")
                await fbot.incoming("где Иван?")
                self.assertIn("изучаю историю", fbot.sent[-1])
                self.assertEqual(len(fake.calls), 0, "до согласия на изучение Claude не вызывается")
                await fbot.incoming("/learn_yes")
                for _ in range(300):
                    await asyncio.sleep(0.01)
                    if app.phase == "ready":
                        break
                self.assertEqual(app.phase, "ready")
                await fbot.incoming("где Иван?")
                fbot.stop.set()
                await asyncio.wait_for(run, 5)
                return app

            app = asyncio.run(scenario())
            sent = "\n".join(fbot.sent)
            self.assertIn("Начинаю скачивать всю историю", sent)
            self.assertIn("История скачана: 30 сообщений из 2 чатов", sent)
            self.assertIn("Примерная стоимость", sent)
            self.assertIn("Вот как я понял твою работу", sent)
            self.assertIn("Ты диспетчер грузоперевозок", sent)
            self.assertIn("Я на связи", sent)
            self.assertEqual(fbot.sent[-1], "Иван — водитель, трак 101.")
            self.assertIsNotNone(app.db.get_kv("initial_download_done"))
            app.db.conn.close()

    def test_regular_start_does_not_block(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "config.yaml").write_text("timezone: UTC\nwatch:\n  enabled: false\nreport:\n  enabled: false\n"
                                              "learn:\n  nightly: false\n", encoding="utf-8")
            cfg = load_config(root)
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
            bot_mod.connect_user = fake_connect
            bot_mod.TelegramClient = lambda *a, **k: fbot

            async def scenario():
                app = bot_mod.App(cfg)
                app.db.set_kv("initial_download_done", "2026-01-01 00:00:00")
                app.db.set_kv("learn_cursor:-1", "1")
                run = asyncio.create_task(app.run())
                for _ in range(100):
                    await asyncio.sleep(0.01)
                    if app.phase == "ready":
                        break
                self.assertEqual(app.phase, "ready", "обычный запуск — сразу в работу, докачка в фоне")
                await asyncio.sleep(0.05)
                fbot.stop.set()
                await asyncio.wait_for(run, 5)
                self.assertEqual(app.db.message_stats()["n"], 1, "пропущенное докачано в фоне")
                app.db.conn.close()
            asyncio.run(scenario())

    def test_nothing_downloaded_explains_config(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "config.yaml").write_text("timezone: UTC\ntelegram:\n  chats: ['Несуществующий чат']\n"
                                              "watch:\n  enabled: false\nreport:\n  enabled: false\n",
                                              encoding="utf-8")
            cfg = load_config(root)
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
            bot_mod.connect_user = fake_connect
            bot_mod.TelegramClient = lambda *a, **k: fbot

            async def scenario():
                app = bot_mod.App(cfg)
                run = asyncio.create_task(app.run())
                for _ in range(100):
                    await asyncio.sleep(0.01)
                    if app.phase == "ready":
                        break
                fbot.stop.set()
                await asyncio.wait_for(run, 5)
                self.assertIsNone(app.db.get_kv("initial_download_done"), "в следующий раз попробуем снова")
                app.db.conn.close()
            asyncio.run(scenario())
            self.assertTrue(any("chats: all" in m for m in fbot.sent))


if __name__ == "__main__":
    unittest.main()
