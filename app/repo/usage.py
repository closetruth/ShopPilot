"""LLM 用量计量。

一行对应一次模型调用，是成本看板与三层成本闸的唯一数据源。
"""

from __future__ import annotations

from app.core.db import connect, now


def record(
    conversation_id: str | None,
    visitor_id: str | None,
    source: str,
    node: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_micros: int,
    latency_ms: int | None = None,
) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO llm_usage
                (conversation_id, visitor_id, source, node, model,
                 input_tokens, output_tokens, cost_micros, latency_ms, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                conversation_id,
                visitor_id,
                source,
                node,
                model,
                input_tokens,
                output_tokens,
                cost_micros,
                latency_ms,
                now(),
            ),
        )


def sum_tokens_since(
    since, conversation_id: str | None = None, source: str | None = None
) -> int:
    where, params = _where(since, conversation_id=conversation_id, source=source)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(SUM(input_tokens + output_tokens), 0) AS n"
            f"  FROM llm_usage{where}",
            params,
        )
        return int(cur.fetchone()["n"])


def sum_cost_since(since, source: str | None = None) -> int:
    where, params = _where(since, source=source)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT COALESCE(SUM(cost_micros), 0) AS n FROM llm_usage{where}",
            params,
        )
        return int(cur.fetchone()["n"])


def by_node(since) -> list[dict]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT node,
                   COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens,
                   COALESCE(SUM(cost_micros), 0) AS cost_micros
              FROM llm_usage
             WHERE created_at >= %s
             GROUP BY node
             ORDER BY tokens DESC
            """,
            (since,),
        )
        rows = cur.fetchall()
    return [
        {"node": r["node"], "tokens": int(r["tokens"]), "cost_micros": int(r["cost_micros"])}
        for r in rows
    ]


def by_source(since) -> list[dict]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT source,
                   COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens,
                   COALESCE(SUM(cost_micros), 0) AS cost_micros
              FROM llm_usage
             WHERE created_at >= %s
             GROUP BY source
             ORDER BY tokens DESC
            """,
            (since,),
        )
        rows = cur.fetchall()
    return [
        {
            "source": r["source"],
            "tokens": int(r["tokens"]),
            "cost_micros": int(r["cost_micros"]),
        }
        for r in rows
    ]


def by_day(since) -> list[dict]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT DATE(created_at) AS day,
                   COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens,
                   COALESCE(SUM(cost_micros), 0) AS cost_micros
              FROM llm_usage
             WHERE created_at >= %s
             GROUP BY DATE(created_at)
             ORDER BY day ASC
            """,
            (since,),
        )
        rows = cur.fetchall()
    return [
        {
            "day": r["day"].isoformat(),
            "tokens": int(r["tokens"]),
            "cost_micros": int(r["cost_micros"]),
        }
        for r in rows
    ]


def by_conversation(conversation_id: str) -> list[dict]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, source, node, model, input_tokens, output_tokens,
                   cost_micros, latency_ms, created_at
              FROM llm_usage
             WHERE conversation_id = %s
             ORDER BY id ASC
            """,
            (conversation_id,),
        )
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def _where(since, *, conversation_id=None, source=None) -> tuple[str, tuple]:
    clauses = ["created_at >= %s"]
    params: list = [since]
    if conversation_id is not None:
        clauses.append("conversation_id = %s")
        params.append(conversation_id)
    if source is not None:
        clauses.append("source = %s")
        params.append(source)
    return " WHERE " + " AND ".join(clauses), tuple(params)