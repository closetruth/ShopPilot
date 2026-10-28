"""会话与消息读写。"""

from __future__ import annotations

import uuid

from app.db import connect, now


def create_conversation(visitor_id: str) -> str:
    conv_id = uuid.uuid4().hex
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO conversations (id, visitor_id, created_at) VALUES (%s, %s, %s)",
            (conv_id, visitor_id, now()),
        )
    return conv_id


def conversation_owned_by(conversation_id: str, visitor_id: str) -> bool:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM conversations WHERE id = %s AND visitor_id = %s",
            (conversation_id, visitor_id),
        )
        return cur.fetchone() is not None


def load_history(conversation_id: str, limit: int = 20) -> list[dict[str, str]]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT role, content FROM (
                SELECT role, content, id FROM messages
                 WHERE conversation_id = %s
                 ORDER BY id DESC
                 LIMIT %s
            ) AS recent ORDER BY id ASC
            """,
            (conversation_id, limit),
        )
        rows = cur.fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in rows]


def list_conversations(visitor_id: str) -> list[dict]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.id,
                   c.created_at,
                   (SELECT m.content FROM messages m
                     WHERE m.conversation_id = c.id AND m.role = 'user'
                     ORDER BY m.id ASC LIMIT 1) AS first_user,
                   (SELECT MAX(m.id) FROM messages m
                     WHERE m.conversation_id = c.id) AS last_msg_id
              FROM conversations c
             WHERE c.visitor_id = %s
             ORDER BY COALESCE(
                 (SELECT MAX(m.id) FROM messages m WHERE m.conversation_id = c.id), 0
             ) DESC
            """,
            (visitor_id,),
        )
        rows = cur.fetchall()
    return [
        {
            "id": r["id"],
            "title": _title_of(r["first_user"]),
            "created_at": r["created_at"].isoformat(),
            "has_messages": r["last_msg_id"] is not None,
        }
        for r in rows
    ]


def append_message(conversation_id: str, role: str, content: str) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO messages (conversation_id, role, content, created_at)"
            " VALUES (%s, %s, %s, %s)",
            (conversation_id, role, content, now()),
        )


def _title_of(text: str | None) -> str:
    if not text:
        return "新对话"
    flat = " ".join(text.split())
    return flat[:20] + ("…" if len(flat) > 20 else "")
