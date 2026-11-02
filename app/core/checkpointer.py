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
import asyncio
from pathlib import Path

import pymysql
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.mysql.base import MIGRATIONS

from app.core.db import conn_params

# 包内 SQL 假定 MySQL 8 的字符集默认排序：SELECT 的 json_table 列硬编码
# CHARACTER SET utf8mb4（解析为 utf8mb4_0900_ai_ci），JSON 函数结果的排序
# 也跟随连接会话。而本项目的库默认排序是 utf8mb4_unicode_ci，setup() 建表
# 会继承它，读路径的 JSON CTE join 就报 1267 Illegal mix of collations。
# 因此建表显式钉排序规则，连接会话也钉成同一个，两边对齐后 1267 消失。
_COLLATION = "utf8mb4_0900_ai_ci"

_SQLITE_PATH = Path(__file__).resolve().parent.parent / "data" / "checkpoints.db"

_lock = asyncio.Lock()
_saver: BaseCheckpointSaver | None = None


async def get_checkpointer() -> BaseCheckpointSaver:
    """按 CHECKPOINT_BACKEND（mysql | sqlite，默认 mysql）返回进程级单例。

    异步构造：aiomysql 连接必须建在事件循环内（同步上下文建的连接绑定
    asyncio.run 的临时循环，循环关闭即失效）。FastAPI lifespan 与测试里
    均 await 本函数。
    """
    global _saver
    if _saver is None:
        async with _lock:
            if _saver is None:
                backend = os.getenv("CHECKPOINT_BACKEND", "mysql").strip().lower()
                saver = await _build_checkpointer(backend)
                await saver.setup()
                _saver = saver
    return _saver


async def _build_checkpointer(backend: str) -> BaseCheckpointSaver:
    if backend == "mysql":
        return await build_mysql_saver()
    if backend == "sqlite":
        return _build_sqlite_checkpointer()
    raise ValueError(f"未知 CHECKPOINT_BACKEND: {backend!r}，支持 mysql / sqlite")


async def build_mysql_saver() -> BaseCheckpointSaver:
    """建一个 AIOMySQLSaver：预建表（同步连接）+ 异步连接（当前事件循环）。

    get_checkpointer 的单例路径与测试的独立实例路径共用。
    """
    sync_conn = pymysql.connect(**conn_params())
    _ensure_mysql_tables(sync_conn)
    sync_conn.close()

    params = conn_params()
    # aiomysql 参数名不同：db（非 database）；无 collation 参数，
    # 排序规则改由 init_command 设会话变量，与读路径的 JSON CTE 对齐
    params["db"] = params.pop("database")
    params.pop("cursorclass", None)
    params["autocommit"] = True
    params["init_command"] = f"SET collation_connection = '{_COLLATION}'"

    import aiomysql
    import aiomysql.connection as _aconn
    import pymysql.converters as _conv
    from langgraph.checkpoint.mysql.aio import AIOMySQLSaver

    # pymysql 1.2+ 把 escape_bytes_prefixed 换成哨兵字符串
    # 「DO NOT IMPORT THIS!!!」（issue #1274，本意是禁外部 import），
    # 而 aiomysql 0.3.2 恰好在 escape() 里 import 了它——blob 写入即崩。
    # 补回真实现（_binary 十六进制字面量），包升级移除此 workaround 时删除
    if not callable(_aconn.escape_bytes_prefixed):
        _aconn.escape_bytes_prefixed = _conv.escape_bytes

    conn = await aiomysql.connect(**params)
    return AIOMySQLSaver(conn)


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