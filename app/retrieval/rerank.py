"""精排：交叉编码器为 (query, doc) 对打分。

默认用硅基流动托管的 bge-reranker-v2-m3，与嵌入同源，省去本地部署。
微调后的自建服务通过 `RERANK_BASE_URL` 指过去即可，协议完全一致，因此
这里**不做分支**——多一条 if 就多一处只在某个环境才走到的代码。
失败时降级为原顺序：精排只是锦上添花，不应让整个检索挂掉。
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache

import httpx

from app.core import store

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.siliconflow.cn/v1"
DEFAULT_MODEL = "BAAI/bge-reranker-v2-m3"
BATCH_DOCS = 50


def base_url() -> str:
    """精排服务根地址，含版本段，与 `EMBED_BASE_URL` 保持同一写法。

    留空走硅基流动；填 `http://192.168.6.23:8010` 这种不带 `/v1` 的也能用，
    少一个斜杠就报 404 的坑不值得留给人踩。
    """
    raw = (os.getenv("RERANK_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/")
    if not raw.endswith("/v1"):
        raw += "/v1"
    return raw


def endpoint() -> str:
    return base_url() + "/rerank"


@lru_cache(maxsize=1)
def _api_key() -> str:
    key = os.getenv("SILICONFLOW_API_KEY", "").strip()
    if not key and base_url() == DEFAULT_BASE_URL:
        logger.warning("缺少 SILICONFLOW_API_KEY，精排将降级为不重排")
    return key


def _doc_text(row: dict) -> str:
    """送进精排的文档文本。与向量侧保持一致。"""
    return f"{row['title']} {row['section']} {row['content']}"


class RerankUnavailable(Exception):
    """精排不可用：缺密钥、服务连不上、返回体异常。

    只有 `strict=True` 时抛出。默认降级不抛——但降级返回的是「原顺序配
    0 分」，调用方若拿它当分数去过阈值闸，就会把本来能答的问题全拒掉。
    所以凡是用精排分做判断的地方，都必须走 strict。
    """


def rerank(
    query: str, doc_ids: list[str], top_n: int = 10, *, strict: bool = False
) -> list[tuple[str, float]]:
    """对候选重排，返回 [(doc_id, score), ...]，按分数降序。

    strict=False（默认）失败时返回原顺序配 0 分，调用方仍能拿到结果，只是
    没有重排；strict=True 失败抛 `RerankUnavailable`，由调用方自己决定
    退回哪条路径。区别在于「分数是否被当作判断依据」：纯展示排序可以降级，
    阈值闸不行。
    """
    if not doc_ids:
        return []

    key = _api_key()
    # 自建服务不需要密钥。只有走硅基流动时才必须要有 key，否则请求必然 401。
    if not key and base_url() == DEFAULT_BASE_URL:
        if strict:
            raise RerankUnavailable("缺少 SILICONFLOW_API_KEY")
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

    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        resp = httpx.post(
            endpoint(),
            headers=headers,
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
        logger.warning("精排失败：%s", exc)
        if strict:
            raise RerankUnavailable(str(exc)) from exc
        return [(d, 0.0) for d in doc_ids[:top_n]]

    results = payload.get("results") or []
    return [
        (ids[item["index"]], float(item.get("relevance_score", 0.0)))
        for item in sorted(results, key=lambda x: -x.get("relevance_score", 0.0))
        if 0 <= item.get("index", -1) < len(ids)
    ]