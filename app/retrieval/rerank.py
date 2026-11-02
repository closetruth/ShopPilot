"""精排：交叉编码器为 (query, doc) 对打分。

用硅基流动托管的 bge-reranker-v2-m3。与嵌入同源，省去本地部署。
失败时降级为原顺序——精排只是锦上添花，不应让整个检索挂掉。
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache

import httpx

from app.core import store

logger = logging.getLogger(__name__)

RERANK_URL = "https://api.siliconflow.cn/v1/rerank"
DEFAULT_MODEL = "BAAI/bge-reranker-v2-m3"
BATCH_DOCS = 50


@lru_cache(maxsize=1)
def _api_key() -> str:
    key = os.getenv("SILICONFLOW_API_KEY", "").strip()
    if not key:
        logger.warning("缺少 SILICONFLOW_API_KEY，精排将降级为不重排")
    return key


def _doc_text(row: dict) -> str:
    """送进精排的文档文本。与向量侧保持一致。"""
    return f"{row['title']} {row['section']} {row['content']}"


def rerank(
    query: str, doc_ids: list[str], top_n: int = 10
) -> list[tuple[str, float]]:
    """对候选重排，返回 [(doc_id, score), ...]，按分数降序。

    失败时返回原顺序配 0 分——调用方据此仍能拿到结果，只是没有重排。
    """
    if not doc_ids:
        return []

    key = _api_key()
    if not key:
        return [(d, 0.0) for d in doc_ids[:top_n]]

    rows = {r["id"]: r for r in store.all_faq_for_index()}
    docs, ids = [], []
    for doc_id in doc_ids:
        row = rows.get(doc_id)
        if row is not None:
            ids.append(doc_id)
            docs.append(_doc_text(row))

    if not docs:
        return []

    try:
        resp = httpx.post(
            RERANK_URL,
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": os.getenv("RERANK_MODEL", DEFAULT_MODEL),
                "query": query,
                "documents": docs,
                "top_n": min(top_n, len(docs)),
            },
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001 - 降级，不让精排失败拖垮检索
        logger.warning("精排失败，降级为原顺序：%s", exc)
        return [(d, 0.0) for d in doc_ids[:top_n]]

    results = payload.get("results") or []
    return [
        (ids[item["index"]], float(item.get("relevance_score", 0.0)))
        for item in sorted(results, key=lambda x: -x.get("relevance_score", 0.0))
        if 0 <= item.get("index", -1) < len(ids)
    ]