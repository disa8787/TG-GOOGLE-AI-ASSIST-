"""Регрессионные тесты на проблемы, найденные ревью: ничего не теряется, исключения работают везде,
чтение Telegram без побочных эффектов."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from telethon.tl import functions, types

from assistant.brain import Assistant
from assistant.config import load_config
from assistant.db import DB
from assistant.jobs import Scheduler
from assistant.learn import build_plan, build_profile, learn_history, run_learn
from assistant.telegram_archive import (
    ChatFilter,
    ReadOnlyViolation,
    _assert_read_only,
    attach_live_listener,
    download_chat,
    remember_owner,
    sync_chats,
)
from assistant.util import mask_cards, to_db, utcnow
from tests.test_core import fake_client, text_resp

NOW = datetime.now(timezone.utc)


def make_cfg(tmp: Path, extra: str = ""):
    (tmp / "config.yaml").write_text("timezone: Europe/Moscow\n" + extra, encoding="utf-8")
    return load_config(tmp)


def row(chat_id, msg_id, text, *, minutes_ago=0, sender_id=1, outgoing=0):
    return {"chat_id": chat_id, "msg_id": msg_id, "date": to_db(utcnow() - timedelta(minutes=minutes_ago)),
            "sender_id": sender_id, "sender_name": "Иван", "text": text, "outgoing": outgoing}


def tl_msg(channel_id, msg_id, text, *, minutes_ago=0):
    m = types.Message(id=msg_id, peer_id=types.PeerChannel(channel_id), message=text,
                      date=NOW - timedelta(minutes=minutes_ago), from_id=types.PeerUser(77))
    m._finish_init(SimpleNamespace(_self_id=1, _mb_entity_cache=SimpleNamespace(self_id=1)),
                   {77: types.User(id=77, first_name="Иван")}, None)
    return m


def group(cid, title="Группа", megagroup=True):
    return types.Channel(id=cid, title=title, photo=types.ChatPhotoEmpty(), date=NOW,
                         megagroup=megagroup, broadcast=not megagroup)


def peer(cid):
    return -1_000_000_000_000 - cid


class FakeTG:
    """Минимальная замена Telethon-клиента: диалоги, история, обработчики событий."""

    def __init__(self):
        self.entities: dict[int, object] = {}
        self.history: dict[int, list] = {}
        self.handlers = []

    def add(self, entity, messages):
        self.entities[peer(entity.id)] = entity
        self.history[peer(entity.id)] = list(messages)

    def add_event_handler(self, fn, event):
        self.handlers.append((type(event).__name__, fn))

    def handler(self, name):
        return next(fn for n, fn in self.handlers if n == name)

    async def iter_dialogs(self):
        for pid, ent in self.entities.items():
            msgs = self.history[pid]
            top = msgs[-1] if msgs else None
            yield SimpleNamespace(id=pid, entity=ent, name=ent.title, message=top,
                                  date=top.date if top else None)

    async def get_dialogs(self):
        return []

    async def get_entity(self, pid):
        return self.entities[pid]

    async def iter_messages(self, entity, reverse=True, min_id=0, offset_date=None, wait_time=None):
        for m in self.history[peer(entity.id)]:
            if m.id <= (min_id or 0):
                continue
            if offset_date is not None and m.date <= offset_date:
                continue
            yield m


class Base(unittest.TestCase):
    extra = "telegram:\n  chats: all\n"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cfg = make_cfg(self.tmp, self.extra)
        self.asst = Assistant(self.cfg)
        self.db = self.asst.db

    def tearDown(self):
        self.db.conn.close()
        self._tmp.cleanup()


# ---------------------------------------------------------------------- ничего не теряется при запуске
class StartupRaceTests(Base):
    def test_live_message_during_sync_does_not_hide_missed_history(self):
        g = group(5, "Иван")
        tg = FakeTG()
        tg.add(g, [tl_msg(5, i, f"сообщение {i}", minutes_ago=100 - i) for i in range(1, 4)])
        asyncio.run(download_chat(tg, self.db, g, progress=lambda s: None))
        # пока компьютер был выключен, пришли 4 и 5; сразу после запуска «вживую» — 6
        tg.history[peer(5)] += [tl_msg(5, 4, "пропущенное 4", minutes_ago=50), tl_msg(5, 5, "пропущенное 5")]
        tg.history[peer(5)].append(tl_msg(5, 6, "живое 6"))
        busy: set[int] = set()
        cf = ChatFilter(self.cfg, self.db)
        attach_live_listener(tg, self.db, self.cfg, cf, busy)
        asyncio.run(tg.handler("NewMessage")(SimpleNamespace(chat_id=peer(5), message=tg.history[peer(5)][-1])))
        asyncio.run(sync_chats(tg, self.db, self.cfg, cf, busy))
        self.assertEqual([r["msg_id"] for r in self.db.read_messages([peer(5)])], [1, 2, 3, 4, 5, 6])

    def test_interrupted_download_resumes(self):
        g = group(7)
        tg = FakeTG()
        tg.add(g, [tl_msg(7, i, f"m{i}", minutes_ago=2000 - i) for i in range(1, 1201)])

        class Boom(Exception):
            pass

        async def broken(entity, **kw):
            async for m in FakeTG.iter_messages(tg, entity, **kw):
                if m.id == 700:
                    raise Boom
                yield m
        tg.iter_messages = broken
        with self.assertRaises(Boom):
            asyncio.run(download_chat(tg, self.db, g, progress=lambda s: None))
        self.assertEqual(self.db.synced_msg_id(peer(7)), 500, "отметка — по сохранённой пачке")
        tg.iter_messages = lambda entity, **kw: FakeTG.iter_messages(tg, entity, **kw)
        asyncio.run(download_chat(tg, self.db, g, progress=lambda s: None))
        self.assertEqual(len(self.db.read_messages([peer(7)], limit=5000)), 1200)

    def test_since_floor_is_kept(self):
        g = group(8)
        tg = FakeTG()
        tg.add(g, [tl_msg(8, i, "старое", minutes_ago=10_000) for i in range(1, 50)])
        asyncio.run(download_chat(tg, self.db, g, since=NOW - timedelta(days=1), progress=lambda s: None))
        asyncio.run(sync_chats(tg, self.db, self.cfg, ChatFilter(self.cfg, self.db)))
        self.assertEqual(self.db.read_messages([peer(8)]), [], "download --since не откатывается синхронизацией")


class ReconcileTests(Base):
    def test_offline_edits_and_deletions_are_picked_up(self):
        g = group(9)
        tg = FakeTG()
        old = tl_msg(9, 1, "давнее", minutes_ago=60 * 24 * 10)
        tg.add(g, [old, tl_msg(9, 2, "выгрузка в 15:00", minutes_ago=60), tl_msg(9, 3, "ок", minutes_ago=50)])
        asyncio.run(download_chat(tg, self.db, g, progress=lambda s: None))
        # пока программа была выключена: 2 изменили, 3 удалили, пришло 4
        tg.history[peer(9)] = [old, tl_msg(9, 2, "выгрузка в 18:00", minutes_ago=60), tl_msg(9, 4, "новое")]
        asyncio.run(sync_chats(tg, self.db, self.cfg, ChatFilter(self.cfg, self.db)))
        out = self.asst.tools.format_messages(self.db.read_messages([peer(9)]))
        self.assertIn("выгрузка в 18:00 (изменено; было: «выгрузка в 15:00»)", out)
        self.assertIn("#3 Иван: ок (УДАЛЕНО", out)
        self.assertNotIn("давнее (УДАЛЕНО", out, "старше окна — не трогаем")
        self.assertIn("#4 Иван: новое", out)


# ---------------------------------------------------------------------- исключения работают везде
class ExclusionTests(Base):
    def setUp(self):
        super().setUp()
        self.db.upsert_chat(-1, "Семья", None, "группа")
        self.db.upsert_chat(-2, "Работа", None, "группа")
        self.db.upsert_messages([row(-1, 1, "у мамы диагноз подтвердился"), row(-2, 1, "груз 777 выгружен")])

    def exclude_family(self):
        self.cfg.exclude_chats = ["Семья"]

    def test_learn_skips_chat_excluded_after_download(self):
        self.exclude_family()
        client, fake = fake_client([text_resp(json.dumps({"summary": "s", "facts": []})), text_resp("профиль")])
        self.asst.llm.client = client
        asyncio.run(run_learn(self.asst, chats=None, reset=False, yes=True, sheets_only=False, model=None))
        sent = json.dumps(fake.calls, ensure_ascii=False)
        self.assertNotIn("диагноз", sent)
        self.assertIn("груз 777", sent)

    def test_tools_do_not_see_excluded_chat(self):
        self.exclude_family()
        ChatFilter(self.cfg, self.db).apply_to_db(self.db)
        run = lambda name, args: asyncio.run(self.asst.tools.execute(name, args, "t"))["content"]  # noqa: E731
        self.assertNotIn("диагноз", run("search_messages", {"query": "диагноз"}))
        self.assertNotIn("диагноз", run("read_chat", {"date_from": "2000-01-01"}))
        self.assertNotIn("Семья", run("list_chats", {}))
        self.assertIn("не найден", run("read_chat", {"chat": "Семья"}))

    def test_chat_removed_from_exclusions_comes_back(self):
        self.exclude_family()
        ChatFilter(self.cfg, self.db).apply_to_db(self.db)
        self.cfg.exclude_chats = []
        ChatFilter(self.cfg, self.db).apply_to_db(self.db)
        self.assertIn(-1, self.db.monitored_chat_ids())

    def test_explicit_channel_is_not_unmonitored(self):
        self.db.upsert_chat(-3, "Грузы канал", None, "канал")
        self.db.set_explicit(-3, True)
        self.db.upsert_chat(-4, "Новости", None, "канал")
        ChatFilter(self.cfg, self.db).apply_to_db(self.db)
        monitored = self.db.monitored_chat_ids()
        self.assertIn(-3, monitored, "явно выбранный канал остаётся")
        self.assertNotIn(-4, monitored, "обычный канал выключен include_channels: false")


class ListModeTests(Base):
    extra = "telegram:\n  chats: ['Грузы канал']\n"

    def test_listed_channel_is_allowed(self):
        ch = group(11, "Грузы канал", megagroup=False)
        self.assertTrue(ChatFilter(self.cfg).allows(ch, explicit=True))
        self.db.upsert_chat(peer(11), "Грузы канал", None, "канал")
        ChatFilter(self.cfg, self.db).apply_to_db(self.db)
        self.assertIn(peer(11), self.db.monitored_chat_ids(), "в режиме списка тип чата не отключает")


# ---------------------------------------------------------------------- изучение
class LearnTests(Base):
    extra = "telegram:\n  chats: all\nmemory:\n  learn_chunk_chars: 1500\n"

    def test_out_of_order_rows_are_never_skipped(self):
        self.db.upsert_chat(-5, "Чат", None, "группа")
        # сначала сохранилось новое (живое) сообщение, потом — пропущенная старая история
        self.db.upsert_messages([row(-5, 201, "ВАЖНО: груз 777 перенесли", minutes_ago=1)])
        self.db.upsert_messages([row(-5, i, "старое " + "x" * 80, minutes_ago=500 - i) for i in range(1, 61)])
        client, fake = fake_client([text_resp(json.dumps({"summary": "s", "facts": []})) for _ in range(30)])
        self.asst.llm.client = client
        for _ in range(10):
            asyncio.run(learn_history(self.asst, None, "claude-opus-5-5", yes=True, say=lambda s: None, max_chunks=1))
        sent = "".join(c["messages"][0]["content"] for c in fake.calls)
        self.assertIn("ВАЖНО: груз 777", sent)
        self.assertEqual(build_plan(self.asst, None), [])

    def test_nightly_defers_big_unapproved_history_and_reports_it(self):
        self.db.upsert_chat(-6, "Одобренный", None, "группа")
        self.db.upsert_messages([row(-6, 1, "новое за день")])
        self.db.set_kv("learn_cursor:-6", "1")
        self.db.upsert_chat(-7, "Большая новая группа", None, "группа")
        self.db.upsert_messages([row(-7, i, "история " + "y" * 100, minutes_ago=9000) for i in range(1, 200)])
        self.db.upsert_chat(-8, "Новый знакомый", None, "личный")
        self.db.upsert_messages([row(-8, 1, "привет, это Олег")])
        client, fake = fake_client([text_resp(json.dumps({"summary": "s", "facts": []})) for _ in range(10)])
        self.asst.llm.client = client
        sent_notes = []

        async def notify(text):
            sent_notes.append(text)

        asyncio.run(Scheduler(self.asst, notify, set()).nightly_once())
        prompts = "".join(c["messages"][0]["content"] for c in fake.calls)
        self.assertIn("новое за день", prompts)
        self.assertIn("привет, это Олег", prompts, "маленький новый чат изучается сам")
        self.assertNotIn("история yyy", prompts, "большая чужая история — только после ручного learn")
        self.assertTrue(any("Большая новая группа" in n for n in sent_notes))

    def test_profile_truncation_drops_oldest_not_whole_chats(self):
        import assistant.learn as learn_mod
        old_limit = learn_mod.PROFILE_INPUT_CHARS
        learn_mod.PROFILE_INPUT_CHARS = 400
        try:
            self.db.upsert_chat(-100, "Старый чат", None, "группа")
            self.db.add_learn_note(-100, "2020-01-01 — 2020-02-01", "очень старое " * 10)
            self.db.add_learn_note(None, "2026-09-01 — 2026-10-01", "свежие мелкие чаты " * 5)
            self.db.add_learn_note(-100, "2026-09-01 — 2026-10-02", "свежее в старом чате " * 5)
            client, fake = fake_client([text_resp("профиль")])
            self.asst.llm.client = client
            asyncio.run(build_profile(self.asst, "claude-opus-5-5", say=lambda s: None))
            prompt = fake.calls[0]["messages"][0]["content"]
            self.assertNotIn("очень старое", prompt)
            self.assertIn("свежие мелкие чаты", prompt)
            self.assertIn("свежее в старом чате", prompt)
        finally:
            learn_mod.PROFILE_INPUT_CHARS = old_limit


# ---------------------------------------------------------------------- фоновая проверка
class WatchTests(Base):
    def test_backfilled_history_is_not_new_and_nothing_is_dropped(self):
        client, fake = fake_client([text_resp("NOTHING") for _ in range(10)])
        self.asst.llm.client = client
        self.db.upsert_chat(-9, "Чат", None, "группа")
        asyncio.run(self.asst.watch_check())  # ставит отметку
        self.db.upsert_messages([row(-9, 1, "сломался трак")])
        self.db.upsert_messages([{**row(-9, i, "старая история", minutes_ago=99999), "backfill": 1}
                                 for i in range(2, 50)])
        asyncio.run(self.asst.watch_check())
        prompt = fake.calls[-1]["messages"][0]["content"]
        self.assertIn("сломался трак", prompt)
        self.assertNotIn("старая история", prompt)

    def test_large_batch_is_paged_not_truncated(self):
        import assistant.brain as brain_mod
        client, fake = fake_client([text_resp("NOTHING") for _ in range(20)])
        self.asst.llm.client = client
        self.db.upsert_chat(-10, "Чат", None, "группа")
        asyncio.run(self.asst.watch_check())
        self.db.upsert_messages([row(-10, i, f"сообщение номер {i} " + "z" * 300) for i in range(1, 41)])
        old = brain_mod.WATCH_PAYLOAD_CHARS
        brain_mod.WATCH_PAYLOAD_CHARS = 5000
        try:
            for _ in range(5):
                asyncio.run(self.asst.watch_check())
        finally:
            brain_mod.WATCH_PAYLOAD_CHARS = old
        seen = "".join(c["messages"][0]["content"] for c in fake.calls)
        for i in (1, 20, 40):
            self.assertIn(f"сообщение номер {i} ", seen)


# ---------------------------------------------------------------------- прочее
class MiscTests(Base):
    def test_owner_name_and_outgoing_backfill(self):
        self.db.upsert_chat(-11, "Мария", None, "личный")
        self.db.upsert_messages([row(-11, 1, "обещаю перезвонить в пятницу", sender_id=42)])
        remember_owner(self.db, types.User(id=42, is_self=True, first_name="Денис"))
        self.assertEqual(self.db.get_kv("owner_name"), "Денис")
        self.assertEqual(len(self.db.search_messages("обещаю", outgoing=True)), 1)

    def test_memory_history_is_kept(self):
        nid = self.db.upsert_memory("водитель", "Водитель: Иван", "Трак 101, телефон 555")
        self.db.upsert_memory("водитель", "Водитель: Иван", "Трак 105")
        self.db.delete_memory(nid)
        out = asyncio.run(self.asst.tools.execute("search_memory", {"query": "иван"}, "t"))["content"]
        self.assertIn("Трак 101, телефон 555", out)
        self.assertIn("удалена", out)

    def test_media_path_not_erased_by_edit(self):
        self.db.upsert_chat(-12, "Чат", None, "группа")
        self.db.upsert_messages([{**row(-12, 1, "BOL"), "media": "[фото]", "media_path": "/data/media/1.jpg"}])
        self.db.upsert_messages([{**row(-12, 1, "BOL исправленный"), "media": "[фото]"}])
        self.assertEqual(self.db.read_messages([-12])[0]["media_path"], "/data/media/1.jpg")

    def test_card_masking_spares_load_numbers(self):
        self.assertEqual(mask_cards("PU# 4521884 4521885"), "PU# 4521884 4521885")
        self.assertEqual(mask_cards("Dallas, TX 75201 2145551201"), "Dallas, TX 75201 2145551201")
        self.assertEqual(mask_cards("карта 4111 1111 1111 1111"), "карта [карта ****1111]")
        self.assertEqual(mask_cards("4111111111111111"), "[карта ****1111]")


class ReadOnlySideEffectTests(unittest.TestCase):
    def test_reads_with_side_effects_are_blocked(self):
        p = types.InputPeerSelf()
        for req in (
            functions.messages.GetBotCallbackAnswerRequest(peer=p, msg_id=1, data=b"x"),
            functions.messages.GetInlineBotResultsRequest(bot=types.InputUserSelf(), peer=p, query="q", offset=""),
            functions.contacts.GetLocatedRequest(geo_point=types.InputGeoPoint(1.0, 2.0), self_expires=60),
            functions.messages.GetMessagesViewsRequest(peer=p, id=[1], increment=True),
            functions.account.GetPasswordRequest(),
            functions.payments.GetPaymentFormRequest(invoice=types.InputInvoiceMessage(peer=p, msg_id=1)),
            functions.auth.SendCodeRequest(phone_number="+1", api_id=1, api_hash="x",
                                           settings=types.CodeSettings()),
        ):
            with self.subTest(req=type(req).__name__), self.assertRaises(ReadOnlyViolation):
                _assert_read_only(req)

    def test_needed_service_requests_pass(self):
        _assert_read_only(functions.auth.ExportAuthorizationRequest(dc_id=2))
        _assert_read_only(functions.upload.ReuploadCdnFileRequest(file_token=b"", request_token=b""))
        _assert_read_only(functions.help.GetConfigRequest())


class MigrationTests(unittest.TestCase):
    def test_database_from_first_version_is_upgraded(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "old.db"
            conn = sqlite3.connect(path)
            conn.executescript("""
                CREATE TABLE kv(key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE chats(chat_id INTEGER PRIMARY KEY, title TEXT, username TEXT, kind TEXT,
                                   monitored INTEGER NOT NULL DEFAULT 1, last_sync TEXT);
                CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
                    msg_id INTEGER NOT NULL, date TEXT NOT NULL, sender_id INTEGER, sender_name TEXT,
                    text TEXT NOT NULL DEFAULT '', reply_to INTEGER, fwd_from TEXT, media TEXT, media_path TEXT,
                    edited_at TEXT, UNIQUE(chat_id, msg_id));
                CREATE TABLE memory(id INTEGER PRIMARY KEY AUTOINCREMENT, skey TEXT NOT NULL UNIQUE,
                    category TEXT NOT NULL, subject TEXT NOT NULL, content TEXT NOT NULL, source TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            """)
            conn.execute("INSERT INTO chats(chat_id, title, kind) VALUES(-1, 'Работа', 'группа')")
            for i in range(1, 6):
                conn.execute("INSERT INTO messages(chat_id, msg_id, date, text) "
                             "VALUES(-1, ?, '2026-01-01 10:00:00', ?)", (i, f"m{i}"))
            conn.execute("INSERT INTO kv VALUES('learn_cursor:-1', '3')")
            conn.commit()
            conn.close()

            db = DB(path)
            self.assertEqual(db.synced_msg_id(-1), 5, "уже скачанное считается докачанным")
            learned = [r["learned"] for r in db.read_messages([-1])]
            self.assertEqual(learned, [1, 1, 1, 0, 0], "прогресс изучения перенесён")
            db.upsert_messages([row(-1, 6, "новое")])
            self.assertEqual(len(db.search_messages("новое")), 1)
            db.conn.close()


if __name__ == "__main__":
    unittest.main()
