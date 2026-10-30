"""r15 Task 1：checkpoint 存储封装的验证测试。

fixture 提供带随机后缀的 thread_id，避免共享 MySQL 库下的并发冲突；
测试留下的 checkpoint 数据不清理，留作跨进程持久化的佐证。
"""

import os
import uuid

import pytest
from langgraph.checkpoint.base import empty_checkpoint

from app.checkpointer import get_checkpointer


@pytest.fixture
def mysql_checkpoint():
    """thread_id 带随机后缀：共享 MySQL 下多个测试进程互不覆盖。"""
    return f"test-r15-cp-{uuid.uuid4().hex[:8]}"


def checkpoint_with_values(values: dict, versions: dict | None = None):
    """按 langgraph 1.2.18 的 Checkpoint 结构构造带 channel_values 的 checkpoint。

    blob 值（dict 等）的读取走 checkpoint 的 channel_versions 与 blob 表的
    version join，两者必须与 put 的 new_versions 对齐，否则读不回来。
    """
    cp = empty_checkpoint()
    cp["channel_values"] = dict(values)
    if versions is not None:
        cp["channel_versions"] = dict(versions)
    return cp


def test_checkpoint_roundtrip(mysql_checkpoint):
    saver = get_checkpointer()
    config = {"configurable": {"thread_id": mysql_checkpoint, "checkpoint_ns": ""}}
    state = {"resolved_query": "退货运费", "intent": "knowledge", "turn_count": 3}
    saver.put(config, checkpoint_with_values(state), {}, {})
    restored = get_checkpointer().get(config)
    assert restored["channel_values"]["resolved_query"] == "退货运费"
    assert restored["channel_values"]["intent"] == "knowledge"
    assert restored["channel_values"]["turn_count"] == 3


def test_roundtrip_with_dict_blob(mysql_checkpoint):
    """slots 这类 dict 值走 blob 路径：channel_versions 与 new_versions 要对齐。"""
    saver = get_checkpointer()
    config = {"configurable": {"thread_id": mysql_checkpoint, "checkpoint_ns": ""}}
    state = {"slots": {"order_no": "A1001", "issue_type": "退货"}}
    cp = checkpoint_with_values(state, versions={"slots": "1"})
    saver.put(config, cp, {}, {"slots": "1"})
    restored = get_checkpointer().get(config)
    assert restored["channel_values"]["slots"] == {"order_no": "A1001", "issue_type": "退货"}


@pytest.mark.skipif(
    os.getenv("CHECKPOINT_BACKEND", "mysql").lower() != "mysql",
    reason="仅 MySQL 后端有 checkpoints 表",
)
def test_state_lands_in_mysql(mysql_checkpoint):
    """绕过 saver 直接查库，确认状态真的写进了 MySQL，而非进程内缓存。"""
    from app.db import connect

    saver = get_checkpointer()
    config = {"configurable": {"thread_id": mysql_checkpoint, "checkpoint_ns": ""}}
    state = {"resolved_query": "查询物流", "intent": "knowledge", "turn_count": 1}
    saver.put(config, checkpoint_with_values(state), {}, {})

    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM checkpoints WHERE thread_id = %s",
            (mysql_checkpoint,),
        )
        assert cur.fetchone()["n"] == 1


def test_get_unknown_thread_returns_none():
    config = {"configurable": {"thread_id": "no-such-thread-" + uuid.uuid4().hex[:8]}}
    assert get_checkpointer().get(config) is None


def test_sqlite_backend_roundtrip(tmp_path):
    """CHECKPOINT_BACKEND=sqlite 的兜底分支：临时库上完整的 put/get 往返。"""
    from app.checkpointer import _build_sqlite_checkpointer

    saver = _build_sqlite_checkpointer(tmp_path / "checkpoints.db")
    saver.setup()
    config = {"configurable": {"thread_id": "sqlite-probe", "checkpoint_ns": ""}}
    state = {"resolved_query": "退货运费", "turn_count": 2}
    cp = checkpoint_with_values(state, versions={"resolved_query": "1", "turn_count": "1"})
    saver.put(config, cp, {}, {"resolved_query": "1", "turn_count": "1"})
    restored = saver.get(config)
    assert restored["channel_values"]["resolved_query"] == "退货运费"
    assert restored["channel_values"]["turn_count"] == 2


def test_backend_selected_by_env(monkeypatch):
    """CHECKPOINT_BACKEND=sqlite 时 get_checkpointer 返回 SqliteSaver，且事后单例还原。"""
    from langgraph.checkpoint.sqlite import SqliteSaver

    from app import checkpointer as cp_mod

    monkeypatch.setenv("CHECKPOINT_BACKEND", "sqlite")
    monkeypatch.setattr(cp_mod, "_saver", None)
    saver = cp_mod.get_checkpointer()
    assert isinstance(saver, SqliteSaver)
