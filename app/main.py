"""FastAPI 应用：SSE 对话接口 + 静态页面托管。"""

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import AIMessage, HumanMessage
import time

from pydantic import BaseModel, Field, ValidationError

from app import store, vector_store
from app.checkpointer import get_checkpointer
from app.graph import build_graph
from app.llm import ConfigError, get_llm, get_llm_quiet
from app.markdown_split import split_markdown
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

# 图结构固定、llm/checkpointer 都是单例：compile 一次整个进程复用
_compiled_graph = None


async def _get_graph():
    global _compiled_graph
    if _compiled_graph is None:
        _compiled_graph = build_graph(
            checkpointer=await get_checkpointer(),
            llm=get_llm(),
            llm_quiet=get_llm_quiet(),
        )
    return _compiled_graph


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.init_db()
    try:
        get_llm()
    except ConfigError as exc:
        logger.error("%s", exc)
        raise
    # checkpoint 连接建在事件循环内（aiomysql 绑定循环，不能事后建）
    await get_checkpointer()
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


@app.post("/api/admin/reindex")
async def reindex(drop: bool = False) -> dict:
    """在服务进程内重建向量索引。

    Milvus Lite 独占数据文件，外部脚本无法在服务运行时打开同一文件，
    因此重建必须经由本接口触发。生产环境应加鉴权。
    """
    result = vector_store.rebuild_from_store(drop=drop)
    logger.info("向量重建完成：%s", result)
    return result


# ---------- 知识库管理 ----------
# 暂不鉴权，仅用于本地开发。上线前必须加保护。


class FaqPayload(BaseModel):
    title: str = Field(min_length=2, max_length=64)
    section: str = Field(min_length=2, max_length=64)
    category: str = Field(min_length=1, max_length=32)
    content: str = Field(min_length=20, max_length=2000)


def _index_faq(faq_id: str) -> None:
    """把单条知识写入向量库。失败只记日志——MySQL 已写入，可重建。"""
    row = store.faq_by_id(faq_id)
    if row is None or not row["enabled"]:
        return
    vector_store.upsert_documents([vector_store.faq_document(row)])


@app.get("/api/admin/faq")
async def admin_list_faq(category: str | None = None) -> dict:
    return {"items": store.list_faq(category)}


@app.get("/api/admin/faq/categories")
async def admin_faq_categories() -> dict:
    return {"categories": store.faq_categories()}


@app.get("/api/faq/{faq_id}")
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


@app.get("/api/admin/low-confidence")
async def admin_low_confidence(limit: int = 100) -> dict:
    """低置信度问题清单，用来决定补什么知识。"""
    limit = max(1, min(limit, 500))
    return {
        "items": store.list_low_confidence(limit),
        "stats": store.low_confidence_stats(),
    }


@app.get("/api/admin/faq/search")
async def admin_debug_search(q: str, top_k: int = 5) -> dict:
    """调试用：查看检索原始分数，绕过阈值过滤。

    录入界面靠它排查「新建后搜不到」到底是没进索引，还是分数不够。
    """
    hits = vector_store.search(q, "", top_k=top_k)
    return {"query": q, "hits": hits}


@app.post("/api/admin/faq")
async def admin_create_faq(payload: FaqPayload) -> dict:
    faq_id = store.create_faq_document(
        title=payload.title,
        section=payload.section,
        category=payload.category,
        content=payload.content,
    )
    _index_faq(faq_id)
    return {"id": faq_id, "item": store.faq_by_id(faq_id)}


@app.put("/api/admin/faq/{faq_id}")
async def admin_update_faq(faq_id: str, payload: FaqPayload) -> dict:
    if not store.update_faq_document(
        faq_id,
        title=payload.title,
        section=payload.section,
        category=payload.category,
        content=payload.content,
    ):
        raise HTTPException(status_code=404, detail="知识不存在")
    _index_faq(faq_id)
    return {"item": store.faq_by_id(faq_id)}


@app.post("/api/admin/faq/{faq_id}/toggle")
async def admin_toggle_faq(faq_id: str, enabled: bool) -> dict:
    if not store.set_faq_enabled(faq_id, enabled):
        raise HTTPException(status_code=404, detail="知识不存在")
    if enabled:
        _index_faq(faq_id)
    else:
        vector_store.delete_by_source("faq", faq_id)
    return {"item": store.faq_by_id(faq_id)}


@app.delete("/api/admin/faq/{faq_id}")
async def admin_delete_faq(faq_id: str) -> dict:
    if not store.delete_faq_document(faq_id):
        raise HTTPException(status_code=404, detail="知识不存在")
    vector_store.delete_by_source("faq", faq_id)
    return {"ok": True}


@app.get("/admin")
async def admin_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "admin.html")


# ---------- 批量导入 ----------


class BulkPreviewRequest(BaseModel):
    markdown: str = Field(min_length=1, max_length=100_000)
    title: str | None = Field(default=None, max_length=64)
    category: str | None = Field(default=None, max_length=32)
    section_mode: str = Field(default="leaf", pattern="^(leaf|path)$")
    max_chars: int = Field(default=500, ge=200, le=2000)


class BulkItem(BaseModel):
    """只做类型约束。长度规则统一交给 FaqPayload 校验，
    这样报错能带上「第 N 条」的下标，而不是 Pydantic 默认的数组索引。"""

    section: str
    content: str


class BulkImportRequest(BaseModel):
    title: str
    category: str
    items: list[BulkItem]


@app.post("/api/admin/faq/bulk/preview")
async def admin_bulk_preview(req: BulkPreviewRequest) -> dict:
    """切分预览。纯计算，不写库、不调嵌入。

    切分是启发式的，同一份文档可能有多种合理切法。必须让人先看清
    切了什么，再决定是否入库。
    """
    result = split_markdown(
        req.markdown,
        title=req.title,
        category=req.category,
        max_chars=req.max_chars,
        section_mode=req.section_mode,
    )

    sections = [c.section for c in result.chunks]
    duplicates = sorted({s for s in sections if sections.count(s) > 1})

    return {
        "title": result.title,
        "category": result.category,
        "chunks": [
            {
                "index": i,
                "section": c.section,
                "content": c.content,
                "chars": c.chars,
                "path": c.path,
                "kind": c.kind,
                "warnings": c.warnings,
            }
            for i, c in enumerate(result.chunks)
        ],
        "summary": {
            "chunks": len(result.chunks),
            "too_short": sum(1 for c in result.chunks if c.chars < 20),
            "duplicate_sections": duplicates,
        },
        "warnings": result.warnings,
    }


@app.post("/api/admin/faq/bulk")
async def admin_bulk_import(req: BulkImportRequest) -> dict:
    """批量入库。全部校验通过才写，写完全量写一次向量。"""
    if not req.items:
        raise HTTPException(status_code=422, detail="至少需要一条知识")
    if len(req.items) > 200:
        raise HTTPException(
            status_code=422, detail=f"单次最多 200 条，当前 {len(req.items)} 条"
        )

    for i, item in enumerate(req.items):
        try:
            FaqPayload(
                title=req.title,
                section=item.section,
                category=req.category,
                content=item.content,
            )
        except ValidationError as exc:
            detail = "; ".join(
                f"{'.'.join(str(p) for p in err['loc']) or '字段'}: {err['msg']}"
                for err in exc.errors()
            )
            raise HTTPException(
                status_code=422, detail=f"第 {i + 1} 条不合法：{detail}"
            ) from exc

    ids = store.create_faq_documents(
        title=req.title,
        category=req.category,
        items=[{"section": i.section, "content": i.content} for i in req.items],
    )

    docs = [vector_store.faq_document(store.faq_by_id(i)) for i in ids]
    indexed = vector_store.upsert_documents(docs)

    return {
        "created": [
            {"id": faq_id, "section": item.section}
            for faq_id, item in zip(ids, req.items)
        ],
        "indexed": indexed,
        "total": len(ids),
    }


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
        started = time.monotonic()
        try:
            graph = await _get_graph()

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


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
