"""把旧 SQLite 数据导入 MySQL。可重复执行：已存在的会话与消息跳过。

用法：uv run python scripts/migrate_sqlite_to_mysql.py [--dry-run]
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pymysql

from app.store import _conn_params

SQLITE_PATH = Path("data/shoppilot.db")


def parse_ts(value: str) -> datetime:
    """SQLite 里存的是 ISO 字符串，可能带时区。统一转成 naive UTC。"""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is not None:
        dt = dt.astimezone(tz=None).replace(tzinfo=None)
    return dt


def migrate(dry_run: bool = False) -> None:
    if not SQLITE_PATH.exists():
        print(f"未找到 {SQLITE_PATH}，跳过迁移")
        return

    src = sqlite3.connect(SQLITE_PATH)
    src.row_factory = sqlite3.Row

    conversations = src.execute(
        "SELECT id, visitor_id, created_at FROM conversations"
    ).fetchall()
    messages = src.execute(
        "SELECT conversation_id, role, content, created_at FROM messages ORDER BY id"
    ).fetchall()
    src.close()

    print(f"读取到 {len(conversations)} 个会话、{len(messages)} 条消息")

    if dry_run:
        for c in conversations:
            print(f"  [dry-run] 会话 {c['id']} ← {c['visitor_id']}")
        return

    conn = pymysql.connect(**_conn_params())
    inserted_conv = 0
    inserted_msg = 0
    try:
        with conn.cursor() as cur:
            for c in conversations:
                cur.execute(
                    "INSERT IGNORE INTO conversations (id, visitor_id, created_at)"
                    " VALUES (%s, %s, %s)",
                    (c["id"], c["visitor_id"], parse_ts(c["created_at"])),
                )
                inserted_conv += cur.rowcount

            for m in messages:
                cur.execute(
                    "SELECT 1 FROM messages"
                    " WHERE conversation_id = %s AND role = %s"
                    "   AND content = %s AND created_at = %s",
                    (
                        m["conversation_id"],
                        m["role"],
                        m["content"],
                        parse_ts(m["created_at"]),
                    ),
                )
                if cur.fetchone():
                    continue
                cur.execute(
                    "INSERT INTO messages"
                    " (conversation_id, role, content, created_at)"
                    " VALUES (%s, %s, %s, %s)",
                    (
                        m["conversation_id"],
                        m["role"],
                        m["content"],
                        parse_ts(m["created_at"]),
                    ),
                )
                inserted_msg += 1
        conn.commit()
    finally:
        conn.close()

    print(f"写入 MySQL：会话 {inserted_conv} 条，消息 {inserted_msg} 条")
    print("SQLite 文件保留未删除")


if __name__ == "__main__":
    migrate(dry_run="--dry-run" in sys.argv)
