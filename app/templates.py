"""模板节点：投诉与闲聊的固定话术，零 LLM 调用。

投诉是情绪场景，不该让模型自由发挥——安抚 + 已记录 + 转人工，并直接
建一张「投诉建议」工单让顾客在面板看到行动。闲聊自我介绍，末尾留一句
「如需查询」给意图误判留台阶：知识类消息被误判成闲聊时，顾客仍知道
可以继续说需求。
"""

from __future__ import annotations

import logging

from app import store

logger = logging.getLogger(__name__)

CHITCHAT_TEXT = (
    "您好，我是本店的智能客服助手，可以帮您：\n"
    "- 查询订单与售后工单进度\n"
    "- 解答退换货、运费、发票等售后政策\n"
    "- 登记售后问题并生成工单\n\n"
    "如需查询订单或有售后问题，请直接告诉我~"
)

COMPLAINT_PREFIX = "非常抱歉给您带来了不好的体验，您的心情我完全理解。"
COMPLAINT_SUFFIX = "已为您安排人工客服优先跟进，请您稍等，我们会尽快给您答复。"


def chitchat_reply() -> str:
    """闲聊固定话术，零副作用。"""
    return CHITCHAT_TEXT


async def complaint_reply(visitor_id: str) -> str:
    """投诉固定话术 + 自动建「投诉建议」工单。

    建单失败不影响话术返回——安抚与转人工承诺是模板的核心职责，
    工单只是给顾客的可见行动；写入失败记日志即可，不阻断对话。
    """
    ticket_no = _create_complaint_ticket(visitor_id)
    if ticket_no:
        return f"{COMPLAINT_PREFIX}已为您登记投诉（工单号 {ticket_no}）。{COMPLAINT_SUFFIX}"
    return f"{COMPLAINT_PREFIX}已记录您的反馈。{COMPLAINT_SUFFIX}"


def _create_complaint_ticket(visitor_id: str) -> str | None:
    """自动建投诉工单；已有未完结投诉单时不重复建。返回工单号或 None。"""
    try:
        existing = store.find_open_work_order(visitor_id, None, "投诉建议")
        if existing is not None:
            return existing["id"]
        return store.create_work_order(
            visitor_id=visitor_id,
            order_no=None,
            issue_type="投诉建议",
            description="顾客在对话中表达投诉，由投诉模板自动登记，待人工跟进核实具体问题。",
            source="template",
        )
    except Exception:  # noqa: BLE001 - 工单失败不阻断话术
        logger.warning("投诉模板自动建单失败", exc_info=True)
        return None
