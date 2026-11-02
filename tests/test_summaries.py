"""r18 T2：会话梗概表与水位线读历史的验证测试。

梗概是派生物（原文表保持逐字事实）；水位线只服务上下文组装，
/api/history 的全量 load_history 不受影响。
"""

import uuid

import pytest

from app.core import store


@pytest.fixture
def conv_id():
    visitor_id = "ctx-" + uuid.uuid4().hex[:8]
    conv = store.create_conversation(visitor_id)
    yield conv
    # 共享库不做清理，靠 visitor 隔离


def test_upsert_summary_idempotent(conv_id):
    """同一会话两次 upsert 是更新不是新增。"""
    store.upsert_summary(conv_id, "顾客问过保温杯", covered_until=10, token_count=9)
    store.upsert_summary(conv_id, "顾客问过保温杯和订单A1", covered_until=20, token_count=15)

    s = store.get_summary(conv_id)
    assert s["summary"] == "顾客问过保温杯和订单A1"
    assert s["covered_until"] == 20
    assert s["token_count"] == 15


def test_get_summary_missing_returns_none(conv_id):
    assert store.get_summary(conv_id) is None


def test_load_history_after_respects_watermark(conv_id):
    """水位线读：只返回 id > covered_until 的消息。"""
    for content in ("第一条", "第二条", "第三条"):
        store.append_message(conv_id, "user", content)

    rows = store.load_history_after(conv_id, after_id=0)
    assert len(rows) == 3

    # 水位线设为第一条消息的 id 之后——需要知道 id：按内容查
    from app.repo import chats

    all_ids = chats._message_ids(conv_id)
    first_id = all_ids[0]
    rows = store.load_history_after(conv_id, after_id=first_id)
    assert [r["content"] for r in rows] == ["第二条", "第三条"]


def test_load_history_full_ignores_watermark(conv_id):
    """/api/history 的 load_history 不受梗概影响（前端不能少消息）。"""
    store.append_message(conv_id, "user", "第一条")
    store.upsert_summary(conv_id, "梗概", covered_until=10**9, token_count=2)

    rows = store.load_history(conv_id, limit=20)
    assert len(rows) == 1, "水位线不应影响全量读取"


def test_messages_table_unaffected(conv_id):
    """upsert 梗概不写原始消息表——原文表保持逐字事实。"""
    msg_id = store.append_message(conv_id, "user", "原文一条")
    store.upsert_summary(conv_id, "梗概文本", covered_until=10**9, token_count=4)

    rows = store.load_history(conv_id, limit=20)
    assert rows == [{"role": "user", "content": "原文一条", "id": msg_id}]