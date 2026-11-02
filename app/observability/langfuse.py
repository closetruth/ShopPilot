"""Langfuse 追踪（可选件）。

缺 key 或装配失败一律降级为「不追踪」，并在进程内记住失败，避免每次
请求都重试导入与建连。产品看板的数据源是 MySQL，这里只服务排障。
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_REQUIRED = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST")

_broken = False


def enabled() -> bool:
    return all(os.getenv(k, "").strip() for k in _REQUIRED)


def _make_handler():
    from langfuse.langchain import CallbackHandler

    return CallbackHandler()


def build_handler():
    global _broken
    if _broken or not enabled():
        return None
    try:
        return _make_handler()
    except Exception:  # noqa: BLE001 - 追踪失败不许打断对话
        _broken = True
        logger.warning("Langfuse 装配失败，本进程不再追踪", exc_info=True)
        return None


def trace_metadata(conversation_id: str | None, visitor_id: str | None) -> dict:
    """Langfuse 认这几个特殊键，其余键原样存进 trace metadata。"""
    return {
        "langfuse_session_id": conversation_id,
        "langfuse_user_id": visitor_id,
        "langfuse_tags": ["chat"],
        "visitor_id": visitor_id,
    }


def flush() -> None:
    """进程退出前把缓冲里的 trace 推出去；没启用就什么都不做。"""
    if not enabled():
        return
    try:
        from langfuse import Langfuse

        Langfuse().flush()
    except Exception:  # noqa: BLE001 - 退出路径上失败只能记日志
        logger.warning("Langfuse flush 失败", exc_info=True)