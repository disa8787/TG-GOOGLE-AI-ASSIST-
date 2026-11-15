"""Готовность к работе на сервере 24/7: обрывы связи, одна копия изучения, понятные сообщения владельцу,
опечатки в настройках, журнал сбоев, проверка диска и перенос данных."""

from __future__ import annotations

import asyncio
import io
import os
import sqlite3
import tempfile
import time
import unittest
from collections import namedtuple
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import assistant.__main__ as cli
import assistant.bot as bot_mod
from assistant.brain import Assistant
from assistant.config import load_config
from assistant.db import DB
from assistant.doctor import Report, check_disk
from assistant.jobs import Scheduler
from assistant.learn import LearnBusy, learning_guard, run_learn
from assistant.runtime import (
    CRASH_LOG_MAX_BYTES,
    EXIT_NEEDS_USER,
    AlreadyRunning,
    FileLock,
    InstanceLock,
    NeedsUser,
    data_file_hint,
    explain,
    last_crash,
    write_crash_log,
)
from tests import test_robustness
from tests.test_robustness import make_root
from tests.test_startup_flow import FakeBot

QUIET = "timezone: UTC\nwatch:\n  enabled: false\nreport:\n  enabled: false\nlearn:\n  nightly: false\n"


def owner():
    from telethon.tl import types
    return types.User(id=42, first_name="Денис", is_self=True)


class ConfigTypoTests(unittest.TestCase):
    """Опечатки в config.yaml не роняют программу на сервере, а превращаются в понятные предупреждения."""

    def test_yes_no_words_and_bad_numbers(self):
        cfg = load_config(make_root(
            "timezone: UTC\nreport:\n  enabled: нет\nwatch:\n  enabled: \"false\"\n  interval_minutes: часто\n"
            "telegram:\n  media_max_mb: двадцать\n  include_channels: да\n  download_media: может\n"))
        self.assertFalse(cfg.report_enabled, "«нет» — это выключено (а не непустая строка = True)")
        self.assertFalse(cfg.watch_enabled)
        self.assertTrue(cfg.include_channels)
        self.assertFalse(cfg.download_media, "непонятное значение — стандартное")
        self.assertEqual(cfg.media_max_mb, 20)
        self.assertEqual(cfg.watch_interval, 60)
        text = "\n".join(cfg.warnings)
        for word in ("media_max_mb", "interval_minutes", "download_media"):
            self.assertIn(word, text)

    def test_days_in_russian_and_groups(self):
        cfg = load_config(make_root(
            "timezone: UTC\nreport:\n  days: \"пн, ср, пятница\"\n"
            "schedule:\n  - name: Сверка\n    time: \"8.30\"\n    days: будни\n    prompt: x\n"
            "  - name: Выходные\n    days: [сб, Вс]\n    prompt: y\n  - name: Ошибка\n    days: [пнд, xyz]\n"
            "    prompt: z\n"))
        self.assertEqual(cfg.report_days, ["mon", "wed", "fri"])
        self.assertEqual(cfg.schedule[0].days, ["mon", "tue", "wed", "thu", "fri"])
        self.assertEqual(cfg.schedule[0].time, "08:30", "8.30 тоже понимаем")
        self.assertEqual(cfg.schedule[1].days, ["sat", "sun"])
        self.assertIn("mon", cfg.schedule[2].days, "«пнд» — это понедельник")
        self.assertTrue(any("xyz" in w for w in cfg.warnings))
        for text, days in (("пн-пт", ["mon", "tue", "wed", "thu", "fri"]), ("fri - mon", ["mon", "fri", "sat", "sun"]),
                           ("Вт–Чт", ["tue", "wed", "thu"])):
            cfg = load_config(make_root(f"timezone: UTC\nreport:\n  days: \"{text}\"\n"))
            self.assertEqual(cfg.report_days, days, text)

    def test_section_written_as_value_and_sheet_as_link(self):
        cfg = load_config(make_root(
            "timezone: UTC\nreport: false\ngoogle:\n  sheets:\n    - https://docs.google.com/spreadsheets/d/abc\n"
            "watch:\n  active_hours: 07:00 – 22:00\n"))
        self.assertTrue(any(w.startswith("report:") for w in cfg.warnings))
        self.assertEqual(cfg.report_time, "08:00", "раздел взят стандартный, программа не упала")
        self.assertEqual([s.url for s in cfg.sheets], ["https://docs.google.com/spreadsheets/d/abc"])
        self.assertEqual(cfg.watch_hours, ("07:00", "22:00"), "длинное тире тоже понимаем")
        self.assertEqual(cfg.resync_minutes, 30)

    def test_impossible_time_is_a_clear_error(self):
        with self.assertRaises(SystemExit) as ctx:
            load_config(make_root("timezone: UTC\nreport:\n  time: \"25:70\"\n"))
        self.assertIn("ЧЧ:ММ", str(ctx.exception.code))


class RuntimeServerTests(unittest.TestCase):
    def test_busy_database_restarts_and_readonly_asks_for_help(self):
        message, needs_user = explain(sqlite3.OperationalError("database is locked"))
        self.assertFalse(needs_user, "временная занятость базы не должна навсегда останавливать службу")
        message, needs_user = explain(sqlite3.OperationalError("attempt to write a readonly database"))
        self.assertTrue(needs_user)
        self.assertIn("прав", message)
        with mock.patch.dict(os.environ, {"ASSISTANT_SERVER": "1"}):
            self.assertIn("tg-assistant restart", explain(PermissionError("data/assistant.db"))[0])

    def test_already_running_is_needs_user_and_server_wording(self):
        d = Path(tempfile.mkdtemp())
        with InstanceLock(d), mock.patch.dict(os.environ, {"ASSISTANT_SERVER": "1"}):
            with self.assertRaises(AlreadyRunning) as ctx:
                with InstanceLock(d):
                    pass
        self.assertIsInstance(ctx.exception, NeedsUser)
        self.assertIn("tg-assistant", str(ctx.exception))

    def test_crash_log_does_not_grow_forever(self):
        d = Path(tempfile.mkdtemp())
        (d / "crash.log").write_text("старый сбой\n" * 200_000, encoding="utf-8")
        try:
            raise RuntimeError("свежий сбой")
        except RuntimeError as e:
            write_crash_log(d, e, "run")
        self.assertLess((d / "crash.log").stat().st_size, CRASH_LOG_MAX_BYTES)
        self.assertIn("свежий сбой", last_crash(d)[1])

    def test_file_hints(self):
        with mock.patch.dict(os.environ, {"ASSISTANT_SERVER": "1"}):
            self.assertTrue(data_file_hint("crash.log").endswith(os.path.join("data", "crash.log")))
            self.assertTrue(os.path.isabs(data_file_hint("crash.log")), "на сервере — полный путь")


class LearnOnceTests(unittest.TestCase):
    """Изучение идёт только одно: иначе одни и те же сообщения ушли бы в Claude дважды."""

    def setUp(self):
        self.cfg = load_config(make_root(QUIET))
        self.asst = Assistant(self.cfg)

    def tearDown(self):
        self.asst.db.conn.close()

    def test_second_learning_in_same_process_waits_its_turn(self):
        async def scenario():
            async with learning_guard(self.asst):
                with self.assertRaises(LearnBusy):
                    async with learning_guard(self.asst):
                        pass
            async with learning_guard(self.asst):  # после окончания — снова можно
                pass
        asyncio.run(scenario())

    def test_learning_in_another_command_blocks(self):
        other = FileLock(self.cfg.data_dir / "learn.lock")
        self.assertTrue(other.try_acquire())
        try:
            with self.assertRaises(NeedsUser) as ctx:
                asyncio.run(run_learn(self.asst, chats=None, reset=False, yes=True, sheets_only=False, model=None))
            self.assertIn("уже идёт", str(ctx.exception))
        finally:
            other.release()

    def test_nightly_skips_while_owner_learning(self):
        self.asst.db.set_kv("learn_cursor:-1", "1")
        sched = Scheduler(self.asst, lambda text: asyncio.sleep(0), set())
        called = []

        async def fake_learn(*a, **k):
            called.append(1)
            return True

        async def scenario():
            with mock.patch("assistant.jobs.learn_history", fake_learn):
                async with learning_guard(self.asst):
                    await sched.nightly_once()
                self.assertEqual(called, [], "пока идёт изучение по кнопке — ночное не запускается")
                await sched.nightly_once()
                self.assertEqual(called, [1])
        asyncio.run(scenario())


class HangingTG:
    """Аккаунт Telegram, у которого посреди первой выгрузки пропала связь."""

    def __init__(self, drop_after: float = 0.05):
        self.drop_after = drop_after

    async def get_me(self):
        return owner()

    async def run_until_disconnected(self):
        await asyncio.sleep(self.drop_after)

    def add_event_handler(self, *a, **k):
        pass

    async def _dialogs(self):
        await asyncio.Event().wait()  # список чатов так и не пришёл
        yield None

    def iter_dialogs(self, *a, **k):
        return self._dialogs()


class AppServerTests(unittest.TestCase):
    def make_app(self, text: str = QUIET):
        cfg = load_config(make_root(text))
        cfg.bot_token = "999:abc"
        app = bot_mod.App(cfg)
        self.addCleanup(app.db.conn.close)
        return app

    def test_disconnect_during_first_download_exits_for_restart(self):
        app = self.make_app()
        fbot = FakeBot()

        async def fake_connect(cfg_, db, interactive=False):
            return HangingTG()

        async def scenario():
            with mock.patch.object(bot_mod, "connect_user", fake_connect), \
                    mock.patch.object(bot_mod, "make_bot_client", lambda c: fbot):
                await asyncio.wait_for(app.run(), 5)

        started = time.monotonic()
        with self.assertRaises(ConnectionError):
            asyncio.run(scenario())
        self.assertLess(time.monotonic() - started, 5, "не висим вечно на мёртвом соединении")

    def test_syncs_never_overlap_and_resync_skips_reconcile(self):
        app = self.make_app()
        app.cfg.resync_minutes = 0.001  # ≈ 0.06 секунды
        active, peak, calls = 0, 0, []

        async def fake_sync(*a, reconcile=True, **k):
            nonlocal active, peak
            calls.append(reconcile)
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1
            return 0

        async def scenario():
            with mock.patch.object(bot_mod, "sync_chats", fake_sync):
                await asyncio.gather(app.sync(), app.sync(), app.download_all(to_bot=False))
                loop = asyncio.create_task(app.resync_loop())
                await asyncio.sleep(0.3)
                loop.cancel()
                for t in app.tasks:
                    t.cancel()
        asyncio.run(scenario())
        self.assertEqual(peak, 1, "докачки идут по очереди")
        self.assertIn(False, calls[3:], "периодическая сверка — без перечитывания последних дней")

    def test_same_crash_is_not_reported_again_and_again(self):
        app = self.make_app()
        sent = []

        async def notify(text):
            sent.append(text)
        app.notify = notify
        data = app.cfg.data_dir

        def crash(text: str, shift: int) -> None:
            try:
                raise ConnectionError(text)
            except ConnectionError as e:
                write_crash_log(data, e, "run")
            os.utime(data / "crash.log", (time.time() + shift, time.time() + shift))

        async def scenario():
            crash("Связь с Telegram потеряна", 1)
            await app.report_previous_crash()
            crash("Связь с Telegram потеряна", 2)
            await app.report_previous_crash()
            self.assertEqual(len(sent), 1, "тот же сбой через минуту — без нового сообщения")
            crash("Другая ошибка", 3)
            await app.report_previous_crash()
            self.assertEqual(len(sent), 2)
            app.db.set_kv("crash_reported_at", "0")  # прошло много часов
            crash("Другая ошибка", 4)
            await app.report_previous_crash()
            self.assertEqual(len(sent), 3)
        asyncio.run(scenario())
        self.assertIn("crash.log", sent[0])

    def test_learning_errors_are_reported_not_lost(self):
        app = self.make_app()
        sent = []

        async def notify(text):
            sent.append(text)
        app.notify = notify

        async def broken(asst, say, model=None):
            raise RuntimeError("сеть пропала")

        async def busy(asst, say, model=None):
            raise LearnBusy("Изучение уже идёт")

        async def scenario():
            with mock.patch.object(bot_mod, "learn_everything", broken):
                await app.learn_all()
            with mock.patch.object(bot_mod, "learn_everything", busy):
                await app.learn_all()
        asyncio.run(scenario())
        self.assertIn("Изучение прервалось", sent[0])
        self.assertIn("/learn", sent[0])
        self.assertIn("уже идёт", sent[1])


class OwnerToldWhenStuckTests(unittest.TestCase):
    """Служба остановилась и сама не поднимется — владелец узнаёт об этом в Telegram, а не из журнала."""

    def run_main(self, root, argv, command):
        return test_robustness.ExitCodeTests.run_main(self, root, argv, command)

    def test_needs_user_in_run_is_reported_through_bot(self):
        root = make_root()
        told = []
        cases = [
            (NeedsUser("Войди заново: tg-assistant login"), EXIT_NEEDS_USER, True),
            (ConnectionError("нет сети"), 1, False),  # служба перезапустится сама — писать не о чем
            (KeyboardInterrupt(), 0, False),
        ]
        for exc, code, expect_told in cases:
            told.clear()

            async def run(cfg, args, exc=exc):
                raise exc
            with self.subTest(exc=type(exc).__name__), \
                    mock.patch.object(cli, "_tell_owner", lambda cfg, text: told.append(text) or True):
                got, _ = self.run_main(root, ["run"], run)
                self.assertEqual(got, code)
                self.assertEqual(bool(told), expect_told)
                if expect_told:
                    self.assertIn("Войди заново", told[0])

    def test_second_copy_does_not_disturb_owner(self):
        root = make_root()
        (root / "data").mkdir(exist_ok=True)
        told = []

        async def run(cfg, args):
            raise AssertionError("вторая копия не должна запуститься")
        with InstanceLock(root / "data"), \
                mock.patch.object(cli, "_tell_owner", lambda cfg, text: told.append(text) or True):
            code, out = self.run_main(root, ["run"], run)
        self.assertEqual(code, EXIT_NEEDS_USER)
        self.assertEqual(told, [])

    def test_tell_owner_uses_bot_and_remembered_owner(self):
        cfg = load_config(make_root())
        cfg.bot_token = "999:abc"
        cfg.owner_id = None
        db = DB(cfg.data_dir / "assistant.db")
        db.set_kv("owner_tg_id", "42")
        db.conn.close()
        sent = []

        class Bot:
            async def start(self, bot_token=None):
                return self

            async def send_message(self, to, text, link_preview=False):
                sent.append((to, text))

            async def disconnect(self):
                return None
        with mock.patch("assistant.telegram_archive.make_bot_client", lambda c: Bot()):
            self.assertTrue(cli._tell_owner(cfg, "проверка"))
        self.assertEqual(sent, [(42, "проверка")])

        class DeadBot(Bot):
            async def start(self, bot_token=None):
                raise ConnectionError("нет сети")
        with mock.patch("assistant.telegram_archive.make_bot_client", lambda c: DeadBot()):
            self.assertFalse(cli._tell_owner(cfg, "проверка"), "не получилось — не падаем")


class MoveToServerTests(unittest.TestCase):
    def test_prepare_move_checks_running_copy_and_flushes_database(self):
        root = make_root()
        cfg = load_config(root)
        db = DB(cfg.data_dir / "assistant.db")
        db.set_kv("x", "1" * 10000)
        wal = cfg.data_dir / "assistant.db-wal"
        self.assertGreater(wal.stat().st_size, 0)
        (cfg.data_dir / "MOVED_TO_SERVER.txt").write_text("x", encoding="utf-8")  # повторный перенос
        code, out = test_robustness.ExitCodeTests.run_main(self, root, ["prepare-move"])
        self.assertEqual(code, 0, out)
        self.assertEqual(wal.stat().st_size, 0, "весь архив — в основном файле базы")
        db.conn.close()
        with InstanceLock(cfg.data_dir):
            code, out = test_robustness.ExitCodeTests.run_main(self, root, ["prepare-move"])
        self.assertEqual(code, EXIT_NEEDS_USER, "ассистент ещё запущен — переносить нельзя")


class DoctorDiskTests(unittest.TestCase):
    def test_low_disk_space_is_a_problem(self):
        cfg = load_config(make_root())
        usage = namedtuple("usage", "total used free")
        for free_gb, ok in ((0.3, False), (25, True)):
            rep = Report()
            out = io.StringIO()
            with mock.patch("assistant.doctor.shutil.disk_usage", lambda p, f=free_gb: usage(0, 0, f * 1024 ** 3)), \
                    redirect_stdout(out):
                check_disk(cfg, rep)
            self.assertIn("Свободно на диске", out.getvalue())
            self.assertEqual(not rep.problems, ok)


if __name__ == "__main__":
    unittest.main()
