"""评估样本接口：列表、新增、状态流转、三路转候选。

飞轮看板与人工运营共用。样本是评估的输入，脏样本会污染回归基线，
所以默认全部落候选池，必须人工采纳才进默认评估集。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core import store

router = APIRouter()


class CaseRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    expected_intent: str | None = None
    expected_doc_ids: list[str] | None = None
    expected_behavior: str = "answer"
    note: str | None = Field(default=None, max_length=255)


class StatusRequest(BaseModel):
    status: str


@router.get("/api/admin/eval/cases")
async def list_cases(status: str | None = None, limit: int = 100) -> dict:
    return {"items": store.list_eval_cases(status=status, limit=min(limit, 200))}


@router.post("/api/admin/eval/cases")
async def create_case(req: CaseRequest) -> dict:
    case_id = store.create_eval_case(
        req.query.strip(),
        expected_intent=req.expected_intent,
        expected_doc_ids=req.expected_doc_ids,
        expected_behavior=req.expected_behavior,
        source="manual",
        note=req.note,
    )
    return {"id": case_id}


@router.post("/api/admin/eval/cases/{case_id}/status")
async def change_status(case_id: int, req: StatusRequest) -> dict:
    if req.status not in ("candidate", "accepted", "retired"):
        raise HTTPException(status_code=422, detail="未知状态")
    if not store.set_eval_case_status(case_id, req.status):
        raise HTTPException(status_code=404, detail="样本不存在")
    return {"id": case_id, "status": req.status}


@router.post("/api/admin/eval/cases/from-feedback/{feedback_id}")
async def case_from_feedback(feedback_id: int) -> dict:
    row = store.feedback_by_id(feedback_id)
    if not row:
        raise HTTPException(status_code=404, detail="反馈不存在")
    query = store.feedback_query_for(row["message_id"])
    if not query:
        raise HTTPException(status_code=404, detail="该反馈没有可回溯的提问")
    case_id = store.create_eval_case(
        query, source="feedback", source_ref=str(feedback_id)
    )
    return {"id": case_id}