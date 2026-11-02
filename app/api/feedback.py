"""顾客侧反馈接口：赞踩与标签/文字补充。

反馈必须绑定到具体某条回答，所以入口只接受 message_id；归属校验走
消息所属会话的 visitor_id，越权按 404 处理，不泄露存在性。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core import store

router = APIRouter()


class FeedbackRequest(BaseModel):
    message_id: int = Field(gt=0)
    visitor_id: str = Field(min_length=1, max_length=64)
    rating: int = Field(ge=-1, le=1)
    reason: str | None = None
    comment: str | None = Field(default=None, max_length=500)


@router.post("/api/feedback")
async def submit(req: FeedbackRequest) -> dict:
    """标签与文字都是可选的：顾客只想点个赞也必须能提交。"""
    msg = store.feedback_message_of(req.message_id)
    if not msg or msg["visitor_id"] != req.visitor_id or msg["role"] != "assistant":
        raise HTTPException(status_code=404, detail="消息不存在")
    if req.reason is not None and req.reason not in store.feedback_reasons:
        raise HTTPException(status_code=422, detail="未知的反馈标签")
    if req.rating == 0:
        removed = store.remove_feedback(req.message_id)
        return {"removed": removed}
    comment = (req.comment or "").strip() or None
    store.upsert_feedback(
        req.message_id,
        msg["conversation_id"],
        req.visitor_id,
        req.rating,
        req.reason,
        comment,
    )
    return {
        "message_id": req.message_id,
        "rating": req.rating,
        "reason": req.reason,
        "comment": comment,
    }


@router.get("/api/feedback")
async def list_by_conversation(conversation_id: str, visitor_id: str) -> dict:
    if not store.conversation_owned_by(conversation_id, visitor_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"items": store.list_feedback(conversation_id)}