"""用户反馈读写。

一条回答最多一行反馈：重复提交覆盖，撤销才删。标签与文字都可选，
所以 rating 是唯一的必填字段。
"""

from __future__ import annotations

from datetime import timedelta

from app.core.db import connect, now

REASONS = ("irrelevant", "inaccurate", "unsolved", "verbose", "other")


def message_of(message_id: int) -> dict | None:
    """带出消息所属会话与访客，用于校验反馈的归属。"""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT m.id, m.conversation_id, m.role, c.visitor_id
              FROM messages m JOIN conversations c ON c.id = m.conversation_id
             WHERE m.id = %s
            """,
            (message_id,),
        )
        return cur.fetchone()


def upsert(
    message_id: int,
    conversation_id: str,
    visitor_id: str,
    rating: int,
    reason: str | None = None,
    comment: str | None = None,
) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO message_feedback
                (message_id, conversation_id, visitor_id, rating, reason, comment,
                 created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                rating = VALUES(rating), reason = VALUES(reason),
                comment = VALUES(comment), updated_at = VALUES(updated_at)
            """,
            (
                message_id,
                conversation_id,
                visitor_id,
                rating,
                reason,
                comment,
                now(),
                now(),
            ),
        )


def remove(message_id: int) -> bool:
    """撤销反馈：返回是否真的删掉了一行。"""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM message_feedback WHERE message_id = %s", (message_id,))
        return cur.rowcount > 0


def by_conversation(conversation_id: str) -> list[dict]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, message_id, conversation_id, visitor_id, rating, reason,
                   comment, created_at, updated_at
              FROM message_feedback
             WHERE conversation_id = %s
             ORDER BY message_id ASC
            """,
            (conversation_id,),
        )
        rows = cur.fetchall()
    return [_serialize(r) for r in rows]


def by_id(feedback_id: int) -> dict | None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, message_id, conversation_id, visitor_id, rating, reason,
                   comment, created_at, updated_at
              FROM message_feedback
             WHERE id = %s
            """,
            (feedback_id,),
        )
        row = cur.fetchone()
    return _serialize(row) if row else None


def stats(days: int = 7) -> dict:
    """飞轮看板顶部的三个数：赞、踩、踩占比。"""
    since = now() - timedelta(days=days)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(SUM(rating = 1), 0)  AS positive,
                   COALESCE(SUM(rating = -1), 0) AS negative
              FROM message_feedback
             WHERE created_at >= %s
            """,
            (since,),
        )
        row = cur.fetchone()
    positive = int(row["positive"])
    negative = int(row["negative"])
    total = positive + negative
    return {
        "positive": positive,
        "negative": negative,
        "negative_rate": (negative / total) if total else 0.0,
    }


def list_negative(days: int = 7, limit: int = 50) -> list[dict]:
    """负反馈清单，附带该条回答之前最近一条顾客提问。"""
    since = now() - timedelta(days=days)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT f.id, f.message_id, f.conversation_id, f.visitor_id, f.rating,
                   f.reason, f.comment, f.created_at,
                   (SELECT m.content FROM messages m
                     WHERE m.conversation_id = f.conversation_id
                       AND m.id < f.message_id AND m.role = 'user'
                     ORDER BY m.id DESC LIMIT 1) AS query
              FROM message_feedback f
             WHERE f.rating = -1 AND f.created_at >= %s
             ORDER BY f.id DESC
             LIMIT %s
            """,
            (since, limit),
        )
        rows = cur.fetchall()
    return [_serialize(r) for r in rows]


def query_for(message_id: int) -> str | None:
    """该条回答之前最近一条顾客提问，转评估样本时用。"""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT m.content
              FROM messages m JOIN message_feedback f
                ON f.conversation_id = m.conversation_id
             WHERE f.message_id = %s AND m.id < %s AND m.role = 'user'
             ORDER BY m.id DESC
             LIMIT 1
            """,
            (message_id, message_id),
        )
        row = cur.fetchone()
    return row["content"] if row else None


def _serialize(row: dict) -> dict:
    data = dict(row)
    for key in ("created_at", "updated_at"):
        if data.get(key) is not None:
            data[key] = data[key].isoformat()
    return data