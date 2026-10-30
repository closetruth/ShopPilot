"""LangGraph checkpoint 存储封装。

r15 起对话改造成 StateGraph，图的会话工作状态（resolved_query / intent /
slots 等）靠 checkpoint 持久化，跨进程恢复对话。这里只回答「存哪里、怎么连」，
图怎么组装是后续任务的事。

后端由环境变量 CHECKPOINT_BACKEND 选择：mysql（默认）或 sqlite。
MySQL 优先——会话状态要和 conversations / messages 一样落在共享库；
SQLite 只是连不上 MySQL 时的兜底。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

import pymysql
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.mysql.base import MIGRATIONS
# PyMySQLSaver 只在子模块里，包的 __init__ 并没有导出它（docstring 里的示例是错的）
from langgraph.checkpoint.mysql.pymysql import PyMySQLSaver

from app.db import conn_params

# 包内 SQL 假定 MySQL 8 的字符集默认排序：SELECT 的 json_table 列硬编码
# CHARACTER SET utf8mb4（解析为 utf8mb4_0900_ai_ci），JSON 函数结果的排序
# 也跟随连接会话。而本项目的库默认排序是 utf8mb4_unicode_ci，setup() 建表
# 会继承它，读路径的 JSON CTE join 就报 1267 Illegal mix of collations。
# 因此建表显式钉排序规则，连接会话也钉成同一个，两边对齐后 1267 消失。
_COLLATION = "utf8mb4_0900_ai_ci"

_SQLITE_PATH = Path(__file__).resolve().parent.parent / "data" / "checkpoints.db"

_lock = threading.Lock()
_saver: BaseCheckpointSaver | None = None


def get_checkpointer() -> BaseCheckpointSaver:
    """按 CHECKPOINT_BACKEND（mysql | sqlite，默认 mysql）返回进程级单例。

    首次调用建连接、建表（全部幂等，可重复启动）；之后复用同一 saver，
    进程内共享一个连接，图编译时直接传给它。
    """
    global _saver
    if _saver is None:
        with _lock:
            if _saver is None:
                backend = os.getenv("CHECKPOINT_BACKEND", "mysql").strip().lower()
                saver = _build_checkpointer(backend)
                saver.setup()
                _saver = saver
    return _saver


def _build_checkpointer(backend: str) -> BaseCheckpointSaver:
    if backend == "mysql":
        return _build_mysql_checkpointer()
    if backend == "sqlite":
        return _build_sqlite_checkpointer()
    raise ValueError(f"未知 CHECKPOINT_BACKEND: {backend!r}，支持 mysql / sqlite")


def _build_mysql_checkpointer() -> PyMySQLSaver:
    params = conn_params()
    # 包的写路径走 conn.begin()/commit() 显式事务，读路径是单条语句；
    # 跟包自带的 from_conn_string 一致用 autocommit，避免读连接停在未提交事务里
    params["autocommit"] = True
    params["collation"] = _COLLATION
    conn = pymysql.connect(**params)
    _ensure_mysql_tables(conn)
    return PyMySQLSaver(conn)


def _ensure_mysql_tables(conn: pymysql.connections.Connection) -> None:
    """按包自带的初始建表 DDL 预建 checkpoint 表，并钉上排序规则。

    不改包的表名：包的全部 SQL 硬编码 checkpoints / checkpoint_blobs /
    checkpoint_writes，换 r15_ 前缀就得把 SQL 全部重写；这几张表名与业务表
    （conversations 等）本就不冲突。setup() 见表已存在，只补迁移记账。
    """
    with conn.cursor() as cur:
        for ddl in MIGRATIONS:
            if not ddl.lstrip().upper().startswith("CREATE TABLE IF NOT EXISTS"):
                break  # 初始建表是 MIGRATIONS 开头连续几条，遇到 ALTER 即止
            body = ddl.rstrip().rstrip(";")
            cur.execute(body + f" DEFAULT CHARSET=utf8mb4 COLLATE={_COLLATION};")
    conn.commit()


def _build_sqlite_checkpointer(path: Path | None = None) -> BaseCheckpointSaver:
    from langgraph.checkpoint.sqlite import SqliteSaver

    path = path or _SQLITE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    # FastAPI 线程池会跨线程使用同一连接，须放开 sqlite3 的同线程检查
    conn = sqlite3.connect(path, check_same_thread=False)
    return SqliteSaver(conn)
