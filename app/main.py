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
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app import store
from app.llm import ConfigError, get_llm
from app.prompt import SYSTEM_PROMPT

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


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
            llm = get_llm()
            messages = [SystemMessage(content=SYSTEM_PROMPT)]
            for m in history:
                cls = HumanMessage if m["role"] == "user" else AIMessage
                messages.append(cls(content=m["content"]))
            messages.append(HumanMessage(content=req.message))

            async for chunk in llm.astream(messages):
                text = _text_of(chunk.content)
                if text:
                    collected.append(text)
                    yield _sse({"delta": text})

            assistant_text = "".join(collected)
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
                store.append_message(conversation_id, "assistant", "".join(collected))

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
