"""评估样本、运行记录与明细的读写。

样本三路来源：负反馈、低置信度、评估失败项，加手工新增。候选池人工
采纳后才进入默认评估集——不能让脏样本自动变成回归基线。
"""

from __future__ import annotations

import json
from datetime import timedelta

from app.core.db import connect, now

CASE_STATUSES = ("candidate", "accepted", "retired")


def create_case(
    query: str,
    *,
    expected_intent: str | None = None,
    expected_doc_ids: list[str] | None = None,
    expected_behavior: str = "answer",
    source: str = "manual",
    source_ref: str | None = None,
    note: str | None = None,
) -> int:
    stamp = now()
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO eval_cases
                (query, expected_intent, expected_doc_ids, expected_behavior,
                 source, source_ref, status, note, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, 'candidate', %s, %s, %s)
            """,
            (
                query,
                expected_intent,
                _dump(expected_doc_ids),
                expected_behavior,
                source,
                source_ref,
                note,
                stamp,
                stamp,
            ),
        )
        return int(cur.lastrowid)


def list_cases(status: str | None = None, limit: int = 100) -> list[dict]:
    sql = (
        "SELECT id, query, expected_intent, expected_doc_ids, expected_behavior,"
        "       source, source_ref, status, note, created_at, updated_at"
        "  FROM eval_cases"
    )
    params: tuple = ()
    if status is not None:
        sql += " WHERE status = %s"
        params = (status,)
    sql += " ORDER BY id DESC LIMIT %s"
    params = (*params, limit)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [_serialize_case(r) for r in rows]


def get_case(case_id: int) -> dict | None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, query, expected_intent, expected_doc_ids, expected_behavior,
                   source, source_ref, status, note, created_at, updated_at
              FROM eval_cases
             WHERE id = %s
            """,
            (case_id,),
        )
        row = cur.fetchone()
    return _serialize_case(row) if row else None


def set_case_status(case_id: int, status: str) -> bool:
    if status not in CASE_STATUSES:
        return False
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE eval_cases SET status = %s, updated_at = %s WHERE id = %s",
            (status, now(), case_id),
        )
        return cur.rowcount > 0


def create_run(run_id: str, *, sample_size: int, config: dict | None = None) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO eval_runs
                (run_id, sample_size, config_json, status, started_at)
            VALUES (%s, %s, %s, 'running', %s)
            """,
            (run_id, sample_size, _dump(config), now()),
        )


def finish_run(
    run_id: str,
    *,
    metrics: dict | None = None,
    report_path: str | None = None,
    status: str = "done",
    error: str | None = None,
) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE eval_runs
               SET metrics_json = %s, report_path = %s, status = %s,
                   error = %s, finished_at = %s
             WHERE run_id = %s
            """,
            (_dump(metrics), report_path, status, error, now(), run_id),
        )


def get_run(run_id: str) -> dict | None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT run_id, sample_size, config_json, metrics_json, report_path,
                   status, error, started_at, finished_at
              FROM eval_runs
             WHERE run_id = %s
            """,
            (run_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    data = dict(row)
    data["config"] = _load(data.pop("config_json"))
    data["metrics"] = _load(data.pop("metrics_json"))
    for key in ("started_at", "finished_at"):
        if data.get(key) is not None:
            data[key] = data[key].isoformat()
    return data


def list_runs(limit: int = 20) -> list[dict]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT run_id, sample_size, config_json, metrics_json, report_path,
                   status, error, started_at, finished_at
              FROM eval_runs
             ORDER BY started_at DESC
             LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
    out = []
    for row in rows:
        data = dict(row)
        data["config"] = _load(data.pop("config_json"))
        data["metrics"] = _load(data.pop("metrics_json"))
        for key in ("started_at", "finished_at"):
            if data.get(key) is not None:
                data[key] = data[key].isoformat()
        out.append(data)
    return out


def running_exists() -> bool:
    """同一时刻只允许一轮——评估要花钱，串行避免额度被打爆。"""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM eval_runs WHERE status = 'running'")
        return int(cur.fetchone()["n"]) > 0


def fail_stale_runs(timeout_minutes: int = 60) -> int:
    """进程崩溃会留下永远 running 的记录，不清掉就再也触发不了评估。"""
    cutoff = now() - timedelta(minutes=timeout_minutes)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE eval_runs
               SET status = 'failed', error = '运行超时，疑似进程中断',
                   finished_at = %s
             WHERE status = 'running' AND started_at < %s
            """,
            (now(), cutoff),
        )
        return int(cur.rowcount)


def add_item(
    run_id: str,
    case_id: int,
    query: str,
    *,
    intent: str | None = None,
    answer: str | None = None,
    cited_ids: list[str] | None = None,
    passed: bool = False,
    refused: bool = False,
    checks: dict | None = None,
    latency_ms: int | None = None,
    cost_micros: int | None = None,
    error: str | None = None,
) -> int:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO eval_run_items
                (run_id, case_id, query, intent, answer, cited_ids, passed,
                 refused, checks_json, latency_ms, cost_micros, error, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                run_id,
                case_id,
                query,
                intent,
                answer,
                _dump(cited_ids),
                int(passed),
                int(refused),
                _dump(checks),
                latency_ms,
                cost_micros,
                error,
                now(),
            ),
        )
        return int(cur.lastrowid)


def list_items(run_id: str) -> list[dict]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, run_id, case_id, query, intent, answer, cited_ids, passed,
                   refused, checks_json, latency_ms, cost_micros, error, created_at
              FROM eval_run_items
             WHERE run_id = %s
             ORDER BY id ASC
            """,
            (run_id,),
        )
        rows = cur.fetchall()
    out = []
    for row in rows:
        data = dict(row)
        data["cited_ids"] = _load(data.pop("cited_ids")) or []
        data["checks"] = _load(data.pop("checks_json")) or {}
        data["passed"] = bool(data["passed"])
        data["refused"] = bool(data["refused"])
        if data.get("created_at") is not None:
            data["created_at"] = data["created_at"].isoformat()
        out.append(data)
    return out


def item_by_id(item_id: int) -> dict | None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, run_id, case_id, query, intent, answer, cited_ids, passed,
                   refused, checks_json, latency_ms, cost_micros, error, created_at
              FROM eval_run_items
             WHERE id = %s
            """,
            (item_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    data = dict(row)
    data["cited_ids"] = _load(data.pop("cited_ids")) or []
    data["checks"] = _load(data.pop("checks_json")) or {}
    data["passed"] = bool(data["passed"])
    data["refused"] = bool(data["refused"])
    if data.get("created_at") is not None:
        data["created_at"] = data["created_at"].isoformat()
    return data


def _serialize_case(row: dict) -> dict:
    data = dict(row)
    data["expected_doc_ids"] = _load(data.get("expected_doc_ids"))
    for key in ("created_at", "updated_at"):
        if data.get(key) is not None:
            data[key] = data[key].isoformat()
    return data


def _dump(value) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def _load(raw):
    if raw is None:
        return None
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None