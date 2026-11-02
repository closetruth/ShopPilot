"""成本闸判定。

三层：
- 单轮 token 上限（`app.chat.agent` 的 TURN_TOKEN_LIMIT）——已有
- 会话日 token 预算（CONVERSATION_DAILY_TOKEN_BUDGET）
- 全局日成本上限（GLOBAL_DAILY_COST_YUAN）

限额配 0 表示关闭该层。全部软闸：触闸只换话术，不拒绝请求。
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, time, timezone

from app.repo import budget as budget_repo
from app.repo import usage as usage_repo

logger = logging.getLogger(__name__)

GLOBAL_BLOCK_TEXT = (
    "抱歉，今天的咨询量比较大，我先把您的问题记下来，"
    "稍后由人工客服跟进处理。"
)
CONVERSATION_BLOCK_TEXT = (
    "这一轮聊得比较久了，为避免信息出错，"
    "建议您稍后新开一个对话继续咨询，或转人工客服跟进。"
)


def conversation_limit() -> int:
    return _int_env("CONVERSATION_DAILY_TOKEN_BUDGET")


def global_limit_micros() -> int:
    """元 → 微元，避免浮点比较误差。"""
    return int(_float_env("GLOBAL_DAILY_COST_YUAN") * 1_000_000)


def day_start() -> datetime:
    """自然日边界，与 `db.now()` 的 UTC naive 口径一致。"""
    today = datetime.now(timezone.utc).date()
    return datetime.combine(today, time.min)


def global_blocked() -> bool:
    limit = global_limit_micros()
    if limit <= 0:
        return False
    return usage_repo.sum_cost_since(day_start()) >= limit


def conversation_over_budget(conversation_id: str | None) -> bool:
    """会话日预算按自然日重置，跨天不累计。"""
    limit = conversation_limit()
    if limit <= 0 or not conversation_id:
        return False
    used = usage_repo.sum_tokens_since(day_start(), conversation_id=conversation_id)
    return used >= limit


def record_event(
    scope: str,
    reason: str,
    *,
    conversation_id: str | None = None,
    visitor_id: str | None = None,
    detail: str | None = None,
) -> None:
    """观测类写入：失败只记日志，不许打断对话。"""
    try:
        budget_repo.record(
            scope,
            reason,
            conversation_id=conversation_id,
            visitor_id=visitor_id,
            detail=detail,
        )
    except Exception:  # noqa: BLE001 - 闸门日志失败不影响响应
        logger.warning("预算事件落库失败", exc_info=True)


def summary(since) -> dict:
    return budget_repo.sum_by_scope(since)


def _int_env(name: str) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return 0
    try:
        return int(float(raw))
    except ValueError:
        logger.warning("环境变量 %s 不是数字：%r", name, raw)
        return 0


def _float_env(name: str) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return 0.0
    try:
        return float(raw)
    except ValueError:
        logger.warning("环境变量 %s 不是数字：%r", name, raw)
        return 0.0