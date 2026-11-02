"""成本闸事件记录。

只负责「记下哪一层闸在什么时候被触发」，判定逻辑在 `app.cost.budget`。
"""

from __future__ import annotations

from app.core.db import connect, now


def record(
    scope: str,
    reason: str,
    *,
    conversation_id: str | None = None,
    visitor_id: str | None = None,
    detail: str | None = None,
) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO budget_events
                (scope, reason, conversation_id, visitor_id, detail, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (scope, reason, conversation_id, visitor_id, detail, now()),
        )


def sum_by_scope(since) -> dict:
    """按闸层计数：看板顶部「触闸（全局/会话/单轮）」。"""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT scope, COUNT(*) AS n FROM budget_events"
            " WHERE created_at >= %s GROUP BY scope",
            (since,),
        )
        rows = cur.fetchall()
    counts = {"global": 0, "conversation": 0, "turn": 0}
    for row in rows:
        counts[row["scope"]] = int(row["n"])
    return counts