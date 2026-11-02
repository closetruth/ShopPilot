"""数据飞轮看板数据接口。

反馈概览、成本分布、低置信度池、评估运行与转候选。数据源全部是 MySQL，
Langfuse 只服务排障，不进看板。

转负反馈为样本的端点放在 `app/api/eval_cases.py`，这里不重复声明。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.core import store
from app.cost import budget
from app.evals.runner import run_eval
from app.observability import usage
from app.repo import evals as evals_repo
from app.repo import insights as insights_repo
from app.repo import usage as usage_repo

router = APIRouter()

MAX_RUN_LIMIT = 50

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@router.get("/admin/flywheel")
async def flywheel_page() -> FileResponse:
    return FileResponse(_STATIC_DIR / "admin_flywheel.html")


class RunRequest(BaseModel):
    limit: int = 30
    case_ids: list[int] | None = None
    judge: bool = False


# ---------- 看板数据 ----------


@router.get("/api/admin/flywheel/feedback")
async def flywheel_feedback(days: int = 7, limit: int = 50) -> dict:
    stats = store.feedback_stats(days=days)
    items = store.list_negative_feedback(days=days, limit=min(limit, 200))
    by_reason: dict[str, int] = {}
    for item in items:
        key = item.get("reason") or "unlabeled"
        by_reason[key] = by_reason.get(key, 0) + 1
    return {
        "days": days,
        "total": stats["positive"] + stats["negative"],
        **stats,
        "by_reason": by_reason,
        "items": items,
    }


@router.get("/api/admin/flywheel/cost")
async def flywheel_cost(days: int = 7) -> dict:
    since = store.now() - timedelta(days=days)
    today = budget.day_start()
    return {
        "days": days,
        "today": {
            "tokens": usage_repo.sum_tokens_since(today),
            "cost_micros": usage_repo.sum_cost_since(today),
        },
        "by_node": usage_repo.by_node(since),
        "by_source": usage_repo.by_source(since),
        "by_day": usage_repo.by_day(since),
        "budget_events": budget.summary(since),
        "price_configured": usage.price_configured(),
    }


@router.get("/api/admin/flywheel/low-confidence")
async def flywheel_low_confidence(limit: int = 50) -> dict:
    return {
        "stats": insights_repo.stats(),
        "items": insights_repo.list_recent(limit=min(limit, 200)),
    }


# ---------- 评估运行 ----------


@router.get("/api/admin/eval/runs")
async def list_runs(limit: int = 20) -> dict:
    return {"items": evals_repo.list_runs(limit=min(limit, 100))}


@router.post("/api/admin/eval/run")
async def trigger_run(req: RunRequest) -> dict:
    """评估要花钱，规模必须显式给出；同一时刻只允许一轮。"""
    if req.limit < 1 or req.limit > MAX_RUN_LIMIT:
        raise HTTPException(
            status_code=422, detail=f"单次评估样本数需为 1-{MAX_RUN_LIMIT}"
        )
    evals_repo.fail_stale_runs()
    if evals_repo.running_exists():
        raise HTTPException(status_code=409, detail="已有评估在跑")

    run_id = uuid.uuid4().hex[:12]
    evals_repo.create_run(
        run_id,
        sample_size=req.limit,
        config={"limit": req.limit, "case_ids": req.case_ids, "judge": req.judge},
    )
    asyncio.create_task(
        run_eval(run_id, limit=req.limit, case_ids=req.case_ids, judge=req.judge)
    )
    return {"run_id": run_id}


@router.get("/api/admin/eval/runs/{run_id}")
async def run_detail(run_id: str) -> dict:
    run = evals_repo.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="评估运行不存在")
    return {"run": run, "items": evals_repo.list_items(run_id)}


# ---------- 转候选 ----------


@router.post("/api/admin/eval/cases/from-low-conf/{low_conf_id}")
async def case_from_low_conf(low_conf_id: int) -> dict:
    row = store.low_confidence_by_id(low_conf_id)
    if not row:
        raise HTTPException(status_code=404, detail="低置信度记录不存在")
    case_id = store.create_eval_case(
        row["query"], source="low_conf", source_ref=str(low_conf_id)
    )
    return {"id": case_id}


@router.post("/api/admin/eval/cases/from-item/{item_id}")
async def case_from_item(item_id: int) -> dict:
    item = evals_repo.item_by_id(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="评估明细不存在")
    case_id = store.create_eval_case(
        item["query"],
        expected_intent=item.get("intent"),
        source="eval_fail",
        source_ref=str(item_id),
    )
    return {"id": case_id}