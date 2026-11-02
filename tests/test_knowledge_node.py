"""r15 Task 5：知识检索节点的验证测试。

节点从 search_knowledge 工具迁入图：意图识别已判定「这是知识类问题」，
不再由模型自主决定查不查。三分支——充分（真检索，skipif 无 API key）、
不相关拒答（mock 检索，不付嵌入往返）、LLM 异常降级（final_text 退化为
拼接资料而不是让对话挂掉）。
"""

import os

import pytest

from app.core import store
from app.retrieval.knowledge_node import KnowledgeOutcome, run_knowledge_retrieval

pytestmark = pytest.mark.skipif(
    not os.getenv("SILICONFLOW_API_KEY", "").strip(),
    reason="未配置 SILICONFLOW_API_KEY",
)


@pytest.fixture(scope="module")
def indexed_faq():
    """把真实 FAQ 灌进临时向量库，用完清理（照 test_retrieval.py 的做法）。"""
    import tempfile

    tmpdir = tempfile.mkdtemp()
    os.environ["VECTOR_DB_PATH"] = os.path.join(tmpdir, "milvus.db")
    from app.retrieval import vector_store

    vector_store.reset_client()

    store.init_db()
    faqs = store.all_faq_for_index()
    assert faqs, "FAQ 表为空，先跑 uv run python -m scripts.import_faq"

    written = vector_store.upsert_documents(
        [vector_store.faq_document(f) for f in faqs]
    )
    assert written == len(faqs), f"只写入 {written}/{len(faqs)} 条"

    yield len(faqs)

    vector_store.reset_client()


class FakeLLM:
    """mock 的 llm 对象：ainvoke 返回给定文本的 AIMessage。"""

    def __init__(self, content: str):
        self._content = content

    async def ainvoke(self, messages):
        from langchain_core.messages import AIMessage

        return AIMessage(content=self._content)


class FakeLLMThrows:
    async def ainvoke(self, messages):
        raise RuntimeError("网络断了")


@pytest.mark.anyio
async def test_sufficient_returns_hits(indexed_faq):
    """充分分支：带引用编号，final_text 含检索词相关内容。"""
    out = await run_knowledge_retrieval(
        "退货运费谁出", "kn-sufficient", llm=FakeLLM("根据规则，退回运费由您承担[P-RET-001-3]")
    )
    assert isinstance(out, KnowledgeOutcome)
    assert not out.insufficient
    assert out.cited_ids, "充分分支应有可引用编号"
    assert out.max_score >= 0.65
    assert "[P-" in out.final_text, "final_text 应含引用编号（r14 格式）"
    assert "运费" in out.final_text


@pytest.mark.anyio
async def test_irrelevant_is_refused(indexed_faq, monkeypatch):
    """不相关问题走拒答分支：insufficient 话术 + 低置信度落库。"""
    calls = []

    def fake_record(visitor_id, query, reason, max_score=None, retrieved=None):
        calls.append((visitor_id, query, reason))

    monkeypatch.setattr(store, "record_low_confidence", fake_record)

    out = await run_knowledge_retrieval("明天几点开会", "kn-refuse", llm=FakeLLMThrows())
    assert out.insufficient
    assert out.cited_ids == []
    assert "没有查到" in out.final_text
    assert "一般来说" not in out.final_text
    assert calls and calls[0][2] in ("empty", "low_score"), calls


@pytest.mark.anyio
async def test_llm_failure_falls_back_to_sources(indexed_faq):
    """LLM 挂了不能让对话挂掉：final_text 降级为资料原文 + 引用编号。"""
    out = await run_knowledge_retrieval(
        "退货运费谁出", "kn-llm-fail", llm=FakeLLMThrows()
    )
    assert not out.insufficient
    assert out.cited_ids
    assert "[P-" in out.final_text


# ---------- r16：query 改写与回退 ----------


@pytest.mark.anyio
async def test_rewrite_boosts_colloquial_query(indexed_faq):
    """口语 query 走改写：物流延迟类从被挡到命中补偿条款。"""
    out = await run_knowledge_retrieval(
        "快递好久没动", "kn-rewrite", llm=None
    )
    assert out.rewritten_terms, "应触发物流延迟词条"
    assert not out.insufficient, f"改写后仍拒答：max_score={out.max_score}"
    assert any("补偿" in r["section"] or "延迟" in r["section"] for r in out.hits)


@pytest.mark.anyio
async def test_fallback_to_original_when_rewrite_insufficient(indexed_faq, monkeypatch):
    """改写后证据不足 → 用原话回退重查，取分高的一组。

    mock 词表强制触发一个「坏」词条，让改写必然发生；再 mock 检索让
    改写 query 分数低于原话，验证回退选中了原话那组。
    """
    from app.retrieval import vector_store

    calls: list[str] = []

    def fake_search(query, visitor_id, top_k=3):
        calls.append(query)
        if "质量问题" in query:
            # 改写后的 query 反而更低分（模拟词表带偏）
            return [{"source_id": "P-RET-001-1", "score": 0.55, "text": "x"}]
        return [{"source_id": "P-RET-001-2", "score": 0.80, "text": "x"}]

    monkeypatch.setattr(vector_store, "search", fake_search)
    monkeypatch.setattr(
        "app.retrieval.knowledge_node.rewrite_query",
        lambda q: (f"{q} 质量问题", ["质量问题"]),
    )

    out = await run_knowledge_retrieval("我那订单", "kn-fallback", llm=None)
    assert out.fallback_used is True
    assert not out.insufficient
    assert out.max_score == 0.80, "应取原话那组（分数更高）"
    assert len(calls) == 2, f"应检索两次（改写+原话），实际 {calls}"


@pytest.mark.anyio
async def test_no_rewrite_for_clean_query(indexed_faq):
    """无词条命中的 query 行为不变：不触发改写也不触发回退。"""
    out = await run_knowledge_retrieval(
        "退货的运费谁出", "kn-clean", llm=None
    )
    assert out.rewritten_terms == []
    assert out.rewritten_query == "退货的运费谁出"
    assert out.fallback_used is False
    assert not out.insufficient
    assert "运费" in out.final_text