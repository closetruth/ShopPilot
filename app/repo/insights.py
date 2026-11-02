"""低置信度问题记录。

检索不到或分数过低时落库，形成「知识库缺什么」的可分析清单。
记录失败不能影响对话——所有调用方都用 try 包住。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.core.db import connect, now

logger = logging.getLogger(__name__)


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


# ---------- agent 轨迹（r15） ----------

def record_trace(
    conversation_id: str,
    visitor_id: str,
    message: str,
    intent: str,
    intent_method: str,
    node_path: str,
    latency_ms: int,
    *,
    resolved_query: str | None = None,
    confidence: float | None = None,
    top_score: float | None = None,
    tool_calls: list[str] | None = None,
    steps: int | None = None,
    total_tokens: int | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """写入一轮对话的结构化轨迹。供回放分析，不参与业务流程。

    轨迹是观测数据，写入失败只记日志，不向上抛——不能拖垮对话主链路。
    """
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO agent_traces
                    (conversation_id, visitor_id, message, resolved_query, intent,
                     intent_method, confidence, node_path, top_score, tool_calls,
                     steps, total_tokens, latency_ms, detail, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    conversation_id,
                    visitor_id,
                    message[:2000],
                    resolved_query,
                    intent,
                    intent_method,
                    confidence,
                    node_path,
                    top_score,
                    ",".join(tool_calls or []) or None,
                    steps,
                    total_tokens,
                    latency_ms,
                    json.dumps(detail, ensure_ascii=False) if detail is not None else None,
                    now(),
                ),
            )
    except Exception:
        logger.warning("轨迹写入失败，不影响对话", exc_info=True)


def list_traces(limit: int = 50) -> list[dict]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, conversation_id, visitor_id, message, resolved_query,
                   intent, intent_method, confidence, node_path, top_score,
                   tool_calls, steps, total_tokens, latency_ms, detail,
                   created_at
              FROM agent_traces
             ORDER BY id DESC
             LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
    traces = []
    for r in rows:
        # detail 是 JSON 列，驱动端按字符串返回，这里还原成 dict
        if isinstance(r["detail"], str):
            r["detail"] = json.loads(r["detail"])
        traces.append({**r, "created_at": r["created_at"].isoformat()})
    return traces


def trace_stats() -> dict:
    """按意图分组统计，看每轮对话分流到了哪条路径。"""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT intent, COUNT(*) AS n FROM agent_traces GROUP BY intent"
        )
        by_intent = {r["intent"]: r["n"] for r in cur.fetchall()}
        cur.execute("SELECT COUNT(*) AS n FROM agent_traces")
        total = cur.fetchone()["n"]
    return {"total": total, "by_intent": by_intent}