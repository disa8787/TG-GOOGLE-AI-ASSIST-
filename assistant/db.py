"""SQLite-хранилище: архив сообщений (с полнотекстовым поиском), память, напоминания, отчёты."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path

from .util import to_db, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS chats(
    chat_id   INTEGER PRIMARY KEY,
    title     TEXT,
    username  TEXT,
    kind      TEXT,
    monitored INTEGER NOT NULL DEFAULT 1,
    last_sync TEXT
);

CREATE TABLE IF NOT EXISTS messages(
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    msg_id      INTEGER NOT NULL,
    date        TEXT NOT NULL,
    sender_id   INTEGER,
    sender_name TEXT,
    text        TEXT NOT NULL DEFAULT '',
    reply_to    INTEGER,
    fwd_from    TEXT,
    media       TEXT,
    media_path  TEXT,
    edited_at   TEXT,
    UNIQUE(chat_id, msg_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_chat_date ON messages(chat_id, date);
CREATE INDEX IF NOT EXISTS idx_messages_date ON messages(date);

CREATE TABLE IF NOT EXISTS memory(
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    skey       TEXT NOT NULL UNIQUE,
    category   TEXT NOT NULL,
    subject    TEXT NOT NULL,
    content    TEXT NOT NULL,
    source     TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reminders(
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    text          TEXT NOT NULL,
    due_at        TEXT NOT NULL,
    cron          TEXT,
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    last_fired_at TEXT,
    source        TEXT
);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(active, due_at);

CREATE TABLE IF NOT EXISTS reports(
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    content    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dialog(
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sheet_snapshots(
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    spreadsheet TEXT NOT NULL,
    worksheet   TEXT NOT NULL,
    taken_at    TEXT NOT NULL,
    hash        TEXT NOT NULL,
    data        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots ON sheet_snapshots(spreadsheet, worksheet, taken_at);

CREATE TABLE IF NOT EXISTS learn_notes(
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER,
    period     TEXT,
    summary    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage(
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    at            TEXT NOT NULL,
    model         TEXT,
    purpose       TEXT,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    cache_read    INTEGER,
    cache_write   INTEGER,
    cost          REAL
);
"""

FTS_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text, sender_name)
    VALUES (new.id, new.text, coalesce(new.sender_name, ''));
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text, sender_name)
    VALUES ('delete', old.id, old.text, coalesce(old.sender_name, ''));
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE OF text, sender_name ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text, sender_name)
    VALUES ('delete', old.id, old.text, coalesce(old.sender_name, ''));
    INSERT INTO messages_fts(rowid, text, sender_name)
    VALUES (new.id, new.text, coalesce(new.sender_name, ''));
END;
"""

MESSAGE_COLUMNS = (
    "chat_id", "msg_id", "date", "sender_id", "sender_name", "text",
    "reply_to", "fwd_from", "media", "media_path", "edited_at",
)


def subject_key(subject: str) -> str:
    return re.sub(r"\s+", " ", subject.strip().lower())


class DB:
    def __init__(self, path: Path):
        self.path = path
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self._init_fts()

    # ------------------------------------------------------------------ служебное
    def _init_fts(self) -> None:
        exists = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='messages_fts'"
        ).fetchone()
        if not exists:
            # trigram ищет по подстроке — это хорошо работает с русскими окончаниями
            try:
                self.conn.execute(
                    "CREATE VIRTUAL TABLE messages_fts USING fts5("
                    "text, sender_name, content='messages', content_rowid='id', tokenize='trigram')"
                )
                tokenizer = "trigram"
            except sqlite3.OperationalError:
                self.conn.execute(
                    "CREATE VIRTUAL TABLE messages_fts USING fts5("
                    "text, sender_name, content='messages', content_rowid='id', "
                    "tokenize='unicode61 remove_diacritics 2')"
                )
                tokenizer = "unicode61"
            self.conn.executescript(FTS_TRIGGERS)
            self.conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
            self.set_kv("fts_tokenizer", tokenizer)
            self.conn.commit()
        self.fts_tokenizer = self.get_kv("fts_tokenizer") or "unicode61"

    def get_kv(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_kv(self, key: str, value: str | None) -> None:
        with self.conn:
            if value is None:
                self.conn.execute("DELETE FROM kv WHERE key=?", (key,))
            else:
                self.conn.execute(
                    "INSERT INTO kv(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )

    # ------------------------------------------------------------------ чаты
    def upsert_chat(self, chat_id: int, title: str, username: str | None, kind: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO chats(chat_id, title, username, kind) VALUES(?,?,?,?) "
                "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, "
                "username=excluded.username, kind=excluded.kind",
                (chat_id, title, username, kind),
            )

    def mark_synced(self, chat_id: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE chats SET last_sync=? WHERE chat_id=?", (to_db(utcnow()), chat_id))

    def set_monitored(self, chat_id: int, monitored: bool) -> None:
        with self.conn:
            self.conn.execute("UPDATE chats SET monitored=? WHERE chat_id=?", (int(monitored), chat_id))

    def chats(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT c.*, (SELECT count(*) FROM messages m WHERE m.chat_id=c.chat_id) AS n, "
            "(SELECT max(date) FROM messages m WHERE m.chat_id=c.chat_id) AS last_date, "
            "(SELECT min(date) FROM messages m WHERE m.chat_id=c.chat_id) AS first_date "
            "FROM chats c ORDER BY last_date DESC"
        ).fetchall()

    def monitored_chat_ids(self) -> set[int]:
        rows = self.conn.execute("SELECT chat_id FROM chats WHERE monitored=1").fetchall()
        return {r["chat_id"] for r in rows}

    def chat_titles(self) -> dict[int, str]:
        return {r["chat_id"]: r["title"] for r in self.conn.execute("SELECT chat_id, title FROM chats")}

    def find_chat_ids(self, spec: str | int | None) -> list[int] | None:
        """Чат по id или части названия/username. None — без фильтра."""
        if spec is None or str(spec).strip() == "":
            return None
        text = str(spec).strip()
        if text.lstrip("-").isdigit():
            num = int(text)
            rows = self.conn.execute(
                "SELECT chat_id FROM chats WHERE chat_id=? OR chat_id=? OR chat_id=?",
                (num, -num, int(f"-100{abs(num)}")),
            ).fetchall()
            if rows:
                return [r["chat_id"] for r in rows]
        needle = text.lstrip("@").lower()
        ids = [
            r["chat_id"] for r in self.conn.execute("SELECT chat_id, title, username FROM chats")
            if needle in (r["title"] or "").lower() or needle == (r["username"] or "").lower()
        ]
        return ids

    # ------------------------------------------------------------------ сообщения
    def last_msg_id(self, chat_id: int) -> int:
        row = self.conn.execute("SELECT max(msg_id) AS m FROM messages WHERE chat_id=?", (chat_id,)).fetchone()
        return int(row["m"] or 0)

    def upsert_messages(self, rows: Iterable[dict]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        cols = ", ".join(MESSAGE_COLUMNS)
        marks = ", ".join("?" for _ in MESSAGE_COLUMNS)
        updates = ", ".join(f"{c}=excluded.{c}" for c in MESSAGE_COLUMNS if c not in ("chat_id", "msg_id"))
        sql = (
            f"INSERT INTO messages({cols}) VALUES({marks}) "
            f"ON CONFLICT(chat_id, msg_id) DO UPDATE SET {updates}"
        )
        values = [
            tuple((r.get(c) or "") if c == "text" else r.get(c) for c in MESSAGE_COLUMNS) for r in rows
        ]
        with self.conn:
            self.conn.executemany(sql, values)
        return len(rows)

    def delete_messages(self, chat_id: int | None, msg_ids: list[int]) -> None:
        with self.conn:
            for mid in msg_ids:
                if chat_id is None:
                    continue
                self.conn.execute("DELETE FROM messages WHERE chat_id=? AND msg_id=?", (chat_id, mid))

    def _fts_query(self, query: str, mode: str) -> str | None:
        words = [w for w in re.findall(r"\w+", query.lower()) if len(w) >= 3]
        if not words:
            return None
        joiner = " OR " if mode == "any" else " AND "
        if self.fts_tokenizer == "trigram":
            return joiner.join(f'"{w}"' for w in words)
        return joiner.join(f'"{w}"*' for w in words)

    def search_messages(
        self,
        query: str,
        mode: str = "all",
        chat_ids: list[int] | None = None,
        sender: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 40,
    ) -> list[sqlite3.Row]:
        where, params = [], []
        match = self._fts_query(query, mode)
        if match:
            base = "SELECT m.* FROM messages_fts f JOIN messages m ON m.id=f.rowid"
            where.append("messages_fts MATCH ?")
            params.append(match)
        else:
            base = "SELECT m.* FROM messages m"
            if query.strip():
                where.append("lower(m.text) LIKE ?")
                params.append(f"%{query.strip().lower()}%")
        if chat_ids is not None:
            where.append(f"m.chat_id IN ({','.join('?' * len(chat_ids)) or 'NULL'})")
            params.extend(chat_ids)
        if sender:
            where.append("lower(coalesce(m.sender_name,'')) LIKE ?")
            params.append(f"%{sender.lower()}%")
        if date_from:
            where.append("m.date >= ?")
            params.append(date_from)
        if date_to:
            where.append("m.date <= ?")
            params.append(date_to)
        sql = base + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY m.date DESC LIMIT ?"
        params.append(limit)
        return self.conn.execute(sql, params).fetchall()

    def read_messages(
        self,
        chat_ids: list[int] | None,
        date_from: str | None = None,
        date_to: str | None = None,
        around: int | None = None,
        limit: int = 100,
    ) -> list[sqlite3.Row]:
        where, params = [], []
        if chat_ids is not None:
            where.append(f"chat_id IN ({','.join('?' * len(chat_ids)) or 'NULL'})")
            params.extend(chat_ids)
        if around is not None:
            row = self.conn.execute(
                "SELECT date FROM messages WHERE msg_id=?"
                + (f" AND chat_id IN ({','.join('?' * len(chat_ids))})" if chat_ids else ""),
                [around, *(chat_ids or [])],
            ).fetchone()
            if row:
                half = max(1, limit // 2)
                before = self.conn.execute(
                    "SELECT * FROM messages WHERE " + " AND ".join(where + ["date <= ?"])
                    + " ORDER BY date DESC, msg_id DESC LIMIT ?",
                    [*params, row["date"], half + 1],
                ).fetchall()
                after = self.conn.execute(
                    "SELECT * FROM messages WHERE " + " AND ".join(where + ["date > ?"])
                    + " ORDER BY date ASC, msg_id ASC LIMIT ?",
                    [*params, row["date"], half],
                ).fetchall()
                return list(reversed(before)) + list(after)
        if date_from:
            where.append("date >= ?")
            params.append(date_from)
        if date_to:
            where.append("date <= ?")
            params.append(date_to)
        clause = " WHERE " + " AND ".join(where) if where else ""
        if date_from:
            sql = f"SELECT * FROM messages{clause} ORDER BY date ASC, msg_id ASC LIMIT ?"
            return self.conn.execute(sql, [*params, limit]).fetchall()
        # без начальной даты — последние N сообщений периода
        sql = f"SELECT * FROM messages{clause} ORDER BY date DESC, msg_id DESC LIMIT ?"
        return list(reversed(self.conn.execute(sql, [*params, limit]).fetchall()))

    def messages_after_rowid(self, rowid: int, chat_ids: set[int] | None = None, limit: int = 100000):
        sql = "SELECT * FROM messages WHERE id > ?"
        params: list = [rowid]
        if chat_ids is not None:
            sql += f" AND chat_id IN ({','.join('?' * len(chat_ids)) or 'NULL'})"
            params.extend(chat_ids)
        sql += " ORDER BY id ASC LIMIT ?"
        params.append(limit)
        return self.conn.execute(sql, params).fetchall()

    def chat_messages_for_learning(self, chat_id: int, after_rowid: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM messages WHERE chat_id=? AND id > ? ORDER BY date ASC, msg_id ASC",
            (chat_id, after_rowid),
        ).fetchall()

    def max_message_rowid(self) -> int:
        row = self.conn.execute("SELECT max(id) AS m FROM messages").fetchone()
        return int(row["m"] or 0)

    def message_stats(self) -> dict:
        row = self.conn.execute(
            "SELECT count(*) AS n, min(date) AS first, max(date) AS last FROM messages"
        ).fetchone()
        return dict(row)

    # ------------------------------------------------------------------ память
    def upsert_memory(self, category: str, subject: str, content: str, source: str = "assistant") -> int:
        now = to_db(utcnow())
        key = subject_key(subject)
        with self.conn:
            self.conn.execute(
                "INSERT INTO memory(skey, category, subject, content, source, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(skey) DO UPDATE SET category=excluded.category, "
                "subject=excluded.subject, content=excluded.content, source=excluded.source, "
                "updated_at=excluded.updated_at",
                (key, category.strip() or "прочее", subject.strip(), content.strip(), source, now, now),
            )
        row = self.conn.execute("SELECT id FROM memory WHERE skey=?", (key,)).fetchone()
        return int(row["id"])

    def delete_memory(self, note_id: int) -> bool:
        with self.conn:
            cur = self.conn.execute("DELETE FROM memory WHERE id=?", (note_id,))
        return cur.rowcount > 0

    def all_memory(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM memory ORDER BY category, subject").fetchall()

    def search_memory(self, query: str, limit: int = 30) -> list[sqlite3.Row]:
        words = [w for w in re.findall(r"\w+", query.lower()) if len(w) >= 2]
        rows = self.all_memory()
        scored = []
        for r in rows:
            hay = f"{r['category']} {r['subject']} {r['content']}".lower()
            score = sum(1 for w in words if w in hay)
            if score:
                scored.append((score, r["updated_at"], r))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return [r for _, _, r in scored[:limit]]

    def memory_count(self) -> int:
        return int(self.conn.execute("SELECT count(*) AS n FROM memory").fetchone()["n"])

    # ------------------------------------------------------------------ напоминания
    def add_reminder(self, text: str, due_at: datetime, cron: str | None, source: str) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO reminders(text, due_at, cron, created_at, source) VALUES(?,?,?,?,?)",
                (text, to_db(due_at), cron, to_db(utcnow()), source),
            )
        return int(cur.lastrowid)

    def active_reminders(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM reminders WHERE active=1 ORDER BY due_at").fetchall()

    def due_reminders(self, now: datetime) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM reminders WHERE active=1 AND due_at <= ? ORDER BY due_at", (to_db(now),)
        ).fetchall()

    def reminder_fired(self, rid: int, next_due: datetime | None) -> None:
        now = to_db(utcnow())
        with self.conn:
            if next_due is None:
                self.conn.execute("UPDATE reminders SET active=0, last_fired_at=? WHERE id=?", (now, rid))
            else:
                self.conn.execute(
                    "UPDATE reminders SET due_at=?, last_fired_at=? WHERE id=?", (to_db(next_due), now, rid)
                )

    def cancel_reminder(self, rid: int) -> bool:
        with self.conn:
            cur = self.conn.execute("UPDATE reminders SET active=0 WHERE id=? AND active=1", (rid,))
        return cur.rowcount > 0

    # ------------------------------------------------------------------ отчёты и диалог
    def add_report(self, kind: str, content: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO reports(kind, created_at, content) VALUES(?,?,?)", (kind, to_db(utcnow()), content)
            )

    def reports_since(self, since: datetime, kind: str | None = None) -> list[sqlite3.Row]:
        sql, params = "SELECT * FROM reports WHERE created_at >= ?", [to_db(since)]
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        return self.conn.execute(sql + " ORDER BY created_at", params).fetchall()

    def add_dialog(self, role: str, content: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO dialog(role, content, created_at) VALUES(?,?,?)", (role, content, to_db(utcnow()))
            )

    def recent_dialog(self, limit: int, after_id: int = 0) -> list[sqlite3.Row]:
        rows = self.conn.execute(
            "SELECT * FROM dialog WHERE id > ? ORDER BY id DESC LIMIT ?", (after_id, limit)
        ).fetchall()
        return list(reversed(rows))

    def last_dialog_id(self) -> int:
        row = self.conn.execute("SELECT max(id) AS m FROM dialog").fetchone()
        return int(row["m"] or 0)

    # ------------------------------------------------------------------ снимки таблиц
    def last_snapshot(self, spreadsheet: str, worksheet: str, before: datetime | None = None):
        sql = "SELECT * FROM sheet_snapshots WHERE spreadsheet=? AND worksheet=?"
        params: list = [spreadsheet, worksheet]
        if before is not None:
            sql += " AND taken_at <= ?"
            params.append(to_db(before))
        row = self.conn.execute(sql + " ORDER BY taken_at DESC, id DESC LIMIT 1", params).fetchone()
        if row is None and before is not None:
            # нет снимка старше указанного момента — берём самый ранний
            row = self.conn.execute(
                "SELECT * FROM sheet_snapshots WHERE spreadsheet=? AND worksheet=? "
                "ORDER BY taken_at ASC, id ASC LIMIT 1",
                (spreadsheet, worksheet),
            ).fetchone()
        return row

    def save_snapshot(self, spreadsheet: str, worksheet: str, values: list[list[str]]) -> bool:
        """Сохраняет снимок, если он отличается от последнего. True — если были изменения."""
        data = json.dumps(values, ensure_ascii=False)
        digest = hashlib.sha256(data.encode("utf-8")).hexdigest()
        last = self.last_snapshot(spreadsheet, worksheet)
        if last is not None and last["hash"] == digest:
            return False
        with self.conn:
            self.conn.execute(
                "INSERT INTO sheet_snapshots(spreadsheet, worksheet, taken_at, hash, data) VALUES(?,?,?,?,?)",
                (spreadsheet, worksheet, to_db(utcnow()), digest, data),
            )
        return last is not None

    def prune_snapshots(self, keep_days: int = 21) -> None:
        cutoff = to_db(utcnow() - timedelta(days=keep_days))
        with self.conn:
            # удаляем старые, но не последний снимок каждого листа
            self.conn.execute(
                "DELETE FROM sheet_snapshots WHERE taken_at < ? AND id NOT IN ("
                "SELECT max(id) FROM sheet_snapshots GROUP BY spreadsheet, worksheet)",
                (cutoff,),
            )

    # ------------------------------------------------------------------ обучение
    def add_learn_note(self, chat_id: int | None, period: str, summary: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO learn_notes(chat_id, period, summary, created_at) VALUES(?,?,?,?)",
                (chat_id, period, summary, to_db(utcnow())),
            )

    def learn_notes(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM learn_notes ORDER BY chat_id, period, id").fetchall()

    def reset_learning(self) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM learn_notes")
            self.conn.execute("DELETE FROM kv WHERE key LIKE 'learn_cursor:%'")

    # ------------------------------------------------------------------ расходы
    def add_usage(self, model: str, purpose: str, usage, cost: float) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO usage(at, model, purpose, input_tokens, output_tokens, cache_read, cache_write, cost) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    to_db(utcnow()), model, purpose,
                    getattr(usage, "input_tokens", 0) or 0,
                    getattr(usage, "output_tokens", 0) or 0,
                    getattr(usage, "cache_read_input_tokens", 0) or 0,
                    getattr(usage, "cache_creation_input_tokens", 0) or 0,
                    cost,
                ),
            )

    def usage_since(self, since: datetime) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT purpose, count(*) AS calls, sum(input_tokens) AS inp, sum(output_tokens) AS outp, "
            "sum(cache_read) AS cr, sum(cache_write) AS cw, sum(cost) AS cost "
            "FROM usage WHERE at >= ? GROUP BY purpose ORDER BY cost DESC",
            (to_db(since),),
        ).fetchall()
