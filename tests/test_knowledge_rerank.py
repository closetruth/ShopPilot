"""精排接线测试：开关默认关、开了只改顺序、挂了退回向量分。

这里全部用 mock，不打嵌入也不打精排服务。要守住的就三件事：

1. 默认不开精排，行为与接线前完全一致；
2. 开了之后判定不能变糟——精排挂了仍用向量分过闸，不能把能答的拒掉；
3. 阈值没标定时精排只管排序，不能拿没刻度的尺去拦回答。
"""

import pytest

from app.retrieval import knowledge_node
from app.retrieval.knowledge_node import run_knowledge_retrieval

FAQS = {
    "P-RET-001-3": {
        "id": "P-RET-001-3",
        "doc_id": "P-RET-001",
        "title": "退货运费",
        "category": "退货",
        "section": "承担方",
        "content": "退回运费由顾客承担。",
    },
    "P-SHIP-001-2": {
        "id": "P-SHIP-001-2",
        "doc_id": "P-SHIP-001",
        "title": "物流时效",
        "category": "物流",
        "section": "延迟",
        "content": "物流延迟超过三天可申请补偿。",
    },
    "P-INV-001-1": {
        "id": "P-INV-001-1",
        "doc_id": "P-INV-001",
        "title": "发票开具",
        "category": "发票",
        "section": "时效",
        "content": "发票在确认收货后七个工作日内开具。",
    },
}


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv("RERANK_ENABLED", raising=False)
    monkeypatch.delenv("RERANK_MIN_SCORE", raising=False)


@pytest.fixture
def stubbed(monkeypatch):
    """把检索、精排、取数、落库都换成内存桩，记录调用次数。"""
    state = {"searched": [], "rerank_calls": [], "low_conf": []}

    def fake_search(query, visitor_id, top_k=3):
        state["searched"].append((query, top_k))
        return [
            {"source_id": "P-RET-001-3", "score": 0.82, "text": "x"},
            {"source_id": "P-INV-001-1", "score": 0.60, "text": "x"},
            {"source_id": "P-SHIP-001-2", "score": 0.55, "text": "x"},
        ][:top_k]

    def fake_rerank(query, doc_ids, top_n=10, *, strict=False):
        state["rerank_calls"].append((query, list(doc_ids), top_n, strict))
        return [
            ("P-INV-001-1", 0.95),
            ("P-RET-001-3", 0.70),
            ("P-SHIP-001-2", 0.20),
        ]

    def fake_record(visitor_id, query, reason, max_score=None, retrieved=None):
        state["low_conf"].append((reason, max_score))

    monkeypatch.setattr(knowledge_node.vector_store, "search", fake_search)
    monkeypatch.setattr(knowledge_node.rerank, "rerank", fake_rerank)
    monkeypatch.setattr(knowledge_node.store, "faq_by_id", lambda fid: FAQS.get(fid))
    monkeypatch.setattr(knowledge_node.store, "record_low_confidence", fake_record)
    monkeypatch.setattr(knowledge_node, "rewrite_query", lambda q: (q, []))
    return state


@pytest.mark.anyio
async def test_rerank_off_keeps_old_behaviour(stubbed):
    """默认关：召回 3 条、不调精排、顺序即向量顺序。"""
    out = await run_knowledge_retrieval("退货运费谁出", "kr-off", llm=None)

    assert stubbed["rerank_calls"] == [], "开关关着不该调精排"
    assert stubbed["searched"][0][1] == knowledge_node.RECALL_K
    assert not out.insufficient
    assert out.cited_ids[0] == "P-RET-001-3", "应按向量分排序"


@pytest.mark.anyio
async def test_rerank_on_widens_recall_and_reorders(stubbed, monkeypatch):
    """开：召回放宽到 10 条，顺序改由精排分决定。"""
    monkeypatch.setenv("RERANK_ENABLED", "1")

    out = await run_knowledge_retrieval("退货运费谁出", "kr-on", llm=None)

    assert stubbed["searched"][0][1] == knowledge_node.rerank_recall_k()
    assert len(stubbed["rerank_calls"]) == 1
    assert stubbed["rerank_calls"][0][3] is True, "拿精排分做判断必须走 strict"
    assert out.cited_ids[0] == "P-INV-001-1", "精排分最高的应排第一"


@pytest.mark.anyio
async def test_rerank_unavailable_falls_back_to_vector(stubbed, monkeypatch):
    """精排挂了退回向量分：原来能答的仍要能答，不能变成拒答。"""
    monkeypatch.setenv("RERANK_ENABLED", "1")

    def boom(*a, **kw):
        raise knowledge_node.rerank.RerankUnavailable("连不上")

    monkeypatch.setattr(knowledge_node.rerank, "rerank", boom)

    out = await run_knowledge_retrieval("退货运费谁出", "kr-down", llm=None)

    assert not out.insufficient
    assert out.max_score == 0.82, "应退回向量分"
    assert out.cited_ids[0] == "P-RET-001-3", "应退回向量顺序"


@pytest.mark.anyio
async def test_uncalibrated_gate_still_uses_vector_score(stubbed, monkeypatch):
    """开了精排但阈值没标定：排序听精排，闸仍用向量分。

    向量分 0.82 足够作答，即便精排分只有 0.95（不同量纲）也不能误判；
    反过来若拿向量阈值 0.65 去比精排分，就会把一个相关条款拒掉。
    """
    monkeypatch.setenv("RERANK_ENABLED", "1")

    out = await run_knowledge_retrieval("退货运费谁出", "kr-nocal", llm=None)

    assert not out.insufficient
    assert out.max_score == 0.82, "未标定时 outcome 分数仍是向量分"


@pytest.mark.anyio
async def test_calibrated_gate_uses_rerank_score(stubbed, monkeypatch):
    """标定后闸改用精排分：精排分全部低于阈值就该拒答。"""
    monkeypatch.setenv("RERANK_ENABLED", "1")
    monkeypatch.setenv("RERANK_MIN_SCORE", "0.98")

    out = await run_knowledge_retrieval("退货运费谁出", "kr-cal", llm=None)

    assert out.insufficient
    assert out.max_score == 0.95
    assert stubbed["low_conf"] and stubbed["low_conf"][0][0] == "low_score"


@pytest.mark.anyio
async def test_rerank_keeps_vector_score_for_filtering(stubbed, monkeypatch):
    """精排把低向量分的条款排到前面时，仍按向量分决定是否引用。

    P-SHIP-001-2 向量分 0.55 低于 0.58，即便精排给了 0.20 也不能被引用——
    RELEVANCE_THRESHOLD 是在向量分上标定的，混用会筛错。
    """
    monkeypatch.setenv("RERANK_ENABLED", "1")

    out = await run_knowledge_retrieval("退货运费谁出", "kr-filter", llm=None)

    assert "P-SHIP-001-2" not in out.cited_ids


@pytest.mark.anyio
async def test_invalid_min_score_treated_as_uncalibrated(stubbed, monkeypatch):
    """阈值填了非数字：按未标定处理，不因配置笔误把问题全拒掉。"""
    monkeypatch.setenv("RERANK_ENABLED", "1")
    monkeypatch.setenv("RERANK_MIN_SCORE", "0.6x")

    out = await run_knowledge_retrieval("退货运费谁出", "kr-bad", llm=None)

    assert knowledge_node.rerank_min_score() is None
    assert not out.insufficient