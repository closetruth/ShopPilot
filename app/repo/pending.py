"""待确认动作：模型提议、顾客点击后才真正执行的那一步。

存的是「将要执行什么」（工具名 + 已校验的入参）与「给顾客看什么」
（summary），执行结果在确认后回填。默认 30 分钟过期。
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any

from app.core.db import connect, now

ACTION_TTL_MINUTES = 30

STATUS_PENDING = "pending"
STATUS_CONFIRMED = "confirmed"
STATUS_CANCELLED = "cancelled"
STATUS_EXPIRED = "expired"


def create(
    visitor_id: str,
    tool_name: str,
    args: dict[str, Any],
    summary: dict[str, Any],
    conversation_id: str = "",
) -> dict:
    """建一条待确认动作，返回含 id 与过期时间的记录。"""
    action_id = "pa_" + uuid.uuid4().hex[:12]
    created = now()
    expires = created + timedelta(minutes=ACTION_TTL_MINUTES)

    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pending_actions
                (id, visitor_id, conversation_id, tool_name, args_json,
                 summary_json, status, created_at, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                action_id,
                visitor_id,
                conversation_id or "",
                tool_name,
                json.dumps(args, ensure_ascii=False),
                json.dumps(summary, ensure_ascii=False),
                STATUS_PENDING,
                created,
                expires,
            ),
        )
    return {
        "id": action_id,
        "visitor_id": visitor_id,
        "tool_name": tool_name,
        "args": args,
        "summary": summary,
        "status": STATUS_PENDING,
        "created_at": created.isoformat(),
        "expires_at": expires.isoformat(),
    }


def _row_to_dict(row: dict) -> dict:
    for key in ("args_json", "summary_json", "result_json"):
        if isinstance(row.get(key), str):
            row[key] = json.loads(row[key])
    if row.get("created_at") is not None:
        row["created_at"] = row["created_at"].isoformat()
    if row.get("expires_at") is not None:
        row["expires_at"] = row["expires_at"].isoformat()
    if row.get("resolved_at") is not None:
        row["resolved_at"] = row["resolved_at"].isoformat()
    return row


def get(action_id: str) -> dict | None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, visitor_id, conversation_id, tool_name, args_json,
                   summary_json, status, created_at, expires_at, resolved_at,
                   result_json
              FROM pending_actions
             WHERE id = %s
            """,
            (action_id,),
        )
        row = cur.fetchone()
    return _row_to_dict(row) if row else None


def resolve(
    action_id: str,
    status: str,
    *,
    result: dict[str, Any] | None = None,
) -> None:
    """把动作置为终态。只更新仍在 pending 的行，天然防并发重复确认。"""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE pending_actions
               SET status = %s, resolved_at = %s, result_json = %s
             WHERE id = %s AND status = %s
            """,
            (
                status,
                now(),
                json.dumps(result, ensure_ascii=False) if result is not None else None,
                action_id,
                STATUS_PENDING,
            ),
        )


def list_by_visitor(visitor_id: str, limit: int = 20) -> list[dict]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, visitor_id, conversation_id, tool_name, args_json,
                   summary_json, status, created_at, expires_at, resolved_at,
                   result_json
              FROM pending_actions
             WHERE visitor_id = %s
             ORDER BY created_at DESC
             LIMIT %s
            """,
            (visitor_id, max(1, min(limit, 100))),
        )
        rows = cur.fetchall()
    return [_row_to_dict(r) for r in rows]