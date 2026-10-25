"""Durable inbox, reply outbox and conversation mappings (one service process)."""

import json
import sqlite3
import time
import uuid
from pathlib import Path

from .messages import Message, is_all_message_command

CONTROL_COMMANDS = frozenset({"/status", "/result", "/help", "/session", "/all_message"})


def is_control_command(text: str) -> bool:
    return text in CONTROL_COMMANDS or is_all_message_command(text)


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, chat_key TEXT NOT NULL,
                thread_id TEXT, created REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chats (
                chat_key TEXT PRIMARY KEY, session_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS group_preferences (
                chat_key TEXT PRIMARY KEY,
                all_message INTEGER NOT NULL CHECK(all_message IN (0, 1))
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dedup_key TEXT UNIQUE NOT NULL, chat_key TEXT NOT NULL,
                payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                reply TEXT, session_id TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0, created REAL NOT NULL,
                error TEXT
            );
            CREATE INDEX IF NOT EXISTS jobs_pending ON jobs(status, id);
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(jobs)")}
        if "is_control" not in columns:
            with self.db:
                self.db.execute("ALTER TABLE jobs ADD COLUMN is_control INTEGER NOT NULL DEFAULT 0")
                # Migrate pending commands in databases created by earlier versions.
                for row in self.db.execute("SELECT id,payload FROM jobs").fetchall():
                    if is_control_command(json.loads(row["payload"])["text"]):
                        self.db.execute("UPDATE jobs SET is_control=1 WHERE id=?", (row["id"],))
        with self.db:
            if "delivery_plan" not in columns:
                self.db.execute("ALTER TABLE jobs ADD COLUMN delivery_plan TEXT")
            if "delivered" not in columns:
                self.db.execute("ALTER TABLE jobs ADD COLUMN delivered INTEGER NOT NULL DEFAULT 0")
            for row in self.db.execute("""
                SELECT id,payload FROM jobs WHERE is_control=0 AND status IN ('pending','ready')
            """).fetchall():
                if is_all_message_command(json.loads(row["payload"])["text"]):
                    self.db.execute("UPDATE jobs SET is_control=1 WHERE id=?", (row["id"],))

    def close(self):
        self.db.close()

    def recover(self):
        # Never replay a potentially completed Codex action after a process crash.
        with self.db:
            self.db.execute("""
                UPDATE jobs SET status='ready', reply=COALESCE(reply, ?)
                WHERE status='running'
            """, ("上次处理被服务重启中断，请重新发送消息；会话记录已保留。",))

    def enqueue(self, message: Message) -> bool:
        with self.db:
            cursor = self.db.execute("""
                INSERT OR IGNORE INTO jobs(dedup_key,chat_key,payload,created,is_control)
                VALUES(?,?,?,?,?)
            """, (message.dedup_key, message.chat_key,
                  json.dumps(message.as_dict(), ensure_ascii=False), time.time(),
                  int(is_control_command(message.text))))
        return cursor.rowcount == 1

    def all_message_enabled(self, chat_key: str) -> bool:
        row = self.db.execute(
            "SELECT all_message FROM group_preferences WHERE chat_key=?", (chat_key,)
        ).fetchone()
        # Preserve existing full-message behavior until a group explicitly switches it off.
        return row is None or bool(row["all_message"])

    def set_all_message(self, chat_key: str, enabled: bool):
        with self.db:
            self.db.execute("""
                INSERT INTO group_preferences(chat_key,all_message) VALUES(?,?)
                ON CONFLICT(chat_key) DO UPDATE SET all_message=excluded.all_message
            """, (chat_key, int(enabled)))

    def claim(self, *, control_only: bool = False):
        # All access occurs on the asyncio loop; no await between SELECT and UPDATE.
        with self.db:
            row = self.db.execute("""
                SELECT * FROM jobs AS j
                WHERE j.status IN ('pending','ready') AND j.next_attempt <= ?
                AND j.is_control = ?
                AND NOT EXISTS (
                    SELECT 1 FROM jobs AS earlier
                    WHERE earlier.chat_key=j.chat_key AND earlier.id<j.id
                    AND earlier.is_control=j.is_control
                    AND earlier.status IN ('pending','running','ready')
                ) ORDER BY j.id LIMIT 1
            """, (time.time(), int(control_only))).fetchone()
            if row:
                self.db.execute("UPDATE jobs SET status='running' WHERE id=?", (row["id"],))
        return row

    def task_status(self, chat_key: str):
        return self.db.execute("""
            SELECT id,status,created FROM jobs WHERE chat_key=? AND is_control=0
            AND status IN ('pending','running','ready') ORDER BY id LIMIT 1
        """, (chat_key,)).fetchone()

    def latest_result(self, chat_key: str):
        return self.db.execute("""
            SELECT id,reply,delivery_plan FROM jobs WHERE chat_key=? AND is_control=0
            AND session_id IS NOT NULL AND reply IS NOT NULL ORDER BY id DESC LIMIT 1
        """, (chat_key,)).fetchone()

    def delivery(self, job_id: int):
        return self.db.execute("SELECT delivery_plan,delivered FROM jobs WHERE id=?", (job_id,)).fetchone()

    def save_delivery(self, job_id: int, plan: list[dict], delivered: int):
        with self.db:
            self.db.execute("UPDATE jobs SET delivery_plan=?,delivered=? WHERE id=?",
                            (json.dumps(plan, ensure_ascii=False), delivered, job_id))

    def current_session(self, chat_key: str):
        return self.db.execute("""
            SELECT s.* FROM sessions s JOIN chats c ON c.session_id=s.id
            WHERE c.chat_key=?
        """, (chat_key,)).fetchone()

    def new_session(self, chat_key: str):
        session_id = str(uuid.uuid4())
        with self.db:
            self.db.execute("INSERT INTO sessions VALUES(?,?,NULL,?)",
                            (session_id, chat_key, time.time()))
            self.db.execute("""
                INSERT INTO chats VALUES(?,?)
                ON CONFLICT(chat_key) DO UPDATE SET session_id=excluded.session_id
            """, (chat_key, session_id))
        return self.current_session(chat_key)

    def reset(self, chat_key: str):
        with self.db:
            self.db.execute("DELETE FROM chats WHERE chat_key=?", (chat_key,))

    def bind_job(self, job_id: int, session_id: str):
        with self.db:
            self.db.execute("UPDATE jobs SET session_id=? WHERE id=?", (session_id, job_id))

    def save_thread(self, session_id: str, thread_id: str):
        with self.db:
            self.db.execute("UPDATE sessions SET thread_id=? WHERE id=?", (thread_id, session_id))

    def save_reply(self, job_id: int, text: str):
        # Remain 'running' until the worker finishes its delivery attempt.
        with self.db:
            self.db.execute("UPDATE jobs SET reply=? WHERE id=?", (text, job_id))

    def finish(self, job_id: int, error: str | None = None):
        with self.db:
            self.db.execute("UPDATE jobs SET status=?,error=? WHERE id=?",
                            ("failed" if error else "done", error, job_id))

    def retry_delivery(self, job_id: int, attempts: int):
        with self.db:
            self.db.execute("""
                UPDATE jobs SET status='ready',attempts=?,next_attempt=? WHERE id=?
            """, (attempts, time.time() + min(2 ** attempts, 15), job_id))
