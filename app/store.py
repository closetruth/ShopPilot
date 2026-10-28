"""MySQL 持久化：会话、消息、商品、工单。

访问控制靠 visitor_id：会话与工单都归属某个访客，查询时强制带上。
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
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

CREATE TABLE IF NOT EXISTS faq_documents (
    id          VARCHAR(64)   NOT NULL PRIMARY KEY,
    doc_id      VARCHAR(32)   NOT NULL,
    title       VARCHAR(128)  NOT NULL,
    category    VARCHAR(32)   NOT NULL,
    version     VARCHAR(32)   NOT NULL DEFAULT '',
    section     VARCHAR(128)  NOT NULL,
    content     TEXT          NOT NULL,
    source      VARCHAR(256)  NOT NULL DEFAULT '',
    updated_at  DATETIME(6)   NOT NULL,
    KEY idx_faq_doc (doc_id),
    KEY idx_faq_category (category)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS orders (
    order_no    VARCHAR(32)   NOT NULL PRIMARY KEY,
    visitor_id  VARCHAR(64)   NOT NULL,
    product_id  VARCHAR(32)   NULL,
    quantity    INT           NOT NULL DEFAULT 1,
    amount_cents INT          NOT NULL DEFAULT 0,
    created_at  DATETIME(6)   NOT NULL,
    KEY idx_orders_visitor (visitor_id),
    KEY idx_orders_product (product_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS work_order_drafts (
    id          VARCHAR(48)   NOT NULL PRIMARY KEY,
    visitor_id  VARCHAR(64)   NOT NULL,
    conversation_id VARCHAR(32) NOT NULL DEFAULT '',
    order_no    VARCHAR(32)   NULL,
    issue_type  VARCHAR(32)   NOT NULL,
    description TEXT          NOT NULL,
    created_at  DATETIME(6)   NOT NULL,
    expires_at  DATETIME(6)   NOT NULL,
    KEY idx_drafts_visitor (visitor_id),
    KEY idx_drafts_expiry (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS work_orders (
    id          VARCHAR(32)   NOT NULL PRIMARY KEY,
    visitor_id  VARCHAR(64)   NOT NULL,
    order_no    VARCHAR(32)   NULL,
    product_id  VARCHAR(32)   NULL,
    status      VARCHAR(32)   NOT NULL,
    issue       TEXT          NOT NULL,
    issue_type  VARCHAR(32)   NOT NULL DEFAULT '其他',
    priority    VARCHAR(16)   NOT NULL DEFAULT '普通',
    source      VARCHAR(16)   NOT NULL DEFAULT 'ai',
    created_at  DATETIME(6)   NOT NULL,
    updated_at  DATETIME(6)   NOT NULL,
    KEY idx_orders_visitor_order (visitor_id, order_no),
    KEY idx_orders_dedupe (visitor_id, order_no, issue_type, status)
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
            _ensure_columns(cur)


def _ensure_columns(cur) -> None:
    """为已存在的旧表补列。MySQL 没有 ADD COLUMN IF NOT EXISTS，先查再改。"""
    required = {
        "work_orders": [
            ("issue_type", "VARCHAR(32) NOT NULL DEFAULT '其他'"),
            ("priority", "VARCHAR(16) NOT NULL DEFAULT '普通'"),
            ("source", "VARCHAR(16) NOT NULL DEFAULT 'ai'"),
        ]
    }
    for table, columns in required.items():
        cur.execute(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS"
            " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
            (table,),
        )
        existing = {row["COLUMN_NAME"] for row in cur.fetchall()}
        for name, definition in columns:
            if name not in existing:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    # 工单可以不带订单号与商品（咨询、投诉建议类），相关列需允许为空
    cur.execute(
        "SELECT TABLE_NAME, COLUMN_NAME, IS_NULLABLE FROM information_schema.COLUMNS"
        " WHERE TABLE_SCHEMA = DATABASE()"
        "   AND COLUMN_NAME = 'order_no'"
        "   AND TABLE_NAME IN ('work_orders', 'work_order_drafts')"
    )
    nullable_fixes = [
        (r["TABLE_NAME"], r["COLUMN_NAME"])
        for r in cur.fetchall()
        if r["IS_NULLABLE"] == "NO"
    ]
    for table, column in nullable_fixes:
        cur.execute(
            f"ALTER TABLE {table} MODIFY COLUMN {column} VARCHAR(32) NULL"
        )

    cur.execute(
        "SELECT IS_NULLABLE FROM information_schema.COLUMNS"
        " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'work_orders'"
        "   AND COLUMN_NAME = 'product_id'"
    )
    row = cur.fetchone()
    if row and row["IS_NULLABLE"] == "NO":
        cur.execute("ALTER TABLE work_orders MODIFY COLUMN product_id VARCHAR(32) NULL")


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


WORK_ORDER_OPEN_STATUSES = ("待处理", "处理中")


def visitor_has_order(visitor_id: str, order_no: str) -> bool:
    """该顾客名下是否有此订单。建单前据此校验归属。"""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM orders WHERE visitor_id = %s AND order_no = %s LIMIT 1",
            (visitor_id, order_no),
        )
        return cur.fetchone() is not None


def list_orders(visitor_id: str) -> list[dict]:
    """该顾客的订单，最新的在前，附带已关联的工单。"""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT o.order_no, o.quantity, o.amount_cents, o.created_at,
                   p.name AS product_name, p.color AS product_color
              FROM orders o
              LEFT JOIN products p ON p.id = o.product_id
             WHERE o.visitor_id = %s
             ORDER BY o.created_at DESC
            """,
            (visitor_id,),
        )
        rows = cur.fetchall()

        cur.execute(
            """
            SELECT order_no, id, status, issue_type
              FROM work_orders
             WHERE visitor_id = %s
             ORDER BY created_at ASC
            """,
            (visitor_id,),
        )
        order_rows = cur.fetchall()

    by_order: dict[str, list[dict]] = {}
    for wo in order_rows:
        by_order.setdefault(wo["order_no"], []).append(
            {
                "work_order_no": wo["id"],
                "status": wo["status"],
                "issue_type": wo["issue_type"],
            }
        )

    return [
        {
            "order_no": r["order_no"],
            "product": (
                f"{r['product_name']}（{r['product_color']}）"
                if r["product_name"]
                else None
            ),
            "quantity": r["quantity"],
            "amount_yuan": r["amount_cents"] / 100,
            "created_at": r["created_at"].isoformat(),
            "work_orders": by_order.get(r["order_no"], []),
        }
        for r in rows
    ]


def get_order(visitor_id: str, order_no: str) -> dict | None:
    for order in list_orders(visitor_id):
        if order["order_no"] == order_no:
            return order
    return None


def find_open_work_order(
    visitor_id: str, order_no: str | None, issue_type: str
) -> dict | None:
    """查找同一顾客、同一订单、同一类型且未完结的工单，用于防重复建单。

    无订单的工单（咨询/投诉建议/其他）按「顾客 + 类型」判重，否则 NULL
    与 NULL 不相等，会导致无限重复建单。
    """
    with _connect() as conn, conn.cursor() as cur:
        if order_no is None:
            cur.execute(
                """
                SELECT id, status, created_at
                  FROM work_orders
                 WHERE visitor_id = %s AND order_no IS NULL AND issue_type = %s
                   AND status IN %s
                 ORDER BY id ASC LIMIT 1
                """,
                (visitor_id, issue_type, WORK_ORDER_OPEN_STATUSES),
            )
        else:
            cur.execute(
                """
                SELECT id, status, created_at
                  FROM work_orders
                 WHERE visitor_id = %s AND order_no = %s AND issue_type = %s
                   AND status IN %s
                 ORDER BY id ASC LIMIT 1
                """,
                (visitor_id, order_no, issue_type, WORK_ORDER_OPEN_STATUSES),
            )
        return cur.fetchone()


DRAFT_TTL_MINUTES = 30


def create_work_order_draft(
    visitor_id: str,
    conversation_id: str,
    order_no: str | None,
    issue_type: str,
    description: str,
) -> dict:
    """暂存待确认的建单草稿。草稿不产生工单，只是给前端渲染确认卡。"""
    draft_id = "drf-" + uuid.uuid4().hex
    now = _now()
    expires = now + timedelta(minutes=DRAFT_TTL_MINUTES)

    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO work_order_drafts
                (id, visitor_id, conversation_id, order_no, issue_type,
                 description, created_at, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                draft_id,
                visitor_id,
                conversation_id,
                order_no,
                issue_type,
                description,
                now,
                expires,
            ),
        )
    return {
        "draft_id": draft_id,
        "order_no": order_no,
        "issue_type": issue_type,
        "description": description,
        "expires_at": expires.isoformat(),
        "product": _product_for_order(visitor_id, order_no),
    }


def get_work_order_draft(draft_id: str, visitor_id: str) -> dict | None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, conversation_id, order_no, issue_type, description, expires_at
              FROM work_order_drafts
             WHERE id = %s AND visitor_id = %s
            """,
            (draft_id, visitor_id),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "draft_id": row["id"],
        "conversation_id": row["conversation_id"],
        "order_no": row["order_no"],
        "issue_type": row["issue_type"],
        "description": row["description"],
        "expires_at": row["expires_at"].isoformat(),
        "product": _product_for_order(visitor_id, row["order_no"]),
    }


def delete_work_order_draft(draft_id: str) -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM work_order_drafts WHERE id = %s", (draft_id,))


def purge_expired_drafts() -> int:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM work_order_drafts WHERE expires_at < %s", (_now(),))
        return cur.rowcount


def _product_for_order(visitor_id: str, order_no: str | None) -> str | None:
    """该顾客此订单关联的商品名，用于确认卡上核对。无订单时返回 None。"""
    if order_no is None:
        return None
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.name, p.color
              FROM orders o
              JOIN products p ON p.id = o.product_id
             WHERE o.visitor_id = %s AND o.order_no = %s
             LIMIT 1
            """,
            (visitor_id, order_no),
        )
        row = cur.fetchone()
        if row is None:
            cur.execute(
                """
                SELECT p.name, p.color
                  FROM work_orders o
                  JOIN products p ON p.id = o.product_id
                 WHERE o.visitor_id = %s AND o.order_no = %s
                 ORDER BY o.created_at DESC LIMIT 1
                """,
                (visitor_id, order_no),
            )
            row = cur.fetchone()
    if row is None:
        return None
    return f"{row['name']}（{row['color']}）"


def list_work_orders(visitor_id: str) -> list[dict]:
    """该顾客全部工单，最近更新在前，附带商品名用于核对。"""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT o.id, o.order_no, o.issue_type, o.status, o.issue,
                   o.created_at, o.updated_at,
                   p.name AS product_name, p.color AS product_color
              FROM work_orders o
              LEFT JOIN products p ON p.id = o.product_id
             WHERE o.visitor_id = %s
             ORDER BY o.updated_at DESC
            """,
            (visitor_id,),
        )
        rows = cur.fetchall()

    return [
        {
            "work_order_no": r["id"],
            "order_no": r["order_no"],
            "issue_type": r["issue_type"],
            "status": r["status"],
            "description": r["issue"],
            "product": (
                f"{r['product_name']}（{r['product_color']}）"
                if r["product_name"]
                else None
            ),
            "created_at": r["created_at"].isoformat(),
            "updated_at": r["updated_at"].isoformat(),
        }
        for r in rows
    ]


def get_work_order(visitor_id: str, work_order_no: str) -> dict | None:
    for order in list_work_orders(visitor_id):
        if order["work_order_no"] == work_order_no:
            return order
    return None


def create_work_order(
    visitor_id: str,
    order_no: str | None,
    issue_type: str,
    description: str,
) -> str:
    """新建工单，返回对外工单号。归属与状态由服务端强制，不由模型传入。

    order_no 为空时表示不与具体订单关联（咨询、投诉建议类）。
    """
    work_order_no = "WO" + uuid.uuid4().hex[:10].upper()

    with _connect() as conn, conn.cursor() as cur:
        # 商品从该顾客同一订单的既有工单继承；无订单则留空
        product_id = None
        if order_no is not None:
            cur.execute(
                """
                SELECT product_id FROM work_orders
                 WHERE visitor_id = %s AND order_no = %s AND product_id IS NOT NULL
                 ORDER BY created_at DESC LIMIT 1
                """,
                (visitor_id, order_no),
            )
            row = cur.fetchone()
            product_id = row["product_id"] if row else None

            if product_id is None:
                cur.execute(
                    "SELECT product_id FROM orders"
                    " WHERE visitor_id = %s AND order_no = %s LIMIT 1",
                    (visitor_id, order_no),
                )
                row = cur.fetchone()
                product_id = row["product_id"] if row else None

        now = _now()
        cur.execute(
            """
            INSERT INTO work_orders
                (id, visitor_id, order_no, product_id, status, issue, issue_type,
                 priority, source, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                work_order_no,
                visitor_id,
                order_no,
                product_id,
                "待处理",
                description,
                issue_type,
                "普通",
                "ai",
                now,
                now,
            ),
        )
    return work_order_no


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


def upsert_faq_document(doc: dict) -> None:
    """写入或更新一条 FAQ 小节。"""
    with _connect() as conn, conn.cursor() as cur:
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
                _now(),
            ),
        )


def all_faq_for_index() -> list[dict]:
    """供向量重建使用：全部 FAQ 小节。"""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, doc_id, title, category, section, content FROM faq_documents"
        )
        return list(cur.fetchall())


def faq_by_id(faq_id: str) -> dict | None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, doc_id, title, category, section, content"
            " FROM faq_documents WHERE id = %s",
            (faq_id,),
        )
        return cur.fetchone()


def list_faq(category: str | None = None) -> list[dict]:
    sql = (
        "SELECT id, doc_id, title, category, section, content"
        " FROM faq_documents WHERE 1=1"
    )
    params: list = []
    if category:
        sql += " AND category = %s"
        params.append(category)
    sql += " ORDER BY doc_id, id"
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def _title_of(text: str | None) -> str:
    if not text:
        return "新对话"
    flat = " ".join(text.split())
    return flat[:20] + ("…" if len(flat) > 20 else "")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
