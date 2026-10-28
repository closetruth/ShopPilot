"""FAQ 政策知识读写。"""

from __future__ import annotations

from app.db import connect, now


def upsert_document(doc: dict) -> None:
    """写入或更新一条 FAQ 小节。"""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO faq_documents
                (id, doc_id, title, category, version, section, content,
                 source, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                title = VALUES(title), category = VALUES(category),
                version = VALUES(version), section = VALUES(section),
                content = VALUES(content), source = VALUES(source),
                updated_at = VALUES(updated_at)
            """,
            (
                doc["id"],
                doc["doc_id"],
                doc["title"],
                doc["category"],
                doc.get("version", ""),
                doc["section"],
                doc["content"],
                doc.get("source", ""),
                now(),
            ),
        )


def all_for_index() -> list[dict]:
    """供向量重建使用：全部 FAQ 小节。"""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, doc_id, title, category, section, content FROM faq_documents"
        )
        return list(cur.fetchall())


def by_id(faq_id: str) -> dict | None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, doc_id, title, category, section, content"
            " FROM faq_documents WHERE id = %s",
            (faq_id,),
        )
        return cur.fetchone()


def list_by_category(category: str | None = None) -> list[dict]:
    sql = (
        "SELECT id, doc_id, title, category, section, content"
        " FROM faq_documents WHERE 1=1"
    )
    params: list = []
    if category:
        sql += " AND category = %s"
        params.append(category)
    sql += " ORDER BY doc_id, id"
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())
