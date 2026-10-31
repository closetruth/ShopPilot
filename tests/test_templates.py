"""r15 Task 7：模板节点（投诉/闲聊）的验证测试。

两条模板路径零 LLM 调用：闲聊自我介绍（给误判留台阶的话术）、
投诉安抚 + 自动建「投诉建议」工单（顾客在工单面板能看到行动）。
"""

import uuid

import pytest

from app import store
from app.templates import chitchat_reply, complaint_reply


def test_chitchat_mentions_capabilities():
    """闲聊模板要自报能力，并给意图误判留台阶。"""
    text = chitchat_reply()
    assert "订单" in text and "售后" in text
    assert "如需查询" in text          # Review Focus：误判进闲聊时仍有出口


@pytest.mark.anyio
async def test_complaint_creates_ticket():
    """投诉模板要安抚 + 转人工话术，且名下新增一张投诉建议工单。"""
    visitor_id = "tmpl-" + uuid.uuid4().hex[:8]
    before = [
        wo for wo in store.list_work_orders(visitor_id)
        if wo["issue_type"] == "投诉建议"
    ]

    text = await complaint_reply(visitor_id)
    assert "人工" in text

    after = [
        wo for wo in store.list_work_orders(visitor_id)
        if wo["issue_type"] == "投诉建议"
    ]
    assert len(after) == len(before) + 1, "投诉应自动建一张投诉建议工单"
    assert after[0]["work_order_no"] in text, "话术里应带工单号让顾客看到行动"
    assert after[0]["order_no"] is None, "投诉工单不强制关联订单"


@pytest.mark.anyio
async def test_complaint_duplicate_no_new_ticket():
    """同一顾客重复投诉不堆工单——已有未完结投诉单时话术不建新单。"""
    visitor_id = "tmpl-" + uuid.uuid4().hex[:8]
    await complaint_reply(visitor_id)
    count_after_first = len(
        [wo for wo in store.list_work_orders(visitor_id) if wo["issue_type"] == "投诉建议"]
    )

    text = await complaint_reply(visitor_id)
    count_after_second = len(
        [wo for wo in store.list_work_orders(visitor_id) if wo["issue_type"] == "投诉建议"]
    )
    assert count_after_second == count_after_first, "重复投诉不应再建新工单"
    assert "人工" in text  # 话术仍然完整
