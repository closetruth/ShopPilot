"""知识检索节点：意图识别判定知识类后，在这里完成「查 → 闸 → 生成回答」。

从 search_knowledge 工具迁入。区别在于：工具模式下模型自主决定查不查、
自己组织回答；节点模式下这个决定已由意图识别做出，检索结果直接生成
final_text 返回给调用方。

证据不足时（INSUFFICIENT_THRESHOLD）走 r14 同款拒答：不返回任何条目内容，
只给拒答话术，并落库 low_confidence_queries 供数据飞轮分析。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app import store, vector_store

logger = logging.getLogger(__name__)

# 实测 47 条 FAQ 上的分布：相关政策问题最低 0.630，无关问题最高 0.521。
# 取 0.58 落在两者中点，两侧各留约 0.05 余量。阈值偏低会让「明天几点开会」
# 命中「发票开具时效」这类噪声，偏高则漏掉「要投诉你们的服务」这类短问句。
# 改动嵌入模型或重建索引后需重新标定，tests/test_retrieval.py 会守住边界。
RELEVANCE_THRESHOLD = 0.58

# 证据充分性下限：低于此分不足以支撑回答，必须拒答。
# 比检索阈值高——0.58 只保证「有点相关」，0.65 才接近「足以作答」。
INSUFFICIENT_THRESHOLD = 0.65

# 拒答话术直接给到顾客，不经过 LLM——理由与 _insufficient 的设计相同：
# 不留任何可以被模型补编的空间。
REFUSAL_TEMPLATE = "这个问题我这边没有查到相关规定，建议以商品详情页为准，或为您转接人工客服。"


@dataclass(frozen=True)
class KnowledgeOutcome:
    """一次知识检索的完整结果。

    final_text 是直接可发给顾客的文本；insufficient 为 true 时它就是
    拒答话术，此时 hits / cited_ids 恒为空。
    """

    final_text: str
    hits: list[dict] = field(default_factory=list)
    insufficient: bool = False
    max_score: float = 0.0
    cited_ids: list[str] = field(default_factory=list)


# 生成 final_text 的一次轻量调用：输入命中资料 + r14 的引用与拒答规则。
ANSWER_PROMPT = """你是店铺客服。根据下面的知识资料回答顾客问题。

资料：
{sources}

顾客问题：{query}

规则：
- 只依据资料回答，每个结论后标注来源编号，格式如 [P-RET-001-3]
- 只能引用资料方括号里出现的编号，不得编造
- 三句话以内，直接回答
- 资料回答不了问题时，只输出：这个问题我这边没有查到相关规定。"""


async def run_knowledge_retrieval(
    resolved_query: str, visitor_id: str, *, llm=None
) -> KnowledgeOutcome:
    """检索 → 置信度闸 → 生成 final_text。

    llm 为 None 时不调模型，final_text 直接由资料原文拼接（图组装时传入
    主 LLM；测试或降级场景可省略）。
    """
    top_k = 3
    # 嵌入是同步 HTTP 调用；检索与原文组装是纯同步逻辑，包一层避免阻塞事件循环
    import anyio

    hits = await anyio.to_thread.run_sync(
        lambda: vector_store.search(resolved_query, "", top_k=top_k)
    )
    hits = sorted(hits, key=lambda h: h["score"], reverse=True)[:top_k]

    max_score = hits[0]["score"] if hits else 0.0

    if not hits:
        _record_low_confidence(visitor_id, resolved_query, "empty", None, [])
        return KnowledgeOutcome(
            final_text=REFUSAL_TEMPLATE, insufficient=True, max_score=0.0
        )

    if max_score < INSUFFICIENT_THRESHOLD:
        _record_low_confidence(
            visitor_id, resolved_query, "low_score", max_score,
            [h["source_id"] for h in hits],
        )
        return KnowledgeOutcome(
            final_text=REFUSAL_TEMPLATE, insufficient=True, max_score=max_score
        )

    # 内容一律取自 MySQL 原文：向量库里存的是为检索拼接、可能被截断的文本
    results = []
    for hit in hits:
        if hit["score"] < RELEVANCE_THRESHOLD:
            continue
        row = store.faq_by_id(hit["source_id"])
        if row is None:
            continue
        results.append(
            {
                "doc_id": row["doc_id"],
                "faq_id": row["id"],
                "title": row["title"],
                "category": row["category"],
                "section": row["section"],
                "content": row["content"],
                "score": round(hit["score"], 4),
            }
        )

    if not results:
        _record_low_confidence(
            visitor_id, resolved_query, "empty", max_score,
            [h["source_id"] for h in hits],
        )
        return KnowledgeOutcome(
            final_text=REFUSAL_TEMPLATE, insufficient=True, max_score=max_score
        )

    final_text = await _compose_answer(resolved_query, results, llm)
    return KnowledgeOutcome(
        final_text=final_text,
        hits=results,
        insufficient=False,
        max_score=max_score,
        cited_ids=[r["faq_id"] for r in results],
    )


async def _compose_answer(query: str, results: list[dict], llm) -> str:
    """充分分支的 final_text：一次轻量 LLM 调用组织回答。

    LLM 故障时降级为资料原文拼接（带编号），宁可话术生硬不能让对话挂掉——
    照 app/intent.py 的降级风格。
    """
    sources = "\n\n".join(
        f"[{r['faq_id']}] {r['title']}：{r['content']}" for r in results
    )
    if llm is None:
        return _fallback_text(results)
    try:
        response = await llm.ainvoke(
            ANSWER_PROMPT.format(sources=sources, query=query)
        )
        content = response.content
        text = content if isinstance(content, str) else str(content)
        if not text.strip():
            return _fallback_text(results)
        return text.strip()
    except Exception as exc:  # noqa: BLE001 - 模型故障降级，不断对话
        logger.warning("知识回答生成失败，降级为资料拼接：%s", exc)
        return _fallback_text(results)


def _fallback_text(results: list[dict]) -> str:
    """资料原文逐条拼接。生硬但保真：内容与编号都来自数据库原文。

    编号用 faq_id（如 P-RET-001-3）——前端 CITE_RE 与 /api/faq/{id}
    都按这个键解析，悬停才能定位到条款。
    """
    return "\n\n".join(f"{r['content']}[{r['faq_id']}]" for r in results)


def _record_low_confidence(
    visitor_id: str,
    query: str,
    reason: str,
    max_score: float | None,
    retrieved: list[str],
) -> None:
    """记录低置信度问题。失败只记日志，不影响顾客拿到回答。"""
    try:
        store.record_low_confidence(visitor_id, query, reason, max_score, retrieved)
    except Exception:  # noqa: BLE001
        logger.warning("低置信度记录失败，不影响对话", exc_info=True)
