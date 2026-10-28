"""FAQ 政策知识读写。

`enabled=0` 的条目不参与检索，但保留在库里——下架比删除安全，
误删可随时恢复。
"""

from __future__ import annotations

import uuid

from app.db import connect, now


def upsert_document(doc: dict) -> None:
    """写入或更新一条 FAQ 小节。"""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO faq_documents
                (id, doc_id, title, category, version, section, content,
                 source, enabled, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                title = VALUES(title), category = VALUES(category),
                version = VALUES(version), section = VALUES(section),
                content = VALUES(content), source = VALUES(source),
                enabled = VALUES(enabled),
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
                int(doc.get("enabled", True)),
                now(),
            ),
        )


def create_document(
    title: str, section: str, category: str, content: str
) -> str:
    """新建一条知识，返回其 id。

    doc_id 与 id 都用生成的编号：Web 录入不涉及「一份文档含多小节」的
    分层，每条知识独立成条目，简化检索与编辑。
    """
    faq_id = "faq-" + uuid.uuid4().hex[:12]
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO faq_documents
                (id, doc_id, title, category, version, section, content,
                 source, enabled, updated_at)
            VALUES (%s, %s, %s, %s, '', %s, %s, 'web', 1, %s)
            """,
            (faq_id, faq_id, title, category, section, content, now()),
        )
    return faq_id


def update_document(
    faq_id: str, title: str, section: str, category: str, content: str
) -> bool:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE faq_documents
               SET title = %s, section = %s, category = %s, content = %s,
                   updated_at = %s
             WHERE id = %s
            """,
            (title, section, category, content, now(), faq_id),
        )
        return cur.rowcount > 0


def set_enabled(faq_id: str, enabled: bool) -> bool:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE faq_documents SET enabled = %s, updated_at = %s WHERE id = %s",
            (int(enabled), now(), faq_id),
        )
        return cur.rowcount > 0


def delete_document(faq_id: str) -> bool:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM faq_documents WHERE id = %s", (faq_id,))
        return cur.rowcount > 0


def all_for_index() -> list[dict]:
    """供向量重建使用：仅启用的 FAQ 小节。"""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, doc_id, title, category, section, content"
            " FROM faq_documents WHERE enabled = 1"
        )
        return list(cur.fetchall())


def by_id(faq_id: str) -> dict | None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, doc_id, title, category, section, content, enabled, source"
            " FROM faq_documents WHERE id = %s",
            (faq_id,),
        )
        return cur.fetchone()


def list_by_category(category: str | None = None) -> list[dict]:
    """供后台列表使用：含未启用条目，带 enabled 标记。"""
    sql = (
        "SELECT id, doc_id, title, category, section, content, enabled,"
        " source, updated_at"
        " FROM faq_documents WHERE 1=1"
    )
    params: list = []
    if category:
        sql += " AND category = %s"
        params.append(category)
    sql += " ORDER BY category, doc_id, id"
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def categories() -> list[dict]:
    """已有分类及各自的条目数，供录入界面的下拉框。"""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT category, COUNT(*) AS count
              FROM faq_documents
             GROUP BY category
             ORDER BY count DESC, category ASC
            """
        )
        return list(cur.fetchall())


def delete_missing(source: str, keep_ids: list[str]) -> list[str]:
    """删除某个来源下不在 keep_ids 中的条目，返回被删的 id。

    用于「以文件为准」的重新导入：文件里删掉的小节，库里也要消失，
    否则作废的政策仍会被检索到。
    """
    with connect() as conn, conn.cursor() as cur:
        if keep_ids:
            placeholders = ",".join(["%s"] * len(keep_ids))
            cur.execute(
                f"SELECT id FROM faq_documents"
                f" WHERE source = %s AND id NOT IN ({placeholders})",
                [source, *keep_ids],
            )
        else:
            cur.execute(
                "SELECT id FROM faq_documents WHERE source = %s", (source,)
            )
        stale = [r["id"] for r in cur.fetchall()]
        if not stale:
            return []

        placeholders = ",".join(["%s"] * len(stale))
        cur.execute(
            f"DELETE FROM faq_documents WHERE id IN ({placeholders})", stale
        )
        return stale
