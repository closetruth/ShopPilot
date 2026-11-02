"""用量回调：每次模型调用落一行 `llm_usage`。

`node` 归因取 metadata["langgraph_node"]——LangGraph 在每个节点执行时把它
塞进 config metadata；`source` 由调用方在 config.metadata 里声明
`shoppilot_source`。取不到就退化为 unknown，不许抛错。
"""

from __future__ import annotations

import logging
import os
import time

from langchain_core.callbacks import BaseCallbackHandler

logger = logging.getLogger(__name__)


def price_input() -> float:
    """输入价目，单位：元 / 百万 token。"""
    return _float_env("LLM_PRICE_INPUT_PER_MTOK")


def price_output() -> float:
    return _float_env("LLM_PRICE_OUTPUT_PER_MTOK")


def price_configured() -> bool:
    """价目未配置时成本恒为 0，看板要能区分「没配」与「真的零成本」。"""
    return price_input() > 0 or price_output() > 0


def cost_micros(input_tokens: int, output_tokens: int) -> int:
    """价目是元/百万 token，token 数 × 价目 直接等于微元。"""
    return int(
        round(input_tokens * price_input() + output_tokens * price_output())
    )


def build_usage_handler(
    *, conversation_id: str | None = None, visitor_id: str | None = None, source: str = "chat"
) -> "UsageCallbackHandler":
    return UsageCallbackHandler(
        conversation_id=conversation_id, visitor_id=visitor_id, source=source
    )


class UsageCallbackHandler(BaseCallbackHandler):
    """按模型调用记一行用量。失败只记日志，绝不影响对话。"""

    def __init__(
        self,
        *,
        conversation_id: str | None = None,
        visitor_id: str | None = None,
        source: str = "chat",
    ) -> None:
        self._starts: dict = {}
        self._conversation_id = conversation_id
        self._visitor_id = visitor_id
        self._source = source
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cost_micros = 0

    def on_chat_model_start(
        self, serialized, messages, *, run_id, metadata=None, **kwargs
    ) -> None:
        self._starts[run_id] = (
            time.monotonic(),
            metadata or {},
            _model_name(serialized),
        )

    def on_llm_end(self, response, *, run_id, **kwargs) -> None:
        started = self._starts.pop(run_id, None)
        if started is None:
            return
        at, meta, model = started
        inp, out = _usage_of(response)
        if inp == 0 and out == 0:
            return

        cost = cost_micros(inp, out)
        self.total_input_tokens += inp
        self.total_output_tokens += out
        self.total_cost_micros += cost

        node = meta.get("langgraph_node") or "unknown"
        source = meta.get("shoppilot_source") or self._source
        conversation_id = meta.get("shoppilot_conversation_id") or self._conversation_id
        visitor_id = meta.get("shoppilot_visitor_id") or self._visitor_id
        latency_ms = int((time.monotonic() - at) * 1000)

        try:
            from app.repo import usage as usage_repo

            usage_repo.record(
                conversation_id,
                visitor_id,
                source,
                node,
                model,
                inp,
                out,
                cost,
                latency_ms,
            )
        except Exception:  # noqa: BLE001 - 观测失败不许打断对话
            logger.warning("用量落库失败", exc_info=True)


def _usage_of(response) -> tuple[int, int]:
    """先读 message.usage_metadata，再退化到 llm_output.token_usage。"""
    try:
        message = response.generations[0][0].message
        usage = getattr(message, "usage_metadata", None)
        if usage:
            return int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
    except (AttributeError, IndexError, TypeError):
        pass

    llm_output = getattr(response, "llm_output", None) or {}
    token_usage = llm_output.get("token_usage") or llm_output.get("usage") or {}
    if token_usage:
        return (
            int(token_usage.get("prompt_tokens") or 0),
            int(token_usage.get("completion_tokens") or 0),
        )
    return 0, 0


def _model_name(serialized) -> str:
    if not isinstance(serialized, dict):
        return "unknown"
    for key in ("name", "model", "model_name"):
        value = serialized.get(key)
        if isinstance(value, str) and value:
            return value
    kwargs = serialized.get("kwargs")
    if isinstance(kwargs, dict):
        for key in ("model", "model_name"):
            value = kwargs.get(key)
            if isinstance(value, str) and value:
                return value
    return "unknown"


def _float_env(name: str) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return 0.0
    try:
        return float(raw)
    except ValueError:
        logger.warning("环境变量 %s 不是数字：%r", name, raw)
        return 0.0