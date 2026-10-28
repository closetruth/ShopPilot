"""SQLite 持久化：顾客、会话与消息。

visitor_id 标识同一浏览器上的顾客，conversation_id 标识其一具体会话。
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB_PATH = "data/shoppilot.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id          TEXT PRIMARY KEY,
    visitor_id  TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    role            TEXT NOT NULL,
    content         TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, id);
CREATE INDEX IF NOT EXISTS idx_conversations_visitor ON conversations(visitor_id);
"""


def db_path() -> Path:
    return Path(os.getenv("DB_PATH", DEFAULT_DB_PATH))


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(SCHEMA)


def create_conversation(visitor_id: str) -> str:
    conv_id = uuid.uuid4().hex
    with _connect() as conn:
        conn.execute(
            "INSERT INTO conversations (id, visitor_id, created_at) VALUES (?, ?, ?)",
            (conv_id, visitor_id, _now()),
        )
    return conv_id


def conversation_owned_by(conversation_id: str, visitor_id: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM conversations WHERE id = ? AND visitor_id = ?",
            (conversation_id, visitor_id),
        ).fetchone()
    return row is not None


def load_history(conversation_id: str, limit: int = 20) -> list[dict[str, str]]:
    """按时间正序返回最近 limit 条消息。"""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT role, content FROM (
                SELECT role, content, id FROM messages
                WHERE conversation_id = ?
                ORDER BY id DESC
                LIMIT ?
            ) ORDER BY id ASC
            """,
            (conversation_id, limit),
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in rows]


def list_conversations(visitor_id: str) -> list[dict]:
    """该顾客的会话，最近活跃在前，标题取顾客第一句话。"""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT c.id,
                   c.created_at,
                   (SELECT content FROM messages m
                     WHERE m.conversation_id = c.id AND m.role = 'user'
                     ORDER BY m.id ASC LIMIT 1) AS first_user,
                   (SELECT MAX(m.id) FROM messages m
                     WHERE m.conversation_id = c.id) AS last_msg_id
              FROM conversations c
             WHERE c.visitor_id = ?
             ORDER BY COALESCE(
                 (SELECT MAX(m.id) FROM messages m WHERE m.conversation_id = c.id), 0
             ) DESC
            """,
            (visitor_id,),
        ).fetchall()
    return [
        {
            "id": r["id"],
            "title": _title_of(r["first_user"]),
            "created_at": r["created_at"],
            "has_messages": r["last_msg_id"] is not None,
        }
        for r in rows
    ]


def append_message(conversation_id: str, role: str, content: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content, created_at)"
            " VALUES (?, ?, ?, ?)",
            (conversation_id, role, content, _now()),
        )


def _title_of(text: str | None) -> str:
    if not text:
        return "新对话"
    flat = " ".join(text.split())
    return flat[:20] + ("…" if len(flat) > 20 else "")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
