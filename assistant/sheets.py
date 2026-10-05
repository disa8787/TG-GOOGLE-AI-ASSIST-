"""Google Таблицы: чтение, форматирование для ИИ, снимки и поиск изменений."""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import re
import time
from dataclasses import dataclass

import gspread

from .config import Config, SheetSource
from .db import DB

log = logging.getLogger(__name__)

CACHE_SECONDS = 60


def col_letter(index: int) -> str:
    """0 → A, 25 → Z, 26 → AA."""
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def normalize(values: list[list[str]]) -> list[list[str]]:
    out = []
    for row in values:
        cells = [str(c).strip() for c in row]
        while cells and not cells[-1]:
            cells.pop()
        out.append(cells)
    while out and not out[-1]:
        out.pop()
    return out


def header_index(values: list[list[str]]) -> int | None:
    for i, row in enumerate(values):
        if any(row):
            return i
    return None


def format_row(i: int, row: list[str]) -> str:
    cells = [c.replace("\n", " / ") for c in row]
    return f"{i + 1}: " + " | ".join(cells)


def format_table(
    values: list[list[str]],
    *,
    start_row: int = 1,
    max_rows: int = 400,
    query: str | None = None,
) -> str:
    """Таблица → текст: «номер строки: ячейка | ячейка | …». Пустые строки пропускаются."""
    values = normalize(values)
    if not values:
        return "(лист пустой)"
    hdr = header_index(values)
    needles = [q.strip().lower() for q in (query or "").split("|") if q.strip()]
    lines, shown, matched, last_shown = [], 0, 0, start_row - 1
    ncols = max(len(r) for r in values)
    lines.append(f"колонки: A…{col_letter(ncols - 1)}, строк в листе: {len(values)}")
    if hdr is not None and (start_row - 1 > hdr or needles):
        lines.append(format_row(hdr, values[hdr]) + "   ← заголовок")
    for i, row in enumerate(values):
        if i + 1 < start_row or not any(row):
            continue
        if needles:
            if i == hdr:
                continue
            hay = " ".join(row).lower()
            if not any(n in hay for n in needles):
                continue
        matched += 1
        if shown >= max_rows:
            continue
        lines.append(format_row(i, row))
        shown += 1
        last_shown = i + 1
    if matched > shown:
        lines.append(f"…показано {shown} из {matched} строк. Продолжение: start_row={last_shown + 1}")
    if needles and matched == 0:
        lines.append(f"(строк с «{query}» не найдено)")
    return "\n".join(lines)


def diff_tables(old: list[list[str]], new: list[list[str]], max_items: int = 60) -> list[str]:
    """Построчное сравнение двух снимков листа (учитывает вставку/удаление строк)."""
    a, b = normalize(old), normalize(new)
    sm = difflib.SequenceMatcher(a=[tuple(r) for r in a], b=[tuple(r) for r in b], autojunk=False)
    out: list[str] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if tag == "replace" and (i2 - i1) == (j2 - j1):
            for k in range(i2 - i1):
                ra, rb = a[i1 + k], b[j1 + k]
                cells = []
                for c in range(max(len(ra), len(rb))):
                    va = ra[c] if c < len(ra) else ""
                    vb = rb[c] if c < len(rb) else ""
                    if va != vb:
                        cells.append(f"{col_letter(c)}: «{va}» → «{vb}»")
                out.append(f"строка {j1 + k + 1} изменена: " + "; ".join(cells))
        else:
            for k in range(i1, i2):
                if any(a[k]):
                    out.append(f"удалена строка (была {k + 1}): " + " | ".join(a[k]))
            for k in range(j1, j2):
                if any(b[k]):
                    out.append(f"добавлена строка {k + 1}: " + " | ".join(b[k]))
    if len(out) > max_items:
        out = out[:max_items] + [f"…и ещё {len(out) - max_items} изменений"]
    return out


@dataclass
class OpenedSheet:
    source: SheetSource
    spreadsheet: gspread.Spreadsheet


class Sheets:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._gc: gspread.Client | None = None
        self._cache: dict[tuple[str, str], tuple[float, list[list[str]]]] = {}
        self._opened: dict[str, gspread.Spreadsheet] = {}

    # ------------------------------------------------------------------ доступ
    @property
    def configured(self) -> bool:
        return bool(self.cfg.sheets) and self.cfg.google_credentials.exists()

    def client(self) -> gspread.Client:
        if self._gc is None:
            cred = self.cfg.google_credentials
            if not cred.exists():
                raise RuntimeError(
                    f"Нет файла ключа Google: {cred}. См. раздел «Google Таблицы» в README."
                )
            if self.cfg.google_auth == "oauth":
                self._gc = gspread.oauth(
                    credentials_filename=str(cred),
                    authorized_user_filename=str(self.cfg.data_dir / "google-token.json"),
                )
            else:
                self._gc = gspread.service_account(filename=str(cred))
        return self._gc

    def service_email(self) -> str | None:
        try:
            with open(self.cfg.google_credentials, encoding="utf-8") as f:
                return json.load(f).get("client_email")
        except Exception:
            return None

    def find_source(self, name_or_url: str) -> SheetSource:
        text = name_or_url.strip()
        low = text.lower()
        for s in self.cfg.sheets:
            if s.name.lower() == low or s.url == text:
                return s
        for s in self.cfg.sheets:
            if low in s.name.lower():
                return s
        if text.startswith("http") or re.fullmatch(r"[A-Za-z0-9_-]{30,}", text):
            return SheetSource(name=text, url=text)
        names = ", ".join(s.name for s in self.cfg.sheets) or "нет"
        raise ValueError(f"Таблица «{text}» не найдена. Настроенные таблицы: {names}")

    def _open(self, source: SheetSource) -> gspread.Spreadsheet:
        if source.url not in self._opened:
            gc = self.client()
            if source.url.startswith("http"):
                self._opened[source.url] = gc.open_by_url(source.url)
            else:
                self._opened[source.url] = gc.open_by_key(source.url)
        return self._opened[source.url]

    def worksheet_titles(self, source: SheetSource) -> list[str]:
        sh = self._open(source)
        titles = [ws.title for ws in sh.worksheets()]
        if source.worksheets:
            wanted = [w for w in source.worksheets if w in titles]
            return wanted or titles
        return titles

    def read(self, source: SheetSource, worksheet: str | None, fresh: bool = False) -> tuple[str, list[list[str]]]:
        sh = self._open(source)
        title = worksheet or self.worksheet_titles(source)[0]
        key = (source.url, title)
        cached = self._cache.get(key)
        if cached and not fresh and time.time() - cached[0] < CACHE_SECONDS:
            return title, cached[1]
        try:
            ws = sh.worksheet(title)
        except gspread.WorksheetNotFound:
            titles = [w.title for w in sh.worksheets()]
            match = [t for t in titles if title.lower() in t.lower()]
            if not match:
                raise ValueError(f"Лист «{title}» не найден. Есть листы: {', '.join(titles)}") from None
            title = match[0]
            ws = sh.worksheet(title)
            key = (source.url, title)
        values = ws.get_all_values()
        self._cache[key] = (time.time(), values)
        return title, values

    # ------------------------------------------------------------------ асинхронные обёртки
    async def aread(self, source: SheetSource, worksheet: str | None, fresh: bool = False):
        return await asyncio.to_thread(self.read, source, worksheet, fresh)

    async def aworksheet_titles(self, source: SheetSource) -> list[str]:
        return await asyncio.to_thread(self.worksheet_titles, source)

    # ------------------------------------------------------------------ снимки
    async def snapshot_all(self, db: DB) -> list[str]:
        """Снимает все настроенные листы и возвращает список изменений с прошлого снимка."""
        changes: list[str] = []
        for source in self.cfg.sheets:
            try:
                titles = await self.aworksheet_titles(source)
            except Exception as e:  # noqa: BLE001
                log.warning("Таблица «%s» недоступна: %s", source.name, e)
                continue
            for title in titles:
                try:
                    _, values = await self.aread(source, title, fresh=True)
                except Exception as e:  # noqa: BLE001
                    log.warning("Лист «%s/%s» не прочитан: %s", source.name, title, e)
                    continue
                prev = db.last_snapshot(source.url, title)
                if db.save_snapshot(source.url, title, normalize(values)) and prev is not None:
                    diff = diff_tables(json.loads(prev["data"]), values, max_items=30)
                    if diff:
                        changes.append(f"Таблица «{source.name}», лист «{title}»:\n" + "\n".join(diff))
        db.prune_snapshots()
        return changes
