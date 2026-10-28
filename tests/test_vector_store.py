"""向量层测试。

嵌入走真实 API、Milvus 用临时文件库。这些测试需要网络与
SILICONFLOW_API_KEY，缺失时整体跳过。
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


@requires_embedding
def test_embed_returns_consistent_dimension():
    vectors = embed_texts(["杯子", "保温杯"])
    assert len(vectors) == 2
    assert all(len(v) == 1024 for v in vectors)


@requires_embedding
def test_embed_preserves_order():
    """批量返回的顺序必须与入参一致。

    嵌入服务对同一文本的批量与单条调用可能存在浮点级差异，因此比较
    余弦相似度而非逐位相等——只要对应项高度相似即说明顺序没错。
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

    # 交叉验证：错位的组合相似度应明显更低
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


def test_product_document_is_public():
    doc = vector_store.product_document(
        {
            "id": "p-mug-blue",
            "name": "青花瓷马克杯",
            "category": "杯具",
            "color": "蓝色",
            "description": "手工青花釉下彩",
        }
    )
    assert doc["id"] == "product:p-mug-blue"
    assert doc["visitor_id"] == ""
    assert "青花瓷马克杯" in doc["text"]
    assert "蓝色" in doc["text"]


def test_work_order_document_is_private():
    doc = vector_store.work_order_document(
        {
            "work_order_no": "WO123",
            "visitor_id": "visitor-a",
            "issue_type": "运输破损",
            "description": "杯口有裂纹",
        }
    )
    assert doc["id"] == "work_order:WO123"
    assert doc["visitor_id"] == "visitor-a"
    assert "运输破损" in doc["text"]


@requires_embedding
def test_upsert_and_search_roundtrip(temp_vector_db):
    visitor = "v-" + uuid.uuid4().hex[:8]
    docs = [
        vector_store.product_document(
            {
                "id": "p-test-1",
                "name": "测试蓝色马克杯",
                "category": "杯具",
                "color": "蓝色",
                "description": "用于向量测试的杯子",
            }
        ),
        vector_store.work_order_document(
            {
                "work_order_no": "WO-TEST-1",
                "visitor_id": visitor,
                "issue_type": "运输破损",
                "description": "收到时杯口有裂纹",
            }
        ),
    ]
    assert vector_store.upsert_documents(docs) == 2

    hits = vector_store.search("杯子裂了", visitor, top_k=5)
    assert hits
    assert any(h["source"] == "work_order" for h in hits)


@requires_embedding
def test_search_is_isolated_between_visitors(temp_vector_db):
    owner = "owner-" + uuid.uuid4().hex[:8]
    stranger = "stranger-" + uuid.uuid4().hex[:8]

    vector_store.upsert_documents(
        [
            vector_store.work_order_document(
                {
                    "work_order_no": "WO-SECRET-1",
                    "visitor_id": owner,
                    "issue_type": "运输破损",
                    "description": "这位顾客的特殊破损情况",
                }
            )
        ]
    )

    own_hits = vector_store.search("破损", owner, top_k=5)
    assert any(h["source_id"] == "WO-SECRET-1" for h in own_hits)

    other_hits = vector_store.search("破损", stranger, top_k=5)
    assert all(h["source_id"] != "WO-SECRET-1" for h in other_hits)


@requires_embedding
def test_delete_by_source(temp_vector_db):
    visitor = "v-" + uuid.uuid4().hex[:8]
    vector_store.upsert_documents(
        [
            vector_store.product_document(
                {
                    "id": "p-del-1",
                    "name": "待删除测试商品",
                    "category": "杯具",
                    "color": "白色",
                    "description": "删除测试",
                }
            )
        ]
    )
    assert vector_store.delete_by_source("product", "p-del-1")
    hits = vector_store.search("待删除测试商品", visitor, top_k=5)
    assert all(h["source_id"] != "p-del-1" for h in hits)


def test_upsert_without_key_does_not_raise(monkeypatch, temp_vector_db):
    """双写失败不应向调用方抛出——MySQL 已写入，索引可重建。"""
    monkeypatch.setenv("SILICONFLOW_API_KEY", "")
    from app import embedding

    embedding._client.cache_clear()
    try:
        written = vector_store.upsert_documents(
            [
                vector_store.product_document(
                    {
                        "id": "p-nokey",
                        "name": "无密钥测试",
                        "category": "杯具",
                        "color": "白色",
                        "description": "测试",
                    }
                )
            ]
        )
        assert written == 0
    finally:
        embedding._client.cache_clear()
