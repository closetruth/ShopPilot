"""数据库连接与建表。

只负责「怎么连」和「表长什么样」，不含任何业务查询。
"""

from __future__ import annotations

import os
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


def conn_params() -> dict:
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
def connect() -> Iterator[pymysql.connections.Connection]:
    conn = pymysql.connect(**conn_params())
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn, conn.cursor() as cur:
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
        cur.execute(f"ALTER TABLE {table} MODIFY COLUMN {column} VARCHAR(32) NULL")

    cur.execute(
        "SELECT IS_NULLABLE FROM information_schema.COLUMNS"
        " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'work_orders'"
        "   AND COLUMN_NAME = 'product_id'"
    )
    row = cur.fetchone()
    if row and row["IS_NULLABLE"] == "NO":
        cur.execute("ALTER TABLE work_orders MODIFY COLUMN product_id VARCHAR(32) NULL")


def now() -> datetime:
    """统一写入 UTC naive 时间，MySQL DATETIME 不带时区。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)
