"""向量层测试。

嵌入走真实 API、Milvus 用临时文件库。需要网络与 SILICONFLOW_API_KEY，
缺失时整体跳过。

向量索引只存 FAQ：商品与工单按结构化条件查询，不进向量库。
"""

import os
import uuid

import pytest

from app import vector_store
from app.embedding import EmbeddingError, embed_texts


def _has_embedding_config() -> bool:
    return bool(os.getenv("SILICONFLOW_API_KEY", "").strip())


requires_embedding = pytest.mark.skipif(
    not _has_embedding_config(), reason="未配置 SILICONFLOW_API_KEY"
)


@pytest.fixture
def temp_vector_db(tmp_path, monkeypatch):
    monkeypatch.setenv("VECTOR_DB_PATH", str(tmp_path / "milvus.db"))
    vector_store.reset_client()
    yield
    vector_store.reset_client()


def _faq_doc(faq_id: str, title: str, section: str, content: str) -> dict:
    return vector_store.faq_document(
        {"id": faq_id, "title": title, "section": section, "content": content}
    )


@requires_embedding
def test_embed_returns_consistent_dimension():
    vectors = embed_texts(["杯子", "保温杯"])
    assert len(vectors) == 2
    assert all(len(v) == 1024 for v in vectors)


@requires_embedding
def test_embed_preserves_order():
    """批量返回的顺序必须与入参一致。

    嵌入服务对同一文本的批量与单条调用可能有浮点级差异，因此比较
    余弦相似度而非逐位相等。
    """
    texts = ["完全不同的第一条文本甲", "第二条文本乙", "第三条文本丙"]
    batch = embed_texts(texts)
    singles = [embed_texts([t])[0] for t in texts]

    def cosine(a, b):
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(y * y for y in b) ** 0.5
        return dot / (na * nb)

    for i in range(len(texts)):
        assert cosine(batch[i], singles[i]) > 0.999, f"第 {i} 条顺序错位"

    assert cosine(batch[0], singles[1]) < cosine(batch[1], singles[1])


def test_embed_empty_list():
    assert embed_texts([]) == []


@requires_embedding
def test_embed_missing_key_raises(monkeypatch):
    monkeypatch.setenv("SILICONFLOW_API_KEY", "")
    from app import embedding

    embedding._client.cache_clear()
    try:
        with pytest.raises(EmbeddingError):
            embed_texts(["测试"])
    finally:
        embedding._client.cache_clear()


def test_faq_document_embeds_title_and_section():
    doc = _faq_doc("P-RET-001-3", "七天无理由退货规则", "运费承担", "退回运费由消费者承担")
    assert doc["id"] == "faq:P-RET-001-3"
    assert doc["source"] == "faq"
    assert doc["visitor_id"] == ""
    assert "七天无理由退货规则" in doc["text"]
    assert "运费承担" in doc["text"]
    assert "退回运费由消费者承担" in doc["text"]


@requires_embedding
def test_upsert_and_search_roundtrip(temp_vector_db):
    docs = [
        _faq_doc("P-RET-001-3", "七天无理由退货规则", "运费承担", "退回运费由消费者承担"),
        _faq_doc("P-INV-001-1", "发票开具与换开规则", "开具条件", "订单完成后可申请电子发票"),
    ]
    assert vector_store.upsert_documents(docs) == 2

    hits = vector_store.search("退货运费谁出", "", top_k=5)
    assert hits
    assert any(h["source_id"] == "P-RET-001-3" for h in hits)


@requires_embedding
def test_search_returns_highest_score_first(temp_vector_db):
    vector_store.upsert_documents(
        [
            _faq_doc("P-LOG-001-2", "物流延迟处理", "补偿标准", "物流停滞超过48小时发放10元券"),
            _faq_doc("P-INV-001-1", "发票开具规则", "开具条件", "订单完成后可申请电子发票"),
        ]
    )
    hits = vector_store.search("快递不走了有补偿吗", "", top_k=5)
    assert hits
    scores = [h["score"] for h in hits]
    assert scores == sorted(scores, reverse=True), "结果须按相似度降序"


@requires_embedding
def test_delete_by_source(temp_vector_db):
    vector_store.upsert_documents(
        [_faq_doc("P-DEL-1", "待删除测试", "正文", "这是一条将被删除的测试知识")]
    )
    assert vector_store.delete_by_source("faq", "P-DEL-1")
    hits = vector_store.search("待删除测试", "", top_k=5)
    assert all(h["source_id"] != "P-DEL-1" for h in hits)


def test_upsert_without_key_does_not_raise(monkeypatch, temp_vector_db):
    """写入失败不应向调用方抛出——MySQL 是事实来源，索引可重建。"""
    monkeypatch.setenv("SILICONFLOW_API_KEY", "")
    from app import embedding

    embedding._client.cache_clear()
    try:
        written = vector_store.upsert_documents(
            [_faq_doc("P-NOKEY", "无密钥测试", "正文", "测试")]
        )
        assert written == 0
    finally:
        embedding._client.cache_clear()
