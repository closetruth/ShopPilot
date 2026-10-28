"""MySQL 持久化：会话、消息、商品、工单。

访问控制靠 visitor_id：会话与工单都归属某个访客，查询时强制带上。
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

import pymysql
import pymysql.cursors

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id          VARCHAR(32)  NOT NULL PRIMARY KEY,
    visitor_id  VARCHAR(64)  NOT NULL,
    created_at  DATETIME(6)  NOT NULL,
    KEY idx_conversations_visitor (visitor_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS messages (
    id              BIGINT        NOT NULL AUTO_INCREMENT PRIMARY KEY,
    conversation_id VARCHAR(32)   NOT NULL,
    role            VARCHAR(16)   NOT NULL,
    content         MEDIUMTEXT    NOT NULL,
    created_at      DATETIME(6)   NOT NULL,
    KEY idx_messages_conv (conversation_id, id),
    CONSTRAINT fk_messages_conv FOREIGN KEY (conversation_id)
        REFERENCES conversations(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS products (
    id          VARCHAR(32)   NOT NULL PRIMARY KEY,
    name        VARCHAR(128)  NOT NULL,
    category    VARCHAR(64)   NOT NULL,
    color       VARCHAR(32)   NOT NULL,
    price_cents INT           NOT NULL,
    stock       INT           NOT NULL,
    description TEXT          NOT NULL,
    KEY idx_products_name (name),
    KEY idx_products_category (category)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS work_orders (
    id          VARCHAR(32)   NOT NULL PRIMARY KEY,
    visitor_id  VARCHAR(64)   NOT NULL,
    order_no    VARCHAR(32)   NOT NULL,
    product_id  VARCHAR(32)   NOT NULL,
    status      VARCHAR(32)   NOT NULL,
    issue       TEXT          NOT NULL,
    created_at  DATETIME(6)   NOT NULL,
    updated_at  DATETIME(6)   NOT NULL,
    KEY idx_orders_visitor_order (visitor_id, order_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""


def _conn_params() -> dict:
    return {
        "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
        "port": int(os.getenv("MYSQL_PORT", "3307")),
        "user": os.getenv("MYSQL_USER", "shoppilot"),
        "password": os.getenv("MYSQL_PASSWORD", "shoppilot"),
        "database": os.getenv("MYSQL_DATABASE", "shoppilot"),
        "charset": "utf8mb4",
        "cursorclass": pymysql.cursors.DictCursor,
        "autocommit": False,
    }


@contextmanager
def _connect() -> Iterator[pymysql.connections.Connection]:
    conn = pymysql.connect(**_conn_params())
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with _connect() as conn:
        with conn.cursor() as cur:
            for stmt in SCHEMA.split(";"):
                if stmt.strip():
                    cur.execute(stmt)


# ---------- 会话 ----------


def create_conversation(visitor_id: str) -> str:
    conv_id = uuid.uuid4().hex
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO conversations (id, visitor_id, created_at) VALUES (%s, %s, %s)",
            (conv_id, visitor_id, _now()),
        )
    return conv_id


def conversation_owned_by(conversation_id: str, visitor_id: str) -> bool:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM conversations WHERE id = %s AND visitor_id = %s",
            (conversation_id, visitor_id),
        )
        return cur.fetchone() is not None


def load_history(conversation_id: str, limit: int = 20) -> list[dict[str, str]]:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT role, content FROM (
                SELECT role, content, id FROM messages
                 WHERE conversation_id = %s
                 ORDER BY id DESC
                 LIMIT %s
            ) AS recent ORDER BY id ASC
            """,
            (conversation_id, limit),
        )
        rows = cur.fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in rows]


def list_conversations(visitor_id: str) -> list[dict]:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.id,
                   c.created_at,
                   (SELECT m.content FROM messages m
                     WHERE m.conversation_id = c.id AND m.role = 'user'
                     ORDER BY m.id ASC LIMIT 1) AS first_user,
                   (SELECT MAX(m.id) FROM messages m
                     WHERE m.conversation_id = c.id) AS last_msg_id
              FROM conversations c
             WHERE c.visitor_id = %s
             ORDER BY COALESCE(
                 (SELECT MAX(m.id) FROM messages m WHERE m.conversation_id = c.id), 0
             ) DESC
            """,
            (visitor_id,),
        )
        rows = cur.fetchall()
    return [
        {
            "id": r["id"],
            "title": _title_of(r["first_user"]),
            "created_at": r["created_at"].isoformat(),
            "has_messages": r["last_msg_id"] is not None,
        }
        for r in rows
    ]


def append_message(conversation_id: str, role: str, content: str) -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO messages (conversation_id, role, content, created_at)"
            " VALUES (%s, %s, %s, %s)",
            (conversation_id, role, content, _now()),
        )


# ---------- 工具查询 ----------


def search_products(
    keyword: str | None = None, product_id: str | None = None, limit: int = 5
) -> list[dict]:
    """按关键词或 ID 查商品。只返回对顾客可见的字段。

    关键词按空格切分，每个词都要命中（名称/类别/颜色/描述任一），
    这样「蓝色 杯子」能找到「青花瓷马克杯 蓝色」。
    """
    if product_id:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, category, color, price_cents, stock, description
                  FROM products WHERE id = %s LIMIT 1
                """,
                (product_id,),
            )
            rows = cur.fetchall()
        return [_product_row(r) for r in rows]

    if not keyword:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, category, color, price_cents, stock, description
                  FROM products ORDER BY stock DESC, id ASC LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
        return [_product_row(r) for r in rows]

    terms = [t for t in keyword.split() if t][:4]
    rows = _search_terms(terms, joiner="AND", limit=limit)
    if not rows and len(terms) > 1:
        # 全部词都命中太严时放宽为「至少一半命中」，让「蓝色 杯子」也能有结果
        rows = _search_terms_any(terms, max(1, len(terms) // 2), limit)
    return [_product_row(r) for r in rows]


def _search_terms_any(terms: list[str], need: int, limit: int) -> list[dict]:
    """至少 need 个词命中即返回。用 SUM(CASE WHEN ...) 统计命中数。"""
    per_term = (
        "SUM(CASE WHEN name LIKE %s OR category LIKE %s"
        " OR color LIKE %s OR description LIKE %s THEN 1 ELSE 0 END)"
    )
    score_expr = " + ".join(per_term for _ in terms)

    params: list = []
    for term in terms:
        like = f"%{term}%"
        params.extend([like, like, like, like])
    params.extend([need, limit])

    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, name, category, color, price_cents, stock, description
              FROM products
             GROUP BY id, name, category, color, price_cents, stock, description
            HAVING {score_expr} >= %s
             ORDER BY stock DESC, id ASC LIMIT %s
            """,
            params,
        )
        return cur.fetchall()


def _search_terms(terms: list[str], joiner: str, limit: int) -> list[dict]:
    where = f" {joiner} ".join(
        "(name LIKE %s OR category LIKE %s OR color LIKE %s OR description LIKE %s)"
        for _ in terms
    )
    params: list = []
    for term in terms:
        like = f"%{term}%"
        params.extend([like, like, like, like])
    params.append(limit)

    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, name, category, color, price_cents, stock, description
              FROM products WHERE {where}
             ORDER BY stock DESC, id ASC LIMIT %s
            """,
            params,
        )
        return cur.fetchall()


def _product_row(r: dict) -> dict:
    return {
        "id": r["id"],
        "name": r["name"],
        "category": r["category"],
        "color": r["color"],
        "price_yuan": r["price_cents"] / 100,
        "stock": r["stock"],
        "description": r["description"],
    }


def find_work_order(visitor_id: str, order_no: str) -> dict | None:
    """按订单号查该访客名下的工单。访客不匹配时返回 None，不区分「不存在」与「不属于你」。"""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT o.id, o.order_no, o.status, o.issue,
                   o.created_at, o.updated_at,
                   p.name AS product_name, p.color AS product_color
              FROM work_orders o
              LEFT JOIN products p ON p.id = o.product_id
             WHERE o.visitor_id = %s AND o.order_no = %s
             LIMIT 1
            """,
            (visitor_id, order_no),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "work_order_id": row["id"],
        "order_no": row["order_no"],
        "status": row["status"],
        "issue": row["issue"],
        "product": (
            f"{row['product_name']}（{row['product_color']}）"
            if row["product_name"]
            else None
        ),
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


def _title_of(text: str | None) -> str:
    if not text:
        return "新对话"
    flat = " ".join(text.split())
    return flat[:20] + ("…" if len(flat) > 20 else "")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
