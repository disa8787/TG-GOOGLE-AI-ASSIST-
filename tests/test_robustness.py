"""Надёжность и гибкость: понятные ошибки, журнал сбоев, одна копия, коды выхода, свои команды и задачи."""

from __future__ import annotations

import asyncio
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import assistant.__main__ as cli
import assistant.bot as bot_mod
from assistant.brain import Assistant
from assistant.config import ScheduledTask, load_config
from assistant.doctor import run_doctor
from assistant.jobs import Scheduler
from assistant.runtime import (
    EXIT_CRASH,
    EXIT_NEEDS_USER,
    InstanceLock,
    NeedsUser,
    explain,
    last_crash,
    write_crash_log,
)
from assistant.telegram_archive import proxy_settings
from tests.test_core import fake_client, text_resp
from tests.test_review_fixes import FakeTG, group, tl_msg
from tests.test_startup_flow import FakeBot


def make_root(text: str = "timezone: UTC\n") -> Path:
    root = Path(tempfile.mkdtemp())
    (root / "config.yaml").write_text(text, encoding="utf-8")
    return root


class RuntimeTests(unittest.TestCase):
    def test_known_errors_are_explained(self):
        from telethon import errors

        cases = [
            (sqlite3.OperationalError("database is locked"), True, "другом окне"),
            (errors.AccessTokenInvalidError(request=None), True, "BOT_TOKEN"),
            (errors.ApiIdInvalidError(request=None), True, "TG_API_ID"),
            (errors.AuthKeyUnregisteredError(request=None), True, "Войди заново"),
            (ConnectionError("x"), False, "Нет связи"),
            (NeedsUser("сам"), True, "сам"),
        ]
        for exc, needs_user, word in cases:
            with self.subTest(exc=type(exc).__name__):
                message, nu = explain(exc)
                self.assertEqual(nu, needs_user)
                self.assertIn(word, message)
        self.assertIsNone(explain(ValueError("неизвестно")))
        self.assertIsNone(explain(FileNotFoundError("нет файла")), "не путать с «нет связи»")

    def test_single_instance(self):
        d = Path(tempfile.mkdtemp())
        with InstanceLock(d):
            with self.assertRaises(NeedsUser):
                with InstanceLock(d):
                    pass
        with InstanceLock(d):  # после закрытия — снова можно
            pass

    def test_crash_log(self):
        d = Path(tempfile.mkdtemp())
        try:
            raise RuntimeError("что-то сломалось")
        except RuntimeError as e:
            path = write_crash_log(d, e, "run")
        self.assertIn("что-то сломалось", path.read_text(encoding="utf-8"))
        self.assertIn("RuntimeError: что-то сломалось", last_crash(d)[1])


class ExitCodeTests(unittest.TestCase):
    def run_main(self, root: Path, argv: list[str], command=None) -> tuple[int, str]:
        out = io.StringIO()
        patches = [mock.patch.object(cli, "ROOT", root),
                   mock.patch.object(cli, "load_config", lambda: load_config(root))]
        if command is not None:
            patches.append(mock.patch.dict(cli.COMMANDS, {argv[0]: command}))
        code = 0
        with patches[0], patches[1], (patches[2] if len(patches) > 2 else mock.patch.object(cli, "sys", cli.sys)):
            with redirect_stdout(out):
                try:
                    cli.main(argv)
                except SystemExit as e:
                    code = e.code or 0
        return code, out.getvalue()

    def test_codes(self):
        empty = Path(tempfile.mkdtemp())
        code, out = self.run_main(empty, ["status"])
        self.assertEqual(code, EXIT_NEEDS_USER)
        self.assertIn("config.yaml", out)

        bad = make_root("telegram:\n  chats: [\n")
        code, out = self.run_main(bad, ["status"])
        self.assertEqual(code, EXIT_NEEDS_USER)
        self.assertIn("строка", out)

        root = make_root()

        async def boom(cfg, args):
            raise ValueError("неожиданное")
        code, out = self.run_main(root, ["status"], boom)
        self.assertEqual(code, EXIT_CRASH, "сбой → start.bat перезапустит")
        self.assertIn("crash.log", out)
        self.assertIn("неожиданное", (root / "data" / "crash.log").read_text(encoding="utf-8"))

        async def needs_user(cfg, args):
            raise NeedsUser("исправь настройку")
        code, out = self.run_main(root, ["status"], needs_user)
        self.assertEqual(code, EXIT_NEEDS_USER, "ошибка настройки → без перезапуска")

        async def offline(cfg, args):
            raise ConnectionError("нет сети")
        code, out = self.run_main(root, ["status"], offline)
        self.assertEqual(code, EXIT_CRASH)
        self.assertIn("Нет связи", out)


class FlexibilityTests(unittest.TestCase):
    def test_about_and_rules_reach_the_model(self):
        root = make_root("timezone: UTC\nassistant:\n  about: |\n    Я диспетчер\n    # пример-комментарий\n"
                         "  instructions: Отвечай коротко\n")
        asst = Assistant(load_config(root))
        text = asst.knowledge_text()
        self.assertIn("Я диспетчер", text)
        self.assertIn("Отвечай коротко", text)
        self.assertNotIn("пример-комментарий", text)
        asst.db.conn.close()

    def test_scheduled_task_runs_once_per_day(self):
        cfg = load_config(make_root())
        cfg.schedule = [ScheduledTask(name="Вечер", time="00:00", days=["mon", "tue", "wed", "thu", "fri", "sat",
                                                                           "sun"], prompt="Сводка дня")]
        asst = Assistant(cfg)
        client, fake = fake_client([text_resp("Итоги: всё спокойно")])
        asst.llm.client = client
        sent = []

        async def notify(text):
            sent.append(text)
        sched = Scheduler(asst, notify, set())
        self.assertEqual(asyncio.run(sched.run_due_tasks()), 1)
        self.assertEqual(asyncio.run(sched.run_due_tasks()), 0, "второй раз за день — нет")
        self.assertIn("Вечер", sent[0])
        self.assertIn("Итоги: всё спокойно", sent[0])
        self.assertIn("Сводка дня", fake.calls[0]["messages"][0]["content"])
        asst.db.conn.close()

    def test_custom_command_in_bot(self):
        root = make_root("timezone: UTC\nwatch:\n  enabled: false\nreport:\n  enabled: false\n"
                         "learn:\n  nightly: false\ncommands:\n  free: Кто освобождается?\n")
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

        async def scenario():
            with mock.patch.object(bot_mod, "connect_user", fake_connect), \
                    mock.patch.object(bot_mod, "make_bot_client", lambda c: fbot):
                app = bot_mod.App(cfg)
                app.db.set_kv("initial_download_done", "2026-01-01 00:00:00")
                app.db.set_kv("learn_cursor:-1", "1")
                client, fake = fake_client([text_resp("Завтра свободен Иван")])
                app.asst.llm.client = client
                run = asyncio.create_task(app.run())
                for _ in range(100):
                    await asyncio.sleep(0.01)
                    if app.phase == "ready":
                        break
                await fbot.incoming("/free завтра")
                await fbot.incoming("/help")
                app.stopping = True
                fbot.stop.set()
                await asyncio.wait_for(run, 5)
                app.db.conn.close()
                return fake
        fake = asyncio.run(scenario())
        prompt = json.dumps(fake.calls[0]["messages"], ensure_ascii=False)
        self.assertIn("Кто освобождается?", prompt)
        self.assertIn("Дополнительно: завтра", prompt)
        self.assertIn("Завтра свободен Иван", fbot.sent)
        self.assertIn("/free — Кто освобождается?", fbot.sent[-1])

    def test_lost_connection_is_a_crash_for_restart(self):
        root = make_root("timezone: UTC\nwatch:\n  enabled: false\nreport:\n  enabled: false\n")
        cfg = load_config(root)
        cfg.bot_token = "999:abc"
        tg = FakeTG()

        async def get_me():
            from telethon.tl import types
            return types.User(id=42, first_name="Денис", is_self=True)
        tg.get_me = get_me
        tg.run_until_disconnected = lambda: asyncio.Event().wait()

        async def fake_connect(cfg_, db, interactive=False):
            return tg
        fbot = FakeBot()

        async def scenario():
            with mock.patch.object(bot_mod, "connect_user", fake_connect), \
                    mock.patch.object(bot_mod, "make_bot_client", lambda c: fbot):
                app = bot_mod.App(cfg)
                app.db.set_kv("initial_download_done", "x")
                app.db.set_kv("learn_cursor:-1", "1")
                run = asyncio.create_task(app.run())
                await asyncio.sleep(0.05)
                fbot.stop.set()  # связь оборвалась сама
                try:
                    await asyncio.wait_for(run, 5)
                finally:
                    app.db.conn.close()
        with self.assertRaises(ConnectionError):
            asyncio.run(scenario())


class ServerModeTests(unittest.TestCase):
    def test_hints_follow_where_it_runs(self):
        from assistant.runtime import how_to
        with mock.patch.dict("os.environ", {"ASSISTANT_SERVER": "1"}):
            self.assertEqual(how_to("login"), "tg-assistant login")
        with mock.patch.dict("os.environ", {"ASSISTANT_SERVER": ""}):
            self.assertNotIn("tg-assistant", how_to("login"))

    def test_moved_to_server_blocks_local_start(self):
        root = make_root()
        (root / "data").mkdir(exist_ok=True)
        (root / "data" / "MOVED_TO_SERVER.txt").write_text("x", encoding="utf-8")
        called = []

        async def run(cfg, args):
            called.append(1)
        code, out = ExitCodeTests.run_main(self, root, ["run"], run)
        self.assertEqual(code, EXIT_NEEDS_USER)
        self.assertIn("перенесён на сервер", out)
        self.assertEqual(called, [], "на компьютере не запускается")
        with mock.patch.dict("os.environ", {"ASSISTANT_SERVER": "1"}):
            code, _ = ExitCodeTests.run_main(self, root, ["run"], run)
        self.assertEqual((code, called), (0, [1]), "на сервере метка не мешает")

    def test_sigterm_is_a_clean_stop(self):
        import os
        import signal
        import subprocess
        import sys
        import textwrap
        root = make_root()
        script = textwrap.dedent(f"""
            import asyncio, sys
            from unittest import mock
            from pathlib import Path
            import assistant.__main__ as cli
            from assistant.config import load_config
            root = Path({str(root)!r})
            async def forever(cfg, args):
                print("started", flush=True)
                await asyncio.sleep(60)
            patches = (mock.patch.object(cli, "ROOT", root),
                       mock.patch.object(cli, "load_config", lambda: load_config(root)),
                       mock.patch.dict(cli.COMMANDS, {{"status": forever}}))
            with patches[0], patches[1], patches[2]:
                cli.main(["status"])
        """)
        proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True,
                                cwd=str(Path(__file__).resolve().parent.parent))
        self.assertEqual(proc.stdout.readline().strip(), "started")
        os.kill(proc.pid, signal.SIGTERM)
        out, _ = proc.communicate(timeout=20)
        self.assertEqual(proc.returncode, 0, "остановка службы — не сбой")
        self.assertIn("Остановлено", out)


class ProxyAndDoctorTests(unittest.TestCase):
    def test_proxy_formats(self):
        self.assertEqual(proxy_settings("")["proxy"] if proxy_settings("") else None, None)
        self.assertEqual(proxy_settings("socks5://127.0.0.1:1080")["proxy"]["port"], 1080)
        secret = "00112233445566778899aabbccddeeff"
        self.assertEqual(proxy_settings(f"https://t.me/proxy?server=h&port=443&secret=dd{secret}")["proxy"],
                         ("h", 443, "dd" + secret))
        with self.assertRaises(NeedsUser):
            proxy_settings(f"tg://proxy?server=h&port=443&secret=ee{secret}")
        with self.assertRaises(NeedsUser):
            proxy_settings("ftp://x:1")

    def test_doctor_offline_lists_problems_without_crashing(self):
        root = make_root("timezone: UTC\ncommands:\n  Плохо: x\n")
        cfg = load_config(root)
        cfg.api_id = cfg.api_hash = cfg.bot_token = None
        out = io.StringIO()
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": ""}), redirect_stdout(out):
            ok = asyncio.run(run_doctor(cfg))
        text = out.getvalue()
        self.assertFalse(ok)
        for word in ("TG_API_ID", "BOT_TOKEN", "ANTHROPIC_API_KEY", "config.yaml", "Архив"):
            self.assertIn(word, text)


if __name__ == "__main__":
    unittest.main()
