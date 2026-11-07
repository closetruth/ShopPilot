"""pytest 全局配置：加载 .env，让依赖嵌入 API 的测试能读到密钥。

没有这个文件时，`os.getenv("SILICONFLOW_API_KEY")` 在收集阶段返回空，
所有相关用例会被静默跳过——看起来「全绿」，实际一条没跑。
"""

import os
from datetime import datetime, timezone

import pytest
from dotenv import load_dotenv

load_dotenv()

# 默认关掉 MCP：否则每个起 TestClient 的用例都会拉一次 stdio 子进程。
# tests/test_mcp.py 用 monkeypatch.setenv 单独打开。
os.environ["MCP_ENABLED"] = "0"

# 只有这三张表的 id 是自增数字，可以按 id 水位线清理。
# `finetune_jobs` 的主键是 VARCHAR(32)（形如 'remote-fff7ec26'），
# `MAX(id)` 返回字符串，拿 int() 去解析会抛 ValueError——原来把它混在
# 同一个循环里，异常被 except 吞掉后直接走了 `yield; return` 分支，
# 清理代码**一次都没执行过**。结果 111 条标注、161 个数据集、102 个任务、
# 66 个模型全是测试造的，其中还有一个 `r21-act-xxxxxx` 被置成 active=1。
# 那些假标注让模型去学「退货怎么弄-1a2b3c4d」的后缀，报出的 Hit@1 0.8
# 毫无意义。存量已用 `scripts/purge_finetune_test_rows.py` 清掉。
_NUMERIC_ID_TABLES = (
    "finetune_labels",
    "finetune_datasets",
    "finetune_models",
)


@pytest.fixture(scope="session", autouse=True)
def _purge_finetune_rows_written_by_tests():
    """把本轮测试写进共享 MySQL 的行删掉。

    微调相关用例连的是**同一个远端库**，用带随机后缀的 query 来「防污染」。
    但这只防了覆盖，没防累积：`finetune_labels` 是整表读进训练集的，每跑
    一次全量测试就多几十条「退货怎么弄-1a2b3c4d」这样的假标注。实测数据集
    里 98 条标注**全部**是这类行，正例 48 条——模型学到的是后缀不是条款，
    报出来的 Hit@1 0.8 一点意义都没有。

    所以按 id 水位线在会话结束时清掉本轮新增的行：只删自己写的，不动任何
    真实标注。
    """
    from app.core.db import connect

    # 必须是 UTC 的 naive 值：`started_at` 是 `datetime(6)`，库里存的是
    # UTC（MySQL 的 NOW() 就是 UTC）。用本地 `datetime.now()` 在东八区会
    # 比库里大 8 小时，`started_at >= 本地时间` 永远匹配不到任何行，
    # 清理看着在跑，实际一条没删。
    session_start = datetime.now(timezone.utc).replace(tzinfo=None)
    marks: dict[str, int] = {}
    try:
        with connect() as conn, conn.cursor() as cur:
            for table in _NUMERIC_ID_TABLES:
                cur.execute(f"SELECT COALESCE(MAX(id), 0) AS m FROM {table}")
                marks[table] = int(cur.fetchone()["m"])
    except Exception:  # noqa: BLE001 - 库不可用时测试本就会失败，别在这里二次报错
        yield
        return

    yield

    try:
        with connect() as conn, conn.cursor() as cur:
            for table, mark in marks.items():
                cur.execute(f"DELETE FROM {table} WHERE id > %s", (mark,))
            # jobs 没有数字水位线，按本次会话开始时间清
            cur.execute(
                "DELETE FROM finetune_jobs WHERE started_at >= %s", (session_start,)
            )
    except Exception:  # noqa: BLE001 - 清理失败不该把测试结果改写成失败
        pass