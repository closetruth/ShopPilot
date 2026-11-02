"""r18 T3：token 预算上下文组装的验证测试。

账目要闭合（total = 各分项之和）；截断从最旧开始（梗概已覆盖部分
不怕丢）；history_budget 有保底 400，紧凑窗口下也不会每轮全截光。
"""

import uuid

import pytest

from app.core import store
from app.context.context_builder import ContextBundle, build_context


@pytest.fixture
def conv_id():
    visitor_id = "ctx-" + uuid.uuid4().hex[:8]
    conv = store.create_conversation(visitor_id)
    yield conv


def test_budget_accounts_close(conv_id):
    """账目闭合：history_budget 是差额与保底 400 的较大者。

    紧凑窗口下 system+tools+输出余量（实测 ~3500）会吃满 total(2048)，
    触发保底——这正是保底存在的意义。
    """
    bundle = build_context(conv_id, "你好")
    b = bundle.budget
    diff = b["total"] - b["system"] - b["tools"] - b["output_reserve"] - b["turn_input"]
    assert b["history_budget"] == max(diff, 400)
    assert b["history_budget"] >= 400, "保底 400 不能为负"


def test_no_summary_returns_none_bundle(conv_id):
    bundle = build_context(conv_id, "你好")
    assert bundle.summary_text is None
    assert bundle.recent_messages == []
    assert bundle.budget["summary_covered_until"] == 0


def test_recent_messages_within_budget(conv_id):
    """短对话：原文全部保留，不截断。"""
    for i in range(5):
        store.append_message(conv_id, "user", f"短消息{i}")
        store.append_message(conv_id, "assistant", f"回复{i}")

    bundle = build_context(conv_id, "下一问")
    assert len(bundle.recent_messages) == 10
    assert bundle.budget["dropped"] == 0


def test_over_budget_drops_oldest_first(conv_id):
    """超预算：从最旧的消息开始丢，最近的消息必须保留。"""
    # 紧凑窗口默认 8192×0.25≈2048，减去 system/tools/输出/输入后
    # history_budget 有保底 400。每条长消息 ~200 token，6 条必超。
    long_msg = "这是一条很长的消息" * 30  # ~270 字 ≈ 200+ token
    for i in range(6):
        store.append_message(conv_id, "user", f"{long_msg} #{i}")

    bundle = build_context(conv_id, "继续")
    b = bundle.budget
    assert b["dropped"] > 0, "应发生截断"
    assert bundle.recent_messages, "截断后必须有保留"
    # 最近一条必须在（从旧往新丢）
    assert bundle.recent_messages[-1]["content"].endswith("#5")
    assert b["recent_tokens"] <= b["history_budget"] or len(bundle.recent_messages) == 1, (
        "保留部分应不超预算（或至少剩一条）"
    )


def test_summary_excluded_from_recent(conv_id):
    """有梗概时：原文只取水位线之后的。"""
    from app.repo import chats

    store.append_message(conv_id, "user", "早期消息")
    watermark = chats._message_ids(conv_id)[0]
    store.upsert_summary(conv_id, "此前顾客问过保温杯", covered_until=watermark, token_count=10)
    store.append_message(conv_id, "user", "水位线后消息")

    bundle = build_context(conv_id, "下一问")
    assert bundle.summary_text == "此前顾客问过保温杯"
    assert [m["content"] for m in bundle.recent_messages] == ["水位线后消息"]
    assert bundle.budget["summary_covered_until"] == watermark


def test_bundle_is_frozen_dataclass(conv_id):
    bundle = build_context(conv_id, "x")
    assert isinstance(bundle, ContextBundle)
    with pytest.raises(Exception):
        bundle.summary_text = "hack"
