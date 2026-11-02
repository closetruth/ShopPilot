"""管理侧接口：FAQ CRUD、批量导入、重建索引、低置信度池。

无鉴权，仅限本地开发（README 有标注）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, ValidationError

from app.core import store
from app.retrieval import vector_store
from app.retrieval.markdown_split import split_markdown
from app.tools.registry import registry

logger = logging.getLogger(__name__)

router = APIRouter()

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@router.post("/api/admin/reindex")
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


@router.get("/api/admin/faq")
async def admin_list_faq(category: str | None = None) -> dict:
    return {"items": store.list_faq(category)}


@router.get("/api/admin/faq/categories")
async def admin_faq_categories() -> dict:
    return {"categories": store.faq_categories()}


@router.get("/api/admin/low-confidence")
async def admin_low_confidence(limit: int = 100) -> dict:
    """低置信度问题清单，用来决定补什么知识。"""
    limit = max(1, min(limit, 500))
    return {
        "items": store.list_low_confidence(limit),
        "stats": store.low_confidence_stats(),
    }


@router.get("/api/admin/context-usage")
async def admin_context_usage(conversation_id: str, limit: int = 20) -> dict:
    """上下文账目：最近几轮的预算构成（r18 可观测）。

    从 agent_traces 的 detail 里还原，看每轮 system/工具/梗概/原文
    各占多少、丢了多少条。
    """
    limit = max(1, min(limit, 100))
    traces = store.list_traces(limit=100)
    rows = []
    for t in traces:
        if t["conversation_id"] != conversation_id:
            continue
        detail = t.get("detail") or {}
        if "budget" not in detail:
            continue
        rows.append(
            {
                "created_at": t["created_at"],
                "intent": t["intent"],
                "message": t["message"][:50],
                "budget": detail["budget"],
            }
        )
        if len(rows) >= limit:
            break
    return {"conversation_id": conversation_id, "turns": rows}


@router.get("/api/admin/faq/search")
async def admin_debug_search(q: str, top_k: int = 5) -> dict:
    """调试用：查看检索原始分数，绕过阈值过滤。

    录入界面靠它排查「新建后搜不到」到底是没进索引，还是分数不够。
    """
    hits = vector_store.search(q, "", top_k=top_k)
    return {"query": q, "hits": hits}


@router.post("/api/admin/faq")
async def admin_create_faq(payload: FaqPayload) -> dict:
    faq_id = store.create_faq_document(
        title=payload.title,
        section=payload.section,
        category=payload.category,
        content=payload.content,
    )
    _index_faq(faq_id)
    return {"id": faq_id, "item": store.faq_by_id(faq_id)}


@router.put("/api/admin/faq/{faq_id}")
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


@router.post("/api/admin/faq/{faq_id}/toggle")
async def admin_toggle_faq(faq_id: str, enabled: bool) -> dict:
    if not store.set_faq_enabled(faq_id, enabled):
        raise HTTPException(status_code=404, detail="知识不存在")
    if enabled:
        _index_faq(faq_id)
    else:
        vector_store.delete_by_source("faq", faq_id)
    return {"item": store.faq_by_id(faq_id)}


@router.delete("/api/admin/faq/{faq_id}")
async def admin_delete_faq(faq_id: str) -> dict:
    if not store.delete_faq_document(faq_id):
        raise HTTPException(status_code=404, detail="知识不存在")
    vector_store.delete_by_source("faq", faq_id)
    return {"ok": True}


@router.get("/admin")
async def admin_page() -> FileResponse:
    return FileResponse(_STATIC_DIR / "admin.html")


@router.get("/admin/tools")
async def admin_tools_page() -> FileResponse:
    return FileResponse(_STATIC_DIR / "admin_tools.html")


# ---------- 工具注册清单与审计 ----------


@router.get("/api/admin/tools")
async def admin_tools() -> dict:
    """模型能看到的工具清单。参数是给页面看的，不是给模型看的。"""
    return {
        "items": [
            {
                "name": spec.name,
                "label": spec.label,
                "source": spec.source,
                "server": spec.server,
                "scope": spec.scope,
                "risk": spec.risk,
                "requires_confirmation": spec.requires_confirmation,
                "allowed_callers": sorted(spec.allowed_callers),
                "timeout_s": spec.timeout_s,
            }
            for spec in registry.all()
        ]
    }


@router.get("/api/admin/audit")
async def admin_audit(
    tool: str | None = None,
    visitor_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    rows = store.list_tool_audits(
        tool=tool, visitor_id=visitor_id, status=status, limit=limit, offset=offset
    )
    return {"items": rows, "limit": limit, "offset": offset}


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


@router.post("/api/admin/faq/bulk/preview")
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


@router.post("/api/admin/faq/bulk")
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