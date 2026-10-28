"""FastAPI 应用：SSE 对话接口 + 静态页面托管。"""

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, Field, ValidationError

from app import store
from app.agent import build_agent
from app.llm import ConfigError, get_llm
from app.schemas import WorkOrderCreate
from app.tools import AgentContext

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

TOOL_LABELS = {
    "lookup_product": "商品",
    "list_my_orders": "订单",
    "lookup_work_order": "工单",
    "draft_work_order": "工单草稿",
}

DRAFT_TOOL = "draft_work_order"


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.init_db()
    try:
        get_llm()
    except ConfigError as exc:
        logger.error("%s", exc)
        raise
    yield


app = FastAPI(title="ShopPilot", lifespan=lifespan)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    visitor_id: str = Field(min_length=1, max_length=64)
    conversation_id: str | None = None


def _text_of(content) -> str:
    """兼容 content 为字符串或内容块列表两种形态。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") in (None, "text"):
                parts.append(block.get("text", ""))
        return "".join(parts)
    return ""


def _sse(payload) -> str:
    if isinstance(payload, str):
        return f"data: {payload}\n\n"
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _draft_from_tool_event(event: dict, visitor_id: str, conversation_id: str):
    """从工具输出里取建单参数，交给后端建草稿。工具失败或参数缺失时返回 None。

    这里刻意不读模型的自然语言输出：草稿内容只来自工具的结构化入参。
    """
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


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.get("/api/history")
async def history(conversation_id: str, visitor_id: str, limit: int = 200) -> dict:
    if not store.conversation_owned_by(conversation_id, visitor_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"messages": store.load_history(conversation_id, limit)}


@app.get("/api/conversations")
async def conversations(visitor_id: str) -> dict:
    return {"conversations": store.list_conversations(visitor_id)}


@app.get("/api/work-orders")
async def work_orders(visitor_id: str) -> dict:
    return {"work_orders": store.list_work_orders(visitor_id)}


@app.get("/api/orders")
async def orders(visitor_id: str) -> dict:
    return {"orders": store.list_orders(visitor_id)}


class WorkOrderConfirm(BaseModel):
    draft_id: str = Field(min_length=1, max_length=64)
    visitor_id: str = Field(min_length=1, max_length=64)


class WorkOrderDraftCreate(BaseModel):
    visitor_id: str = Field(min_length=1, max_length=64)
    order_no: str | None = Field(default=None, max_length=32)
    issue_type: str = Field(min_length=1, max_length=32)
    description: str = Field(min_length=1, max_length=500)


@app.post("/api/work-orders/drafts")
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


@app.post("/api/work-orders")
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


@app.post("/api/chat")
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
        try:
            agent = build_agent()
            messages = []
            for m in history:
                cls = HumanMessage if m["role"] == "user" else AIMessage
                messages.append(cls(content=m["content"]))
            messages.append(HumanMessage(content=req.message))

            async for event in agent.astream_events(
                {"messages": messages},
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

            assistant_text = "".join(collected).strip()
            if assistant_text:
                store.append_message(conversation_id, "assistant", assistant_text)
                saved = True
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


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
