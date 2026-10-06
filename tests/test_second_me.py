"""Тесты режима «второй я»: все чаты, только чтение, ничего не забывает."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from telethon.tl import functions, types

from assistant.brain import Assistant
from assistant.config import load_config
from assistant.jobs import Scheduler
from assistant.learn import build_plan, learn_history
from assistant.telegram_archive import (
    ChatFilter,
    ReadOnlyClient,
    ReadOnlyViolation,
    _assert_read_only,
    attach_live_listener,
)
from assistant.util import to_db, utcnow
from tests.test_core import fake_client, text_resp

BASE_CONFIG = "timezone: Europe/Moscow\n"


def make_cfg(tmp: Path, extra: str = ""):
    (tmp / "config.yaml").write_text(BASE_CONFIG + extra, encoding="utf-8")
    return load_config(tmp)


def row(chat_id, msg_id, text, *, sender="Иван", outgoing=0, minutes_ago=0):
    return {
        "chat_id": chat_id, "msg_id": msg_id, "date": to_db(utcnow() - timedelta(minutes=minutes_ago)),
        "sender_id": 1, "sender_name": sender, "text": text, "outgoing": outgoing,
    }


class Tmp(unittest.TestCase):
    extra_config = ""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cfg = make_cfg(self.tmp, self.extra_config)
        self.asst = Assistant(self.cfg)
        self.db = self.asst.db

    def tearDown(self):
        self.db.conn.close()
        self._tmp.cleanup()


# ---------------------------------------------------------------------- только чтение
class ReadOnlyTests(unittest.TestCase):
    def test_write_requests_are_blocked(self):
        peer = types.InputPeerSelf()
        for req in (
            functions.messages.SendMessageRequest(peer=peer, message="hi"),
            functions.messages.ReadHistoryRequest(peer=peer, max_id=0),
            functions.messages.SetTypingRequest(peer=peer, action=types.SendMessageTypingAction()),
            functions.messages.DeleteMessagesRequest(id=[1]),
            functions.messages.ForwardMessagesRequest(from_peer=peer, id=[1], to_peer=peer),
            functions.messages.EditMessageRequest(peer=peer, id=1, message="x"),
            functions.account.UpdateStatusRequest(offline=False),
            functions.channels.JoinChannelRequest(channel=types.InputChannel(1, 1)),
        ):
            with self.subTest(req=type(req).__name__), self.assertRaises(ReadOnlyViolation):
                _assert_read_only(req)

    def test_read_requests_pass(self):
        peer = types.InputPeerSelf()
        for req in (
            functions.messages.GetHistoryRequest(peer=peer, offset_id=0, offset_date=None, add_offset=0,
                                                 limit=1, max_id=0, min_id=0, hash=0),
            functions.messages.GetDialogsRequest(offset_date=None, offset_id=0, offset_peer=peer, limit=1, hash=0),
            functions.updates.GetStateRequest(),
            functions.upload.GetFileRequest(location=types.InputPhotoFileLocation(1, 1, b"", "x"),
                                            offset=0, limit=1024),
            functions.contacts.ResolveUsernameRequest(username="x"),
        ):
            with self.subTest(req=type(req).__name__):
                _assert_read_only(req)

    def test_wrapped_and_batched_writes_are_blocked(self):
        send = functions.messages.SendMessageRequest(peer=types.InputPeerSelf(), message="hi")
        wrapped = functions.InvokeWithLayerRequest(layer=1, query=functions.InitConnectionRequest(
            api_id=1, device_model="d", system_version="s", app_version="a", system_lang_code="en",
            lang_pack="", lang_code="en", query=send))
        with self.assertRaises(ReadOnlyViolation):
            _assert_read_only(wrapped)
        with self.assertRaises(ReadOnlyViolation):
            _assert_read_only([functions.updates.GetStateRequest(), send])

    def test_client_high_level_send_is_blocked_before_network(self):
        with tempfile.TemporaryDirectory() as d:
            async def go():
                client = ReadOnlyClient(str(Path(d) / "s"), 1, "x")
                with self.assertRaises(ReadOnlyViolation):
                    await client.send_message(types.InputPeerSelf(), "привет")
                with self.assertRaises(ReadOnlyViolation):
                    await client.send_read_acknowledge(types.InputPeerSelf(), max_id=5)
                client.session.close()
            asyncio.run(go())


# ---------------------------------------------------------------------- какие чаты берём
def user(uid, *, bot=False, is_self=False, username=None, first="Имя"):
    return types.User(id=uid, bot=bot, is_self=is_self, username=username, first_name=first)


def channel(cid, *, megagroup=False, title="Канал", username=None):
    return types.Channel(id=cid, title=title, photo=types.ChatPhotoEmpty(), date=datetime.now(timezone.utc),
                         megagroup=megagroup, broadcast=not megagroup, username=username)


class ChatFilterTests(Tmp):
    extra_config = (
        "telegram:\n  chats: all\n  exclude_chats: ['Семья', '@spam', 555]\n"
    )

    def test_defaults(self):
        f = ChatFilter(self.cfg)
        self.assertTrue(f.allows(user(1)), "личный чат")
        self.assertTrue(f.allows(channel(10, megagroup=True, title="Диспетчерская")), "группа")
        self.assertTrue(f.allows(user(2, is_self=True)), "Избранное")
        self.assertFalse(f.allows(channel(11, title="Новости")), "канал выключен по умолчанию")
        self.assertFalse(f.allows(user(3, bot=True, username="weatherbot")), "боты выключены по умолчанию")
        self.assertFalse(f.allows(user(777000, first="Telegram")), "служебный чат с кодами")
        self.assertFalse(f.allows(user(4, bot=True, username="BotFather")), "BotFather")
        self.assertFalse(f.allows(channel(12, megagroup=True, title="Семья")), "исключён по названию")
        self.assertFalse(f.allows(user(5, username="spam")), "исключён по @username")
        self.assertFalse(f.allows(user(555)), "исключён по id")

    def test_flags_and_own_bot(self):
        self.cfg.include_channels = True
        self.cfg.include_bots = True
        self.cfg.bot_token = "999:abc"
        f = ChatFilter(self.cfg)
        self.assertTrue(f.allows(channel(11, title="Новости")))
        self.assertTrue(f.allows(user(3, bot=True, username="loadsbot")))
        self.assertFalse(f.allows(user(999, bot=True, username="my_assistant_bot")), "свой бот — никогда")
        self.assertFalse(f.allows(user(4, bot=True, username="botfather")), "BotFather — никогда")

    def test_apply_to_db_unmonitors_excluded(self):
        self.db.upsert_chat(-1001, "Новости", None, "канал")
        self.db.upsert_chat(-1002, "Семья", None, "группа")
        self.db.upsert_chat(-1003, "Работа", None, "группа")
        self.assertEqual(ChatFilter(self.cfg, self.db).apply_to_db(self.db), 2)
        self.assertEqual(self.db.monitored_chat_ids(), {-1003})


class ConfigChatsTests(unittest.TestCase):
    def test_variants(self):
        cases = {
            "": (True, []), "telegram:\n  chats: all\n": (True, []), "telegram:\n  chats: Все\n": (True, []),
            "telegram:\n  chats: []\n": (True, []), "telegram:\n  chats: ['Работа', -100]\n": (False, ["Работа", -100]),
            "telegram:\n  chats: ['Работа', all]\n": (True, []),
        }
        for extra, expected in cases.items():
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as d:
                cfg = make_cfg(Path(d), extra)
                self.assertEqual((cfg.all_chats, cfg.chats), expected)

    def test_old_ignore_chats_name_still_works(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = make_cfg(Path(d), "telegram:\n  ignore_chats: ['Старое']\n")
            self.assertEqual(cfg.exclude_chats, ["Старое"])


# ---------------------------------------------------------------------- ничего не забывает
class MemoryOfEverythingTests(Tmp):
    def setUp(self):
        super().setUp()
        self.db.upsert_chat(-100, "Диспетчерская", None, "группа")
        self.db.upsert_chat(42, "Мария", None, "личный")

    def test_owner_messages_edits_and_deletions(self):
        self.db.upsert_messages([
            row(-100, 1, "Выгрузка в 15:00", minutes_ago=10),
            row(-100, 2, "Принял, держи в курсе", sender="Денис", outgoing=1, minutes_ago=5),
        ])
        self.db.upsert_messages([{**row(-100, 1, "Выгрузка перенесена на 17:00", minutes_ago=10),
                                  "edited_at": to_db(utcnow())}])
        self.assertEqual(self.db.mark_deleted(-100, [2]), 1)
        out = self.asst.tools.format_messages(self.db.read_messages([-100]))
        self.assertIn("#2 Я: Принял", out)
        self.assertIn("перенесена на 17:00 (изменено; было: «Выгрузка в 15:00»)", out)
        self.assertIn("УДАЛЕНО", out)
        # поиск видит актуальный текст
        self.assertEqual(len(self.db.search_messages("перенесена")), 1)

    def test_deleted_without_chat_id_does_not_touch_channels(self):
        self.db.upsert_messages([row(42, 7, "личное"), row(-1001234567890, 7, "канал")])
        self.db.upsert_chat(-1001234567890, "Супергруппа", None, "группа")
        self.assertEqual(self.db.mark_deleted(None, [7]), 1)
        deleted = {r["chat_id"] for r in self.db.read_messages(None) if r["deleted_at"]}
        self.assertEqual(deleted, {42})

    def test_search_own_messages_and_card_masking(self):
        self.db.upsert_messages([
            row(42, 1, "Скинь номер карты"),
            row(42, 2, "Вот: 4111 1111 1111 1111", outgoing=1),
        ])
        res = asyncio.run(self.asst.tools.execute("search_messages", {"query": "вот", "sender": "я"}, "t"))
        self.assertIn("Я:", res["content"])
        self.assertIn("[карта ****1111]", res["content"])
        self.assertNotIn("4111 1111", res["content"])

    def test_cyrillic_case_insensitive_filters(self):
        self.db.upsert_messages([row(-100, 9, "Еду на выгрузку", sender="Иван Петров")])
        self.assertEqual(len(self.db.search_messages("выгруз", sender="иван")), 1)
        self.assertEqual(len(self.db.search_messages("ЕДУ")), 1)

    def test_search_conversations(self):
        self.db.add_dialog("user", "Запомни, что Пётр не ездит в Нью-Йорк")
        self.db.add_dialog("assistant", "Запомнил.")
        self.db.add_report("daily", "Отчёт: Пётр свободен в Чикаго")
        res = asyncio.run(self.asst.tools.execute("search_conversations", {"query": "пётр"}, "t"))
        self.assertIn("Владелец: Запомни", res["content"])
        self.assertIn("Отчёт: Отчёт: Пётр свободен", res["content"])

    def test_knowledge_has_owner(self):
        self.db.set_kv("owner_name", "Денис")
        self.assertIn("Денис. В архиве его сообщения подписаны «Я».", self.asst.knowledge_text())


# ---------------------------------------------------------------------- изучение всех чатов
class LearnPackingTests(Tmp):
    extra_config = "memory:\n  learn_chunk_chars: 2000\n"

    def setUp(self):
        super().setUp()
        for i in range(6):  # шесть маленьких личных чатов
            self.db.upsert_chat(1000 + i, f"Контакт {i}", None, "личный")
            self.db.upsert_messages([row(1000 + i, 1, f"Привет от контакта {i}", minutes_ago=100 - i)])
        self.db.upsert_chat(-200, "Большая группа", None, "группа")
        self.db.upsert_messages([row(-200, n, "сообщение " + "x" * 90, minutes_ago=50) for n in range(1, 61)])
        self.db.upsert_chat(-300, "Исключённый", None, "группа")
        self.db.upsert_messages([row(-300, 1, "не должно попасть")])
        self.db.set_monitored(-300, False)

    def test_small_chats_share_a_chunk_and_big_chat_splits(self):
        plan = build_plan(self.asst, None)
        first = plan[0]
        self.assertGreaterEqual(len(first.rows), 6, "мелкие чаты упакованы вместе")
        self.assertIn("=== Чат «Контакт 0» (личный) ===", "\n".join(first.lines))
        self.assertTrue(all(c.size <= 2000 + 200 for c in plan))
        self.assertGreater(sum(1 for c in plan if -200 in c.rows), 1, "большой чат разбит на части")
        self.assertFalse(any(-300 in c.rows for c in plan), "исключённый чат не изучается")

    def test_cursors_per_chat_and_nightly_cap(self):
        facts = {"summary": "s", "facts": []}
        client, fake = fake_client([text_resp(json.dumps(facts)) for _ in range(50)])
        self.asst.llm.client = client
        total = len(build_plan(self.asst, None))
        self.assertTrue(asyncio.run(learn_history(self.asst, None, "claude-opus-5-5", yes=True,
                                                  say=lambda s: None, max_chunks=2)))
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(len(build_plan(self.asst, None)), total - 2, "остальное — в следующий раз")
        self.assertIn("«Я» — владелец", fake.calls[0]["messages"][0]["content"])

    def test_nightly_waits_for_first_manual_learn(self):
        client, fake = fake_client([text_resp(json.dumps({"summary": "s", "facts": []})) for _ in range(50)])
        self.asst.llm.client = client

        async def notify(_):
            pass

        sched = Scheduler(self.asst, notify, set())
        asyncio.run(sched.nightly_once())
        self.assertEqual(len(fake.calls), 0, "без ручного learn ночью ничего не тратим")
        self.db.set_kv("learn_cursor:1000", "0")
        self.cfg.nightly_max_chunks = 1
        asyncio.run(sched.nightly_once())
        self.assertEqual(len(fake.calls), 2, "1 часть истории + обновление профиля")


# ---------------------------------------------------------------------- живое прослушивание
class FakeTelethon:
    def __init__(self, history):
        self.handlers = []
        self.history = history  # chat_id → list[Message]

    def add_event_handler(self, fn, event):
        self.handlers.append((type(event).__name__, fn))

    async def iter_messages(self, entity, reverse=True, min_id=0, wait_time=None, offset_date=None):
        for m in self.history.get(-1000000000000 - entity.id, []):
            if m.id > min_id:
                yield m

    def handler(self, name):
        return next(fn for n, fn in self.handlers if n == name)


def tl_message(chat_peer_id, msg_id, text, out=False):
    m = types.Message(id=msg_id, peer_id=types.PeerChannel(chat_peer_id), date=datetime.now(timezone.utc),
                      message=text, from_id=types.PeerUser(77), out=out)
    m._finish_init(SimpleNamespace(_self_id=1, _mb_entity_cache=SimpleNamespace(self_id=1)),
                   {77: types.User(id=77, first_name="Иван")}, None)
    return m


class LiveListenerTests(Tmp):
    extra_config = "telegram:\n  chats: all\n"

    def test_new_chat_is_downloaded_whole_and_deletions_marked(self):
        group = channel(5, megagroup=True, title="Новая группа")
        chat_id = -1000000000005
        history = {chat_id: [tl_message(5, i, f"старое {i}") for i in range(1, 4)] + [tl_message(5, 4, "новое")]}
        client = FakeTelethon(history)
        busy: set[int] = set()
        attach_live_listener(client, self.db, self.cfg, ChatFilter(self.cfg, self.db), busy)

        async def go():
            event = SimpleNamespace(chat_id=chat_id, message=history[chat_id][-1])

            async def get_chat():
                return group
            event.get_chat = get_chat
            await client.handler("NewMessage")(event)
            await asyncio.sleep(0.05)
            await client.handler("MessageDeleted")(SimpleNamespace(chat_id=chat_id, deleted_ids=[2]))
        asyncio.run(go())

        rows = self.db.read_messages([chat_id])
        self.assertEqual([r["msg_id"] for r in rows], [1, 2, 3, 4], "вся история нового чата")
        self.assertIn(chat_id, self.db.monitored_chat_ids())
        self.assertIsNotNone(rows[1]["deleted_at"])
        self.assertEqual(busy, set())

    def test_excluded_new_chat_is_ignored(self):
        news = channel(6, title="Новости")
        client = FakeTelethon({})
        attach_live_listener(client, self.db, self.cfg, ChatFilter(self.cfg, self.db), set())

        async def go():
            async def get_chat():
                return news
            await client.handler("NewMessage")(SimpleNamespace(chat_id=-1000000000006,
                                                               message=tl_message(6, 1, "x"), get_chat=get_chat))
        asyncio.run(go())
        self.assertEqual(self.db.chats(), [])


if __name__ == "__main__":
    unittest.main()
