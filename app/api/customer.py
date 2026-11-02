"""顾客侧接口：对话 SSE、历史、会话、订单、工单、FAQ 详情。

chat 的图单例与轨迹记录也在这里——它们只服务对话流。
"""

from __future__ import annotations

import json
import logging
import os
import time

from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, Field, ValidationError

from app import store
from app.api._graph import get_graph
from app.context_builder import build_context
from app.llm import get_llm_quiet
from app.schemas import WorkOrderCreate
from app.tools import AgentContext, tool_labels
from app.tools.executor import ActionError, executor

logger = logging.getLogger(__name__)

TOOL_LABELS = tool_labels()


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    visitor_id: str = Field(min_length=1, max_length=64)
    conversation_id: str | None = None


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _sse(payload) -> str:
    if payload == "[DONE]":
        return "data: [DONE]\n\n"
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _pending_from_tool_event(event: dict) -> dict | None:
    """从工具输出里取待确认动作。卡片内容由执行引擎生成，不经模型转述。"""
    output = event.get("data", {}).get("output")
    raw = getattr(output, "content", output)
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
    else:
        payload = raw
    if not isinstance(payload, dict):
        return None
    action = payload.get("pending_action")
    return action if isinstance(action, dict) else None


def _record_turn_trace(
    conversation_id: str,
    visitor_id: str,
    message: str,
    state: dict,
    started: float,
) -> None:
    """把一轮对话的工作字段写入 agent_traces。"""
    detail = {}
    if state.get("rewritten_terms"):
        # r16 改写观测：数据飞轮分析词条命中与回退率
        detail["rewritten_terms"] = state["rewritten_terms"]
        detail["rewritten_query"] = state.get("rewritten_query", "")
        detail["fallback_used"] = state.get("fallback_used", False)
    if state.get("context_budget"):
        # r18 上下文账目：每轮的预算构成可观测
        detail["budget"] = state["context_budget"]
    if not detail:
        detail = None
    try:
        store.record_trace(
            conversation_id=conversation_id,
            visitor_id=visitor_id,
            message=message,
            intent=state.get("intent", "unknown"),
            intent_method=state.get("intent_method", "unknown"),
            node_path=state.get("node_path", ""),
            latency_ms=int((time.monotonic() - started) * 1000),
            resolved_query=state.get("resolved_query"),
            confidence=state.get("intent_confidence"),
            top_score=state.get("top_score"),
            tool_calls=state.get("tool_calls"),
            steps=state.get("steps"),
            detail=detail,
        )
    except Exception:  # noqa: BLE001 - 轨迹是观测数据，失败不影响对话
        logger.warning("轨迹记录失败", exc_info=True)


from fastapi import APIRouter

router = APIRouter()


@router.post("/api/chat")
async def chat(req: ChatRequest) -> StreamingResponse:
    conversation_id = req.conversation_id
    if not conversation_id or not store.conversation_owned_by(
        conversation_id, req.visitor_id
    ):
        conversation_id = store.create_conversation(req.visitor_id)

    # r18：token 预算组装（梗概 + 预算内原文），本轮输入参与扣减
    bundle = build_context(conversation_id, req.message)
    history: list[dict] = []
    if bundle.summary_text:
        history.append({"role": "user", "content": f"【此前对话梗概】{bundle.summary_text}"})
    history.extend(bundle.recent_messages)
    store.append_message(conversation_id, "user", req.message)

    async def stream():
        yield _sse({"conversation_id": conversation_id})
        collected: list[str] = []
        saved = False
        started = time.monotonic()
        try:
            graph = await get_graph(get_llm_quiet)

            async for event in graph.astream_events(
                {
                    "message": req.message,
                    "history": history,
                    "visitor_id": req.visitor_id,
                },
                context=AgentContext(visitor_id=req.visitor_id),
                config={"configurable": {"thread_id": conversation_id}},
                version="v2",
            ):
                kind = event.get("event")
                if kind == "on_chat_model_stream":
                    text = _text_of(event["data"]["chunk"].content)
                    if text:
                        collected.append(text)
                        yield _sse({"delta": text})
                elif kind == "on_tool_start":
                    name = event.get("name", "")
                    label = TOOL_LABELS.get(name, name)
                    yield _sse({"tool": name, "label": label, "status": "start"})
                elif kind == "on_tool_end":
                    name = event.get("name", "")
                    label = TOOL_LABELS.get(name, name)
                    yield _sse({"tool": name, "label": label, "status": "done"})
                    action = _pending_from_tool_event(event)
                    if action is not None:
                        # 卡片由执行引擎生成并直接推给前端，内容不经过模型转述
                        yield _sse({"pending_action": action})

            # 模板/知识路径的 final_text 不经 LLM 流式（或拼接生成），
            # 事件流里没有逐 token delta，从最终 state 一次性补发。
            # business 路径 delta 已流过则跳过；触线时 final_text 是转人工
            # 话术，必须补发——触线前流出的部分回答不算完整回答
            state = await graph.aget_state(
                {"configurable": {"thread_id": conversation_id}}
            )
            values = state.values or {}
            final_text = values.get("final_text", "")
            stopped_by = values.get("stopped_by")
            if stopped_by and final_text:
                collected = [final_text]
                yield _sse({"delta": final_text})
            elif not collected and final_text:
                collected.append(final_text)
                yield _sse({"delta": final_text})

            assistant_text = "".join(collected).strip()
            if assistant_text:
                store.append_message(conversation_id, "assistant", assistant_text)
                saved = True

            # 轨迹落库：观测数据，失败不影响对话（record_trace 自兜底）
            values["context_budget"] = bundle.budget
            _record_turn_trace(
                conversation_id, req.visitor_id, req.message, values, started
            )
            yield _sse("[DONE]")
        except Exception as exc:  # noqa: BLE001 - 上游任何异常都要转成前端可见错误
            logger.exception("对话失败")
            yield _sse({"error": f"服务暂时不可用：{exc.__class__.__name__}"})
        finally:
            # 客户端中断时把已生成的部分落库，保持会话完整可回看
            if not saved and collected:
                store.append_message(
                    conversation_id, "assistant", "".join(collected).strip()
                )
            # r18：原文超预算时后台异步压缩（不阻塞响应、失败下轮重评）
            import asyncio

            asyncio.create_task(_summarize_quietly(conversation_id))

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _summarize_quietly(conversation_id: str) -> None:
    try:
        from app.summarizer import maybe_summarize

        await maybe_summarize(conversation_id)
    except Exception:  # noqa: BLE001 - 后台任务任何异常都不外泄
        logger.warning("后台摘要任务异常", exc_info=True)


@router.get("/api/history")
async def history(conversation_id: str, visitor_id: str, limit: int = 200) -> dict:
    """会话历史。越权（不是本人的会话）按不存在处理，返回 404。"""
    if not store.conversation_owned_by(conversation_id, visitor_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"messages": store.load_history(conversation_id, min(limit, 200))}


@router.get("/api/conversations")
async def conversations(visitor_id: str) -> dict:
    return {"conversations": store.list_conversations(visitor_id)}


@router.get("/api/work-orders")
async def work_orders(visitor_id: str) -> dict:
    return {"work_orders": store.list_work_orders(visitor_id)}


@router.get("/api/orders")
async def orders(visitor_id: str) -> dict:
    return {"orders": store.list_orders(visitor_id)}


@router.get("/api/faq/{faq_id}")
async def faq_detail(faq_id: str) -> dict:
    """按 id 取单条政策全文，供对话页的引用悬停使用。

    这是公开端点：政策本来就是给顾客看的，不需要管理员权限。
    """
    row = store.faq_by_id(faq_id)
    if row is None or not row["enabled"]:
        raise HTTPException(status_code=404, detail="知识不存在")
    return {
        "id": row["id"],
        "doc_id": row["doc_id"],
        "title": row["title"],
        "category": row["category"],
        "section": row["section"],
        "content": row["content"],
    }


class WorkOrderConfirm(BaseModel):
    draft_id: str = Field(min_length=1, max_length=64)
    visitor_id: str = Field(min_length=1, max_length=64)


class WorkOrderDraftCreate(BaseModel):
    visitor_id: str = Field(min_length=1, max_length=64)
    order_no: str | None = Field(default=None, max_length=32)
    issue_type: str = Field(min_length=1, max_length=32)
    description: str = Field(min_length=1, max_length=500)


@router.post("/api/work-orders/drafts")
async def create_draft(req: WorkOrderDraftCreate) -> dict:
    """顾客在界面上主动提单时使用。内容由顾客填写，AI 不参与。"""
    try:
        query = WorkOrderCreate(
            order_no=req.order_no,
            issue_type=req.issue_type,
            description=req.description,
        )
    except ValidationError as exc:
        detail = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '参数'}: {err['msg']}"
            for err in exc.errors()
        )
        raise HTTPException(status_code=422, detail=detail) from exc

    if query.order_no is not None and not store.visitor_has_order(
        req.visitor_id, query.order_no
    ):
        raise HTTPException(status_code=404, detail="订单不存在或不属于当前顾客")

    existing = store.find_open_work_order(
        req.visitor_id, query.order_no, query.issue_type
    )
    if existing is not None:
        return {
            "duplicate": True,
            "existing_work_order_no": existing["id"],
            "existing_status": existing["status"],
        }

    draft = store.create_work_order_draft(
        visitor_id=req.visitor_id,
        conversation_id="",
        order_no=query.order_no,
        issue_type=query.issue_type,
        description=query.description,
    )
    return draft


@router.post("/api/work-orders")
async def confirm_work_order(req: WorkOrderConfirm) -> dict:
    """顾客在界面上点确认后才会走这里。模型的输出与前端提交的字段都不参与
    写入内容——草稿内容取自数据库，前端只能提供草稿 id。"""
    draft = store.get_work_order_draft(req.draft_id, req.visitor_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="草稿不存在或已失效")

    try:
        existing = store.find_open_work_order(
            req.visitor_id, draft["order_no"], draft["issue_type"]
        )
        if existing is not None:
            store.delete_work_order_draft(req.draft_id)
            return {
                "created": False,
                "reason": "duplicate",
                "work_order": store.get_work_order(req.visitor_id, existing["id"]),
            }

        work_order_no = store.create_work_order(
            visitor_id=req.visitor_id,
            order_no=draft["order_no"],
            issue_type=draft["issue_type"],
            description=draft["description"],
        )
        store.delete_work_order_draft(req.draft_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("提交工单失败")
        raise HTTPException(status_code=500, detail="提交失败，请稍后重试") from exc

    return {
        "created": True,
        "work_order": store.get_work_order(req.visitor_id, work_order_no),
    }


class PendingActionRequest(BaseModel):
    action_id: str = Field(min_length=1, max_length=64)
    visitor_id: str = Field(min_length=1, max_length=64)


@router.post("/api/tool/confirm")
async def confirm_pending_action(req: PendingActionRequest) -> dict:
    """顾客点确认后才会真正执行挂起的工具。参数取自数据库，前端只提供 id。"""
    try:
        result = await executor.confirm(req.action_id, visitor_id=req.visitor_id)
    except ActionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return {"status": "confirmed", "result": result}


@router.post("/api/tool/cancel")
async def cancel_pending_action(req: PendingActionRequest) -> dict:
    try:
        return await executor.cancel(req.action_id, visitor_id=req.visitor_id)
    except ActionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc