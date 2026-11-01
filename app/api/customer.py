"""顾客侧接口：对话 SSE、历史、会话、订单、工单、FAQ 详情。

chat 的图单例与轨迹记录也在这里——它们只服务对话流。
"""

from __future__ import annotations

import logging
import os
import time

from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, Field, ValidationError

from app import store
from app.api._graph import get_graph
from app.llm import get_llm_quiet
from app.schemas import WorkOrderCreate
from app.tools import AgentContext

logger = logging.getLogger(__name__)

TOOL_LABELS = {
    "lookup_product": "商品",
    "list_my_orders": "订单",
    "lookup_work_order": "工单",
    "draft_work_order": "工单草稿",
}

DRAFT_TOOL = "draft_work_order"


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
    import json

    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _draft_from_tool_event(event: dict, visitor_id: str, conversation_id: str):
    """从工具输出里取建单参数，交给后端建草稿。工具失败或参数缺失时返回 None。

    这里刻意不读模型的自然语言输出：草稿内容只来自工具的结构化入参。
    """
    import json

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
    if payload.get("drafted") is not True:
        return None

    order_no = payload.get("order_no")
    issue_type = payload.get("issue_type")
    if not issue_type:
        return None

    tool_input = event.get("data", {}).get("input", {})
    description = tool_input.get("description")
    if not description:
        return None

    try:
        return store.create_work_order_draft(
            visitor_id=visitor_id,
            conversation_id=conversation_id,
            order_no=order_no,
            issue_type=issue_type,
            description=description,
        )
    except Exception:  # noqa: BLE001 - 草稿失败不影响对话继续
        logger.exception("生成工单草稿失败")
        return None


def _record_turn_trace(
    conversation_id: str,
    visitor_id: str,
    message: str,
    state: dict,
    started: float,
) -> None:
    """把一轮对话的工作字段写入 agent_traces。"""
    detail = None
    if state.get("rewritten_terms"):
        # r16 改写观测：数据飞轮分析词条命中与回退率
        detail = {
            "rewritten_terms": state["rewritten_terms"],
            "rewritten_query": state.get("rewritten_query", ""),
            "fallback_used": state.get("fallback_used", False),
        }
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

    history_limit = int(os.getenv("HISTORY_LIMIT", "20"))
    history = store.load_history(conversation_id, history_limit)
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
                    if name == DRAFT_TOOL:
                        # 草稿由后端按工具实际入参生成并直接推给前端，
                        # 卡片内容不经过模型转述，杜绝编造
                        draft = _draft_from_tool_event(
                            event, req.visitor_id, conversation_id
                        )
                        if draft is not None:
                            yield _sse({"work_order_draft": draft})

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

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
