"""工具调用审计：每次执行一行，参数脱敏、结果只留摘要。

审计是观测数据，写库失败只记日志，绝不能影响工具返回。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.core.db import connect, now

logger = logging.getLogger(__name__)

SENSITIVE_KEY_RE = re.compile(
    r"phone|mobile|address|real_name|id_card|email", re.IGNORECASE
)

ARGS_VALUE_LIMIT = 200
RESULT_DIGEST_LIMIT = 500


def mask_args(args: dict[str, Any] | None) -> dict[str, Any]:
    """键名命中敏感词的字段整体替换为 ***，其余值超长截断。"""
    masked: dict[str, Any] = {}
    for key, value in (args or {}).items():
        if SENSITIVE_KEY_RE.search(str(key)):
            masked[key] = "***"
            continue
        if isinstance(value, str) and len(value) > ARGS_VALUE_LIMIT:
            masked[key] = value[:ARGS_VALUE_LIMIT] + "…"
        else:
            masked[key] = value
    return masked


def digest(result: Any) -> str | None:
    """结果摘要：JSON 序列化后截断，不存全量结果。"""
    if result is None:
        return None
    try:
        text = json.dumps(result, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001 - 序列化失败退化为 str
        text = str(result)
    return text[:RESULT_DIGEST_LIMIT]


def _error_text(exc: BaseException | str | None) -> str | None:
    if exc is None:
        return None
    if isinstance(exc, str):
        return exc.splitlines()[0][:500]
    first_line = (str(exc) or "").splitlines()[0]
    return f"{exc.__class__.__name__}: {first_line}"[:500]


def record(
    tool_name: str,
    source: str,
    caller: str,
    status: str,
    *,
    conversation_id: str | None = None,
    visitor_id: str | None = None,
    args: dict[str, Any] | None = None,
    result: Any = None,
    error: BaseException | str | None = None,
    duration_ms: int | None = None,
    action_id: str | None = None,
) -> None:
    """写一行审计。失败只 warning。"""
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tool_audit_logs
                    (conversation_id, visitor_id, tool_name, source, caller,
                     status, args_json, result_digest, error, duration_ms,
                     action_id, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    conversation_id,
                    visitor_id,
                    tool_name[:64],
                    source,
                    caller,
                    status,
                    json.dumps(mask_args(args), ensure_ascii=False) if args is not None else None,
                    digest(result),
                    _error_text(error),
                    duration_ms,
                    action_id,
                    now(),
                ),
            )
    except Exception:
        logger.warning("审计写入失败，不影响工具调用", exc_info=True)


def list_logs(
    *,
    tool: str | None = None,
    visitor_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """按条件倒序查审计流水，供管理页使用。"""
    clauses: list[str] = []
    params: list[Any] = []
    if tool:
        clauses.append("tool_name = %s")
        params.append(tool)
    if visitor_id:
        clauses.append("visitor_id = %s")
        params.append(visitor_id)
    if status:
        clauses.append("status = %s")
        params.append(status)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend([max(1, min(limit, 500)), max(0, offset)])

    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, conversation_id, visitor_id, tool_name, source, caller,
                   status, args_json, result_digest, error, duration_ms,
                   action_id, created_at
              FROM tool_audit_logs
              {where}
             ORDER BY id DESC
             LIMIT %s OFFSET %s
            """,
            tuple(params),
        )
        rows = cur.fetchall()

    logs = []
    for row in rows:
        if isinstance(row["args_json"], str):
            row["args_json"] = json.loads(row["args_json"])
        logs.append({**row, "created_at": row["created_at"].isoformat()})
    return logs