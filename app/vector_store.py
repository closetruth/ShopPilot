"""向量索引：Milvus Lite（本地文件，进程内运行）。

Milvus Lite 对数据文件加独占锁，同一时刻只能有一个进程打开。因此：
- 服务运行时，重建须走 POST /api/admin/reindex（在服务进程内执行）
- 或先停服务再跑 scripts/reindex.py

Milvus 只是可重建的检索副本，MySQL 才是事实来源。写入失败不阻断业务。
"""

from __future__ import annotations

import logging
import os

from pymilvus import DataType, MilvusClient

from app.embedding import EMBED_DIM, EmbeddingError, embed_texts

logger = logging.getLogger(__name__)

COLLECTION = "shoppilot_knowledge"
DEFAULT_URI = "data/milvus.db"
TEXT_MAX = 2048

_client: MilvusClient | None = None


def milvus_uri() -> str:
    return os.getenv("VECTOR_DB_PATH", DEFAULT_URI)


def get_client() -> MilvusClient:
    global _client
    if _client is None:
        uri = milvus_uri()
        parent = os.path.dirname(uri)
        if parent:
            os.makedirs(parent, exist_ok=True)
        _client = MilvusClient(uri)
        _ensure_collection(_client)
    return _client


def reset_client() -> None:
    """测试用：丢弃缓存连接。"""
    global _client
    if _client is not None:
        try:
            _client.close()
        except Exception:  # noqa: BLE001
            pass
    _client = None


def _ensure_collection(client: MilvusClient) -> None:
    if client.has_collection(COLLECTION):
        return

    schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=64)
    schema.add_field("source", DataType.VARCHAR, max_length=16)
    schema.add_field("source_id", DataType.VARCHAR, max_length=64)
    schema.add_field("visitor_id", DataType.VARCHAR, max_length=64)
    schema.add_field("text", DataType.VARCHAR, max_length=TEXT_MAX)
    schema.add_field("vector", DataType.FLOAT_VECTOR, dim=EMBED_DIM)

    index = client.prepare_index_params()
    index.add_index(field_name="vector", index_type="FLAT", metric_type="IP")

    client.create_collection(COLLECTION, schema=schema, index_params=index)
    logger.info("已创建 Milvus 集合 %s", COLLECTION)


# ---------- 文档构建 ----------


def product_document(product: dict) -> dict:
    """商品向量：公开数据，visitor_id 为空串表示所有人可见。"""
    text = " ".join(
        part
        for part in (
            product.get("name"),
            product.get("category"),
            product.get("color"),
            product.get("description"),
        )
        if part
    )
    return {
        "id": f"product:{product['id']}",
        "source": "product",
        "source_id": product["id"],
        "visitor_id": "",
        "text": text[:TEXT_MAX],
    }


def work_order_document(order: dict) -> dict:
    """工单向量：私有数据，visitor_id 参与过滤，防止跨顾客泄露。"""
    text = f"{order.get('issue_type', '')} {order.get('description', '')}".strip()
    return {
        "id": f"work_order:{order['work_order_no']}",
        "source": "work_order",
        "source_id": order["work_order_no"],
        "visitor_id": order.get("visitor_id", ""),
        "text": text[:TEXT_MAX],
    }


def faq_document(section: dict) -> dict:
    """政策知识向量：公开数据，全店顾客可见。

    标题与小节名一并嵌入——只嵌正文会丢掉主题线索，
    比如「运费承担」小节单看正文不易判断属于退货还是换货。
    """
    text = f"{section.get('title', '')} {section.get('section', '')} {section.get('content', '')}".strip()
    return {
        "id": f"faq:{section['id']}",
        "source": "faq",
        "source_id": section["id"],
        "visitor_id": "",
        "text": text[:TEXT_MAX],
    }


# ---------- 写入 ----------


def upsert_documents(docs: list[dict]) -> int:
    """写入或覆盖向量。失败只记日志，不向调用方抛出——索引不应阻断业务。"""
    if not docs:
        return 0

    try:
        vectors = embed_texts([d["text"] for d in docs])
        rows = [{**d, "vector": v} for d, v in zip(docs, vectors)]
        client = get_client()
        client.upsert(COLLECTION, rows)
        client.flush(COLLECTION)
        return len(rows)
    except EmbeddingError as exc:
        logger.warning("向量写入跳过（嵌入失败）：%s", exc)
    except Exception as exc:  # noqa: BLE001 - 索引问题不应影响主流程
        logger.warning("向量写入跳过（%s）：%s", type(exc).__name__, exc)
    return 0


def delete_by_source(source: str, source_id: str) -> bool:
    try:
        client = get_client()
        client.delete(
            COLLECTION, filter=f'source == "{source}" and source_id == "{source_id}"'
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("向量删除跳过（%s）：%s", type(exc).__name__, exc)
        return False


# ---------- 检索 ----------


def search(query: str, visitor_id: str, top_k: int = 3) -> list[dict]:
    """按语义检索。顾客只能命中公开数据与自己的私有数据。"""
    try:
        vector = embed_texts([query])[0]
        client = get_client()
        client.load_collection(COLLECTION)
        results = client.search(
            COLLECTION,
            data=[vector],
            limit=top_k,
            output_fields=["source", "source_id", "text"],
            filter=f'visitor_id == "" or visitor_id == "{visitor_id}"',
        )
    except EmbeddingError as exc:
        logger.warning("向量检索失败（嵌入）：%s", exc)
        return []
    except Exception as exc:  # noqa: BLE001
        logger.warning("向量检索失败（%s）：%s", type(exc).__name__, exc)
        return []

    hits = []
    for hit in results[0] if results else []:
        entity = hit.get("entity", {})
        hits.append(
            {
                "source": entity.get("source"),
                "source_id": entity.get("source_id"),
                "text": entity.get("text"),
                "score": round(float(hit.get("distance", 0.0)), 4),
            }
        )
    return hits


def clear() -> None:
    """删除集合，供重建脚本使用。"""
    client = get_client()
    if client.has_collection(COLLECTION):
        client.drop_collection(COLLECTION)
    _ensure_collection(client)


def rebuild_from_store(drop: bool = False) -> dict:
    """从 MySQL 全量重建索引。

    在服务进程内调用可以避开 Milvus Lite 的文件独占锁问题。
    """
    from app import store

    if drop:
        clear()

    products = store.all_products_for_index()
    orders = store.all_work_orders_for_index()
    faqs = store.all_faq_for_index()

    docs = [product_document(p) for p in products]
    docs += [work_order_document(o) for o in orders]
    docs += [faq_document(f) for f in faqs]

    written = 0
    batch_size = 20
    for start in range(0, len(docs), batch_size):
        written += upsert_documents(docs[start : start + batch_size])

    return {
        "products": len(products),
        "work_orders": len(orders),
        "faq": len(faqs),
        "total": len(docs),
        "written": written,
    }
