"""混合检索：向量 + BM25 经 RRF 融合，可选精排。

RRF（Reciprocal Rank Fusion）只用排名不用分数，避免了「向量分数是余弦
相似度、BM25 分数无上界」这两个量纲无法直接加权的问题。k=60 是原论文
的默认值，在 47 条语料上不值得调参——那样只是在拟合这一份数据。
"""

from __future__ import annotations

import logging

from app.core import store
from app.retrieval import bm25_index, vector_store
from app.retrieval.rerank import rerank

logger = logging.getLogger(__name__)

RRF_K = 60
CANDIDATES = 30
DEFAULT_TOP_K = 10
RELEVANCE_THRESHOLD = 0.58

STRATEGIES = ("vector", "bm25", "hybrid", "hybrid_rerank")


def rrf_fuse(
    ranked_lists: list[list[str]], k: int = RRF_K
) -> list[tuple[str, float]]:
    """把多路排名融合成一个。每路贡献 1/(k + rank)。

    用排名而非分数，天然免疫量纲差异。
    """
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, doc_id in enumerate(ranked, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda x: -x[1])


def _hydrate(doc_ids: list[str], scores: dict[str, float]) -> list[dict]:
    """把 doc_id 还原成完整条目，内容一律取自 MySQL 原文。"""
    results = []
    for doc_id in doc_ids:
        row = store.faq_by_id(doc_id)
        if row is None:
            continue
        results.append(
            {
                "id": doc_id,
                "doc_id": row["doc_id"],
                "title": row["title"],
                "category": row["category"],
                "section": row["section"],
                "content": row["content"],
                "score": round(scores.get(doc_id, 0.0), 4),
            }
        )
    return results


def _vector_hits(query: str, limit: int) -> list[dict]:
    """向量检索结果补齐成统一结构。

    vector_store.search 返回的是 {source, source_id, text, score}，
    这里回表换成与其他策略一致的 {id, doc_id, title, ...}。
    """
    raw = vector_store.search(query, "", top_k=limit)
    rows = {r["id"]: r for r in store.all_faq_for_index()}
    out = []
    for hit in sorted(raw, key=lambda h: -h["score"]):
        row = rows.get(hit["source_id"])
        if row is None:
            continue
        out.append(
            {
                "id": hit["source_id"],
                "doc_id": row["doc_id"],
                "title": row["title"],
                "category": row["category"],
                "section": row["section"],
                "content": row["content"],
                "score": round(hit["score"], 4),
            }
        )
    return out


def _bm25_hits(query: str, limit: int) -> list[dict]:
    index = bm25_index.get_index()
    raw = index.search(query, top_k=limit)
    rows = {r["id"]: r for r in store.all_faq_for_index()}
    out = []
    for hit in raw:
        row = rows.get(hit["id"])
        if row is None:
            continue
        out.append(
            {
                "id": hit["id"],
                "doc_id": row["doc_id"],
                "title": row["title"],
                "category": row["category"],
                "section": row["section"],
                "content": row["content"],
                "score": round(hit["normalized"], 4),
                "raw_score": round(hit["score"], 4),
            }
        )
    return out


def retrieve(
    query: str,
    strategy: str = "hybrid_rerank",
    top_k: int = DEFAULT_TOP_K,
    rows: list[dict] | None = None,
) -> list[dict]:
    """统一检索入口。四种策略输出结构一致，便于评估对比。"""
    if strategy not in STRATEGIES:
        raise ValueError(f"未知策略 {strategy}，可选 {STRATEGIES}")

    if strategy == "vector":
        hits = _vector_hits(query, top_k)
        return [h for h in hits if h["score"] >= RELEVANCE_THRESHOLD][:top_k]

    if strategy == "bm25":
        hits = _bm25_hits(query, top_k)
        # BM25 无绝对阈值，取归一化后 0.05 以上，滤掉只有极弱词重叠的
        return [h for h in hits if h["score"] >= 0.05][:top_k]

    vector_hits = _vector_hits(query, CANDIDATES)
    bm25_hits = _bm25_hits(query, CANDIDATES)
    fused = rrf_fuse(
        [[h["id"] for h in vector_hits], [h["id"] for h in bm25_hits]]
    )

    if strategy == "hybrid":
        docs = _hydrate([d for d, _ in fused[:top_k]], dict(fused))
        return docs

    # hybrid_rerank：先取融合后的前 30 条，精排后取 top_k
    candidates = [d for d, _ in fused[:CANDIDATES]]
    reranked = rerank(query, candidates, top_n=top_k)

    max_fused = fused[0][1] if fused else 1.0
    scores = {d: s / max_fused for d, s in fused}
    docs = _hydrate([d for d, _ in reranked], scores)

    # 精排分数写回，作为最终相关度
    by_id = dict(reranked)
    for doc in docs:
        doc["score"] = round(by_id.get(doc["id"], 0.0), 4)

    return docs


def render_context(hits: list[dict]) -> str:
    """把命中排成给模型看的文本。

    模型对开头和结尾的注意力最强，中段最弱。分两段而非机械交错：
    严格交错（1,2,7,6,5,4,3,10,9,8）会让编号跳跃，模型读起来认知负担大，
    实测收益未必盖过损失。前三条放「高相关」，其余放「补充」。
    """
    if not hits:
        return ""

    head = hits[:3]
    rest = hits[3:]

    def fmt(offset: int, items: list[dict]) -> str:
        lines = []
        for i, h in enumerate(items, start=offset):
            lines.append(
                f"{i}. [{h['doc_id']}] {h['title']} / {h['section']}\n"
                f"   {h['content']}"
            )
        return "\n\n".join(lines)

    parts = ["【高相关资料】优先参考以下内容", fmt(1, head)]
    if rest:
        parts.append("")
        parts.append("【补充资料】仅在上述内容不足时参考")
        parts.append(fmt(len(head) + 1, rest))
    return "\n".join(parts)