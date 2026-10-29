"""低置信度问题记录。

检索不到或分数过低时落库，形成「知识库缺什么」的可分析清单。
记录失败不能影响对话——所有调用方都用 try 包住。
"""

from __future__ import annotations

import json

from app.db import connect, now


def record(
    visitor_id: str,
    query: str,
    reason: str,
    max_score: float | None = None,
    retrieved: list[str] | None = None,
) -> None:
    """写入一条低置信度记录。

    reason: empty（检索为空）或 low_score（有结果但分数不足）
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO low_confidence_queries
                (visitor_id, query, max_score, result_count, reason,
                 retrieved, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                visitor_id,
                query[:1000],
                max_score,
                len(retrieved or []),
                reason,
                json.dumps(retrieved or [], ensure_ascii=False)[:512],
                now(),
            ),
        )


def list_recent(limit: int = 100) -> list[dict]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, visitor_id, query, max_score, result_count, reason,
                   retrieved, created_at
              FROM low_confidence_queries
             ORDER BY id DESC
             LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
    return [
        {**r, "created_at": r["created_at"].isoformat()} for r in rows
    ]


def stats() -> dict:
    """按原因分组统计，看缺口主要集中在哪类。"""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT reason, COUNT(*) AS n FROM low_confidence_queries"
            " GROUP BY reason"
        )
        by_reason = {r["reason"]: r["n"] for r in cur.fetchall()}
        cur.execute("SELECT COUNT(*) AS n FROM low_confidence_queries")
        total = cur.fetchone()["n"]
    return {"total": total, "by_reason": by_reason}
