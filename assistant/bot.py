"""Основной режим: слушаем рабочие чаты, общаемся с владельцем через бота, работают отчёты и напоминания."""

from __future__ import annotations

import asyncio
import base64
import logging
import sys
import threading
import time

from telethon import TelegramClient, events

from .brain import Assistant
from .config import Config
from .jobs import Scheduler
from .learn import learn_estimate, learn_everything
from .llm import LLMError
from .telegram_archive import (
    ChatFilter,
    attach_live_listener,
    connect_user,
    forward_name,
    sync_chats,
)
from .util import fmt_local, from_db, split_text, to_db, utcnow

log = logging.getLogger(__name__)

HELP = """\
Я — твой «второй я»: знаю всё из твоего Telegram, таблиц и памяти и ничего не забываю. \
От твоего имени я ничего не пишу — твой Telegram для меня только на чтение.

Пиши вопросы обычным текстом, например:
• Где сейчас Иван и когда он выгружается?
• Кто освобождается завтра и где?
• Что я обещал Марии на прошлой неделе?
• Кто ждёт от меня ответа?
• Напомни в 15:00 запросить апдейт у Сергея
• Набросай ответ брокеру по грузу 12345 — как бы написал я
• Запомни: у Петра трак 512, он не ездит в Нью-Йорк

Можно присылать фото и PDF (рейт-коны, BOL, скриншоты) и пересылать сообщения из чатов.

Команды:
/report — сделать отчёт прямо сейчас
/check — проверить новые сообщения и таблицы сейчас
/reminders — активные напоминания
/memory [текст] — что я помню (или поиск по памяти)
/new — начать новую тему (контекст разговора сбросится, память останется)
/download — скачать всю историю Telegram заново / докачать новое (с прогрессом)
/learn — изучить неизученную историю (сначала покажу стоимость)
/status — состояние ассистента
/usage — расходы на Claude"""

IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


async def send(client: TelegramClient, to, text: str) -> None:
    for part in split_text(text) or ["(пусто)"]:
        try:
            await client.send_message(to, part, parse_mode="md", link_preview=False)
        except Exception:  # noqa: BLE001 — если разметка не понравилась Telegram, шлём простым текстом
            await client.send_message(to, part, parse_mode=None, link_preview=False)


async def read_attachments(bot: TelegramClient, msg) -> tuple[list[dict], list[str]]:
    """Фото и PDF от владельца → блоки для Claude. Возвращает (блоки, текстовые пометки)."""
    blocks, notes = [], []
    if msg.photo:
        data = await bot.download_media(msg, file=bytes)
        blocks.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/jpeg", "data": base64.standard_b64encode(data).decode()}})
        notes.append("[фото]")
    elif msg.document and not msg.sticker:
        mime = (msg.file.mime_type or "").lower()
        size = msg.file.size or 0
        name = msg.file.name or "файл"
        if mime in IMAGE_TYPES and size <= 5 * 1024 * 1024:
            data = await bot.download_media(msg, file=bytes)
            blocks.append({"type": "image", "source": {
                "type": "base64", "media_type": mime, "data": base64.standard_b64encode(data).decode()}})
            notes.append(f"[изображение {name}]")
        elif mime == "application/pdf" and size <= 25 * 1024 * 1024:
            data = await bot.download_media(msg, file=bytes)
            blocks.append({"type": "document", "source": {
                "type": "base64", "media_type": "application/pdf", "data": base64.standard_b64encode(data).decode()}})
            notes.append(f"[PDF {name}]")
        elif (mime.startswith("text/") or name.lower().endswith((".txt", ".csv"))) and size <= 1024 * 1024:
            data = await bot.download_media(msg, file=bytes)
            notes.append(f"[файл {name}]:\n{data.decode('utf-8', errors='replace')}")
        else:
            notes.append(f"[файл {name} — такой тип я пока не умею читать]")
    elif msg.voice or msg.video_note:
        notes.append("[голосовое сообщение — слушать пока не умею]")
    return blocks, notes


PROGRESS_EVERY_SECONDS = 90  # как часто присылать прогресс в Telegram во время долгих операций
YES_WORDS = {"да", "д", "y", "yes", "ага", "давай"}


class App:
    """Основной режим. Порядок первого запуска: вход → скачать ВСЮ историю → изучить её → бот работает.
    Пока идёт скачивание/изучение, бот отвечает, на каком он этапе."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.asst = Assistant(cfg)
        self.db = self.asst.db
        self.user: TelegramClient | None = None
        self.bot: TelegramClient | None = None
        self.owner_id = 0
        self.chat_filter = ChatFilter(cfg, self.db)
        self.busy: set[int] = set()  # чаты, история которых сейчас скачивается
        last_online = self.db.get_kv("last_online")
        # пока не докачано пропущенное, новые чаты тоже считают «новым» всё, что пришло после выключения
        self.online_state = {"since": from_db(last_online) if last_online else None}
        self.tasks: list[asyncio.Task] = []
        self.heartbeat_started = False
        self.sched = Scheduler(self.asst, self.notify, set())
        self.phase = "starting"  # starting → download → learn → ready
        self.progress = "запускаюсь"
        self.download_task: asyncio.Task | None = None
        self.learn_task: asyncio.Task | None = None
        self.learn_decision: asyncio.Future | None = None

    # ------------------------------------------------------------------ уведомления
    async def notify(self, text: str) -> None:
        if self.bot is not None:
            await send(self.bot, self.owner_id, text)
            return
        print("\n" + text + "\n", flush=True)
        _append_notification(self.cfg, text)

    async def safe_notify(self, text: str) -> None:
        try:
            await self.notify(text)
        except Exception:  # noqa: BLE001
            log.warning("Не удалось отправить сообщение владельцу (нажми /start у бота)")

    def say(self, text: str) -> None:
        """Прогресс: в консоль и в «что сейчас делаю» для ответов бота."""
        print(text, flush=True)
        self.progress = text.strip()

    # ------------------------------------------------------------------ скачивание
    async def sync(self, on_chat=None) -> int:
        """Докачка (в режиме «все чаты» — и новые чаты целиком). После первой успешной — отметка «в сети»."""
        n = await sync_chats(self.user, self.db, self.cfg, self.chat_filter, self.busy,
                             online_since=self.online_state["since"], on_chat=on_chat)
        self.online_state["since"] = None
        if not self.heartbeat_started:
            self.heartbeat_started = True
            self.tasks.append(asyncio.create_task(self.sched.heartbeat_loop(), name="heartbeat"))
        return n

    async def download_all(self, *, to_bot: bool) -> int:
        """Скачать/докачать всю историю с понятным прогрессом. Возвращает число новых сообщений."""
        last_sent = time.monotonic()
        total_new = 0

        def on_chat(i: int, total: int, title: str, n: int) -> None:
            nonlocal total_new, last_sent
            total_new += n
            st = self.db.message_stats()
            self.say(f"📥 Чат {i} из {total}: «{title}» +{n}. В архиве {st['n']} сообщений.")
            if to_bot and time.monotonic() - last_sent > PROGRESS_EVERY_SECONDS:
                last_sent = time.monotonic()
                asyncio.create_task(self.safe_notify(f"⏳ {self.progress}"))

        self.say("📥 Скачиваю историю Telegram (только чтение, ничего не отправляю и не отмечаю прочитанным)…")
        await self.sync(on_chat=on_chat)
        return total_new

    async def first_download(self) -> None:
        """Самый первый запуск: сначала вся история, без неё ассистент ничего не знает. Нет связи — повторяем."""
        self.phase = "download"
        await self.safe_notify("⏳ Начинаю скачивать всю историю твоего Telegram — без неё я ничего не знаю. "
                               "Это может занять от нескольких минут до пары часов. Буду присылать прогресс.")
        attempt = 0
        while True:
            try:
                await self.download_all(to_bot=True)
                break
            except Exception:  # noqa: BLE001
                log.exception("Ошибка скачивания истории — повторю")
                attempt += 1
                self.say(f"⚠️ Скачивание прервалось (нет связи?). Повтор через минуту, попытка {attempt + 1}…")
                await asyncio.sleep(min(300, 30 * 2 ** min(attempt, 4)))
        st = self.db.message_stats()
        chats = len([c for c in self.db.chats() if c["monitored"]])
        if not st["n"]:
            if self.cfg.all_chats:
                hint = ("Похоже, все чаты отфильтрованы настройками (exclude_chats / include_*) — "
                        "проверь assistant chats.")
            else:
                hint = ("В config.yaml указан список чатов (telegram.chats), и ни один не найден. "
                        "Поставь там chats: all — тогда я скачаю все чаты.")
            msg = f"⚠️ Ничего не скачалось. {hint} После правки перезапусти программу."
            self.say(msg)
            await self.safe_notify(msg)
            return  # отметку «скачано» не ставим — при следующем запуске попробуем снова
        self.db.set_kv("initial_download_done", to_db(utcnow()))
        msg = f"✅ История скачана: {st['n']} сообщений из {chats} чатов."
        self.say(msg)
        await self.safe_notify(msg)

    # ------------------------------------------------------------------ изучение
    def learn_offer_text(self, est: dict) -> str:
        return (f"📚 Чтобы понять твою работу и все её мелочи, мне нужно изучить историю: "
                f"{est['messages']} сообщений из {est['chats']} чатов ({est['parts']} частей). "
                f"Примерная стоимость ≈ ${est['cost']:.2f} (модель {est['model']}).\n"
                "Начать? Ответь /learn_yes (или «да» в окне программы). Отложить — /learn_no")

    def _ask_console(self, question: str) -> None:
        """Вопрос в окне программы — в отдельном фоновом потоке, чтобы не мешать боту."""
        if sys.stdin is None or not sys.stdin.isatty():
            return
        loop = asyncio.get_running_loop()

        def worker() -> None:
            try:
                answer = input(question)
            except (EOFError, KeyboardInterrupt, OSError):
                return
            loop.call_soon_threadsafe(self._decide, answer.strip().lower() in YES_WORDS)

        threading.Thread(target=worker, daemon=True).start()

    def _decide(self, yes: bool) -> None:
        if self.learn_decision is not None and not self.learn_decision.done():
            self.learn_decision.set_result(yes)

    async def learn_all(self) -> None:
        """Изучение (стоимость уже подтверждена) с прогрессом в консоль и в Telegram."""
        last_sent = time.monotonic()

        def say(text: str) -> None:
            nonlocal last_sent
            self.say(text)
            if time.monotonic() - last_sent > PROGRESS_EVERY_SECONDS:
                last_sent = time.monotonic()
                asyncio.create_task(self.safe_notify(f"⏳ Изучаю: {self.progress}"))

        ok = await learn_everything(self.asst, say)
        if not ok:
            await self.safe_notify(f"⚠️ Изучение остановилось: {self.progress}\nПрогресс сохранён, продолжить — /learn")
            return
        profile = self.asst.profile() or ""
        excerpt = profile[:1500] + ("…" if len(profile) > 1500 else "")
        await self.safe_notify(
            f"✅ Изучил историю. В памяти {self.db.memory_count()} заметок.\n\n"
            f"Вот как я понял твою работу:\n\n{excerpt}\n\n"
            "Полностью — в файле data\\profile.md. Если что-то не так — поправь файл или просто напиши мне."
        )

    async def startup_learning(self) -> None:
        """Первое изучение: спрашиваем стоимость и ждём ответа (в окне программы или в боте)."""
        est = learn_estimate(self.asst)
        if est["parts"] == 0:
            return
        self.phase = "learn"
        self.progress = "жду твоего решения об изучении истории"
        self.learn_decision = asyncio.get_running_loop().create_future()
        offer = self.learn_offer_text(est)
        print("\n" + offer.split("\nНачать?")[0], flush=True)
        await self.safe_notify(offer)
        self._ask_console("Изучить сейчас? Напиши «да» и нажми Enter (или «нет» — отложить): ")
        if self.bot is None and (sys.stdin is None or not sys.stdin.isatty()):
            return  # спросить некого — изучение можно запустить позже командой learn
        if await self.learn_decision:
            self.progress = "изучаю историю"
            await self.learn_all()
        else:
            await self.safe_notify("Ок, отложил. Изучить историю можно в любой момент командой /learn")

    def start_background_learning(self) -> str:
        if self.learn_task is not None and not self.learn_task.done():
            return f"⏳ Уже изучаю: {self.progress}"
        self.learn_task = asyncio.create_task(self.learn_all(), name="learn")
        return "Начинаю изучать историю. Буду присылать прогресс, по окончании покажу, что понял."

    # ------------------------------------------------------------------ бот
    def status_prefix(self) -> str:
        names = {"download": "скачиваю историю", "learn": "изучаю историю", "starting": "запускаюсь"}
        if self.phase in names:
            return f"⏳ Сейчас я {names[self.phase]}: {self.progress}\n\n"
        return ""

    def register_bot_handlers(self) -> None:
        bot, asst, cfg = self.bot, self.asst, self.cfg

        async def run_with_typing(chat_id, coro):
            async with bot.action(chat_id, "typing"):
                return await coro

        @bot.on(events.NewMessage(incoming=True))
        async def on_message(event) -> None:
            if not event.is_private:
                return
            if event.sender_id != self.owner_id:
                await event.reply("Это личный ассистент, он отвечает только своему владельцу.")
                return
            msg = event.message
            text = (msg.message or "").strip()
            cmd = text.split()[0].lower().split("@")[0] if text.startswith("/") else ""
            arg = text.split(maxsplit=1)[1] if cmd and len(text.split(maxsplit=1)) > 1 else ""
            chat_id = event.chat_id
            try:
                # --- команды, которые работают всегда
                if cmd in ("/start", "/help"):
                    await send(bot, chat_id, self.status_prefix() + HELP)
                    return
                if cmd == "/status":
                    await send(bot, chat_id, self.status_prefix() + asst.status_text())
                    return
                if cmd == "/usage":
                    await send(bot, chat_id, asst.usage_text())
                    return
                if cmd in ("/learn_yes", "/learn_no") and self.learn_decision is not None \
                        and not self.learn_decision.done():
                    self._decide(cmd == "/learn_yes")
                    await send(bot, chat_id, "Начинаю изучать историю…" if cmd == "/learn_yes" else "Ок, отложил.")
                    return
                if self.phase != "ready":
                    # история ещё не скачана/не изучена — отвечать по ней рано
                    await send(bot, chat_id, self.status_prefix() + "Как закончу — напишу. "
                               "Можно посмотреть /status или расходы /usage.")
                    return

                # --- обычная работа
                if cmd == "/report":
                    await send(bot, chat_id, "Готовлю отчёт, это займёт пару минут…")
                    answer = await run_with_typing(chat_id, asst.daily_report())
                    await send(bot, chat_id, "📋 **Отчёт**\n\n" + answer)
                elif cmd == "/check":
                    note = await run_with_typing(chat_id, asst.watch_check(self.sched.ignored))
                    await send(bot, chat_id, note or "Ничего важного с прошлой проверки ✅")
                elif cmd == "/reminders":
                    await send(bot, chat_id, asst.tools.t_list_reminders())
                elif cmd == "/memory":
                    rows = asst.db.search_memory(arg) if arg else sorted(
                        asst.db.all_memory(), key=lambda r: r["updated_at"], reverse=True)[:30]
                    if not rows:
                        await send(bot, chat_id, "Ничего не найдено.")
                    else:
                        head = f"В памяти {asst.db.memory_count()} заметок." + (
                            "" if arg else " Последние обновлённые:")
                        body = "\n\n".join(
                            f"#{r['id']} **{r['subject']}** ({r['category']}, "
                            f"{fmt_local(r['updated_at'], cfg.tz)[:10]})\n{r['content']}" for r in rows)
                        await send(bot, chat_id, f"{head}\n\n{body}")
                elif cmd == "/new":
                    asst.reset_dialog()
                    await send(bot, chat_id, "Ок, начинаем с чистого листа. Память и архив на месте.")
                elif cmd in ("/download", "/sync"):
                    if self.download_task is not None and not self.download_task.done():
                        await send(bot, chat_id, f"⏳ Уже скачиваю: {self.progress}")
                        return
                    await send(bot, chat_id, "📥 Скачиваю/докачиваю историю всех чатов. Буду присылать прогресс.")

                    async def job() -> None:
                        try:
                            n = await self.download_all(to_bot=True)
                            st = self.db.message_stats()
                            await self.safe_notify(f"✅ Готово: +{n} сообщений, всего в архиве {st['n']}.")
                            est = learn_estimate(self.asst)
                            if est["parts"]:
                                await self.safe_notify(f"Неизученного: {est['messages']} сообщений "
                                                       f"(≈ ${est['cost']:.2f}). Изучить — /learn")
                        except Exception as e:  # noqa: BLE001
                            log.exception("Ошибка скачивания")
                            await self.safe_notify(f"⚠️ Скачивание прервалось: {e}. Повтори /download")
                    self.download_task = asyncio.create_task(job(), name="download")
                elif cmd == "/learn":
                    est = learn_estimate(self.asst)
                    if est["parts"] == 0:
                        await send(bot, chat_id, "Вся скачанная история уже изучена ✅")
                    else:
                        self.learn_decision = asyncio.get_running_loop().create_future()

                        async def wait_decision(fut) -> None:
                            if await fut:
                                await self.safe_notify(self.start_background_learning())
                        asyncio.create_task(wait_decision(self.learn_decision))
                        await send(bot, chat_id, self.learn_offer_text(est).replace(
                            " (или «да» в окне программы)", ""))
                else:
                    blocks, notes = await read_attachments(bot, msg)
                    if msg.fwd_from:
                        notes.insert(0, f"(переслано от {forward_name(msg)})")
                    full = "\n".join(notes + ([text] if text else []))
                    if not full and not blocks:
                        return
                    answer = await run_with_typing(chat_id, asst.chat(full or "(вложение)", blocks))
                    await send(bot, chat_id, answer)
            except LLMError as e:
                await send(bot, chat_id, f"⚠️ {e}")
            except Exception as e:  # noqa: BLE001
                log.exception("Ошибка обработки сообщения")
                await send(bot, chat_id, f"⚠️ Что-то пошло не так: {type(e).__name__}: {e}")

    # ------------------------------------------------------------------ запуск
    async def catch_up(self) -> None:
        """Обычный запуск: докачка пропущенного в фоне. Не получилось — повторяем с паузами."""
        attempt, told = 0, False
        while True:
            try:
                new = await self.sync()
            except Exception:  # noqa: BLE001
                log.exception("Ошибка синхронизации чатов — повторю")
                if not told:
                    told = True
                    await self.safe_notify("⚠️ Не получилось докачать пропущенное (нет связи?). Повторю автоматически.")
                attempt += 1
                await asyncio.sleep(min(300, 30 * 2 ** min(attempt, 4)))
                continue
            log.info("Синхронизация завершена: новых сообщений %s", new)
            if new:
                await self.safe_notify(f"🔄 Докачал пропущенное: {new} новых сообщений.")
            return

    async def run(self) -> None:
        cfg = self.cfg
        self.user = await connect_user(cfg, self.db, interactive=True)  # аккаунт владельца — только чтение
        me = await self.user.get_me()
        self.owner_id = cfg.owner_id or me.id
        self.chat_filter.apply_to_db(self.db)

        if cfg.bot_token:
            self.bot = TelegramClient(str(cfg.data_dir / "bot"), cfg.api_id, cfg.api_hash)
            await self.bot.start(bot_token=cfg.bot_token)
            self.register_bot_handlers()
        else:
            log.warning("BOT_TOKEN не задан — уведомления будут только в этом окне и в data/notifications.log. "
                        "От имени твоего аккаунта ассистент не пишет никогда.")

        # слушаем сразу, чтобы не пропустить сообщения, пока скачивается история
        attach_live_listener(self.user, self.db, cfg, self.chat_filter, self.busy, self.online_state)
        waiters = [asyncio.create_task(self.user.run_until_disconnected())]
        if self.bot is not None:
            waiters.append(asyncio.create_task(self.bot.run_until_disconnected()))
        try:
            mode = "все чаты" if cfg.all_chats else f"{len(cfg.chats)} выбранных чатов"
            print(f"Режим: {mode}. Твой Telegram — только чтение.", flush=True)

            # 1) история: в самый первый раз — целиком и до запуска работы
            first_run = self.db.get_kv("initial_download_done") is None
            if first_run:
                await self.first_download()
            # 2) изучение: пока ни разу не изучали — предлагаем (стоимость показываем)
            if not self.db.learning_started():
                await self.startup_learning()
            # 3) работа
            self.phase = "ready"
            self.progress = "готов"
            self.tasks += self.sched.tasks()
            if not first_run:
                self.tasks.append(asyncio.create_task(self.catch_up(), name="catch-up"))
            print("✅ Ассистент работает. Пиши боту в Telegram. Остановить — закрой окно или Ctrl+C.", flush=True)
            await self.safe_notify("✅ Я на связи и в курсе всего. /help — что я умею.")
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in self.tasks + waiters:
                t.cancel()


def _append_notification(cfg: Config, text: str) -> None:
    with open(cfg.data_dir / "notifications.log", "a", encoding="utf-8") as f:
        f.write(f"\n----- {fmt_local(utcnow(), cfg.tz)}\n{text}\n")


async def run_app(cfg: Config) -> None:
    await App(cfg).run()
