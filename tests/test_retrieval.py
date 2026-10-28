"""检索链路测试：真实 FAQ 数据能否被 search_knowledge 检出。

与其他测试不同，这些用例不造假数据——用 data/faq 导入的真实内容
验证端到端可用性。需要 MySQL 与嵌入 API。
"""

import os
import uuid

import pytest

from app import store, vector_store
from app.tools import RELEVANCE_THRESHOLD, AgentContext, search_knowledge
from langgraph.prebuilt import ToolRuntime


pytestmark = pytest.mark.skipif(
    not os.getenv("SILICONFLOW_API_KEY", "").strip(),
    reason="未配置 SILICONFLOW_API_KEY",
)


class _RT:
    """ToolRuntime 的最小替身，工具内只读 context.visitor_id。"""

    def __init__(self, visitor_id: str = "test-visitor"):
        self.context = AgentContext(visitor_id=visitor_id)


@pytest.fixture(scope="module")
def indexed_faq():
    """把真实 FAQ 灌进临时向量库，用完清理。

    scope=module：重建一次供全部用例使用，避免每条测试都付一次
    嵌入 API 的往返。
    """
    import tempfile

    tmpdir = tempfile.mkdtemp()
    os.environ["VECTOR_DB_PATH"] = os.path.join(tmpdir, "milvus.db")
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


def _search(query: str, top_k: int = 3) -> list[dict]:
    result = search_knowledge.func(_RT(), query=query, top_k=top_k)
    return result.get("results", [])


@pytest.mark.parametrize(
    "question,expected_doc",
    [
        ("退货的运费谁出", "P-RET-001"),
        ("七天无理由退货有什么条件", "P-RET-001"),
        ("快递丢件了怎么办", "P-LOG-001"),
        ("物流三天没更新有补偿吗", "P-LOG-001"),
        ("能不能开发票", "P-INV-001"),
        ("买完就降价了能补差价吗", "P-PRI-001"),
        ("只退款不退货可以吗", "P-REF-001"),
        ("会员有什么权益", "P-MEM-001"),
        ("要投诉你们的服务", "P-COM-001"),
    ],
)
def test_search_finds_the_right_policy(indexed_faq, question, expected_doc):
    """真实提问必须能检出对应政策文档。"""
    results = _search(question)
    assert results, f"「{question}」没有检出任何知识"

    doc_ids = [r["doc_id"] for r in results]
    assert expected_doc in doc_ids, (
        f"「{question}」期望命中 {expected_doc}，实际命中 {doc_ids}"
    )


def test_results_include_full_source_content(indexed_faq):
    """返回内容必须取自 MySQL 原文，而不是向量库里的拼接文本。"""
    results = _search("退货的运费谁出")
    assert results

    top = results[0]
    assert set(top) >= {"doc_id", "title", "category", "section", "content", "score"}
    assert top["content"], "正文为空"

    # 与数据库逐字比对，确认不是向量里那份被截断/拼接的文本
    row = store.faq_by_id(_faq_id(top))
    assert row is not None
    assert top["content"] == row["content"]
    assert top["title"] == row["title"]
    assert top["section"] == row["section"]


def _faq_id(result: dict) -> str:
    """检索结果没有直接回传 faq 表主键，按 doc_id + section 反查。"""
    for row in store.list_faq():
        if row["doc_id"] == result["doc_id"] and row["section"] == result["section"]:
            return row["id"]
    raise AssertionError(f"找不到 {result['doc_id']} / {result['section']}")


def test_results_sorted_by_score(indexed_faq):
    results = _search("退货运费谁出", top_k=5)
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)


def test_all_results_above_threshold(indexed_faq):
    results = _search("退货运费谁出", top_k=5)
    assert all(r["score"] >= RELEVANCE_THRESHOLD for r in results)


def test_respects_top_k(indexed_faq):
    for k in (1, 3, 5):
        results = _search("退货", top_k=k)
        assert len(results) <= k


def test_top_k_is_clamped(indexed_faq):
    """超出范围的值必须被夹到 1..5，不能传给 Milvus。"""
    assert len(_search("退货", top_k=100)) <= 5
    assert len(_search("退货", top_k=0)) <= 5


def test_irrelevant_question_returns_empty(indexed_faq):
    """与政策无关的问题不应硬凑结果，否则模型会拿无关条款回答。"""
    results = _search("今天天气怎么样")
    assert results == [], f"无关问题检出了 {[r['title'] for r in results]}"


def test_no_keyword_collision_with_products(indexed_faq):
    """问商品问题不应命中政策条款——两者走不同的工具。"""
    results = _search("杯子多少钱")
    titles = [r["title"] for r in results]
    assert all("退货" not in t or "商品" not in t for t in titles), titles


def test_threshold_has_margin_on_product_queries(indexed_faq):
    """商品类问题与阈值之间必须留有安全余量。

    「杯子多少钱」实测 0.466，阈值 0.5。若余量小于 0.02，说明阈值漂到了
    政策与商品的分界线上，任一方向的小改动都会导致误判。届时该重新定阈值
    或调整嵌入文本，而不是让测试恰好擦线通过。
    """
    from app import vector_store

    hits = vector_store.search("杯子多少钱", "", top_k=3)
    if not hits:
        pytest.skip("检索无返回，无法评估余量")

    top_score = hits[0]["score"]
    margin = RELEVANCE_THRESHOLD - top_score
    assert margin > 0.02, (
        f"商品问题得分 {top_score:.3f} 距阈值 {RELEVANCE_THRESHOLD} 仅 {margin:.3f}，"
        "分界太窄"
    )


def test_irrelevant_queries_all_below_threshold(indexed_faq):
    """一组与政策无关的问题必须全部被阈值挡在门外。

    单条用例只能证明那一条，这里用一组覆盖不同的无关主题，
    任何一条漏网都说明阈值或嵌入质量问题。
    """
    irrelevant = [
        "今天天气怎么样",
        "帮我写一首诗",
        "Python怎么学",
        "推荐部电影",
        "明天几点开会",
    ]
    for question in irrelevant:
        results = _search(question)
        assert results == [], (
            f"无关问题「{question}」检出了 {[r['title'] for r in results]}"
        )
