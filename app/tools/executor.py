"""工具执行引擎：校验 → 鉴权 → 预览/挂起 → 执行 → 审计。

对模型承诺「永不抛异常」：任何失败都归一成 `{"error": ...}`，
调用方（ToolNode）永远拿到一个可读的 dict。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass
from typing import Any

from langgraph.prebuilt import ToolRuntime
from pydantic import ValidationError

from app import store
from app.tools.permission import Caller, Denied, authorize
from app.tools.registry import ToolSpec

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimeMeta:
    """从 ToolRuntime 里抽出审计需要的两个字段。"""

    conversation_id: str | None
    visitor_id: str | None


def runtime_meta(runtime: ToolRuntime | None) -> RuntimeMeta:
    if runtime is None:
        return RuntimeMeta(None, None)
    config = getattr(runtime, "config", None) or {}
    configurable = config.get("configurable") if isinstance(config, dict) else None
    conversation_id = (configurable or {}).get("thread_id")
    context = getattr(runtime, "context", None)
    visitor_id = getattr(context, "visitor_id", None)
    return RuntimeMeta(conversation_id, visitor_id)


def validation_error(exc: ValidationError) -> dict:
    detail = "; ".join(
        f"{'.'.join(str(p) for p in err['loc']) or '参数'}: {err['msg']}"
        for err in exc.errors()
    )
    return {"error": f"参数不合法（{detail}）。请修正后重试。"}


class Executor:
    async def invoke(
        self,
        spec: ToolSpec,
        runtime: ToolRuntime | None,
        raw_args: dict[str, Any],
    ) -> dict:
        meta = runtime_meta(runtime)
        started = time.perf_counter()

        try:
            args = spec.args_schema.model_validate(raw_args)
        except ValidationError as exc:
            self._audit(spec, meta, "error", started, args=raw_args, error=exc)
            return validation_error(exc)

        caller = Caller(kind="agent", visitor_id=meta.visitor_id)
        try:
            authorize(spec, caller)
        except Denied as exc:
            self._audit(spec, meta, "denied", started, args=raw_args, error=exc)
            return {"error": f"该操作不被允许：{exc}"}

        payload = args.model_dump()

        if spec.preview is not None:
            preview = await self._call(spec.preview, runtime, payload)
            if isinstance(preview, dict) and preview.get("ok") is False:
                self._audit(spec, meta, "ok", started, args=payload, result=preview)
                return preview

        if spec.requires_confirmation:
            self._audit(
                spec, meta, "error", started, args=payload,
                error="待确认动作链路尚未接入",
            )
            return {"error": "该操作需要顾客确认，确认链路暂不可用，请转人工处理。"}

        try:
            result = await asyncio.wait_for(
                self._call(spec.impl, runtime, payload), spec.timeout_s
            )
        except (TimeoutError, asyncio.TimeoutError):
            self._audit(
                spec, meta, "error", started, args=payload,
                error="TimeoutError: 工具执行超时",
            )
            return {"error": "工具执行超时，请稍后重试"}
        except Exception as exc:  # noqa: BLE001 - 工具异常对模型不可见
            logger.warning("工具 %s 执行失败", spec.name, exc_info=True)
            self._audit(spec, meta, "error", started, args=payload, error=exc)
            return {"error": "工具暂时不可用，请稍后重试"}

        normalized = result if isinstance(result, dict) else {"result": result}
        self._audit(spec, meta, "ok", started, args=payload, result=normalized)
        return normalized

    async def _call(self, fn: Any, runtime: ToolRuntime | None, args: dict) -> Any:
        if inspect.iscoroutinefunction(fn):
            return await fn(runtime, **args)
        return await asyncio.to_thread(fn, runtime, **args)

    def _audit(
        self,
        spec: ToolSpec,
        meta: RuntimeMeta,
        status: str,
        started: float,
        *,
        args: dict | None = None,
        result: Any = None,
        error: Any = None,
    ) -> None:
        try:
            store.record_tool_audit(
                spec.name,
                spec.source,
                "agent",
                status,
                conversation_id=meta.conversation_id,
                visitor_id=meta.visitor_id,
                args=args,
                result=result,
                error=error,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        except Exception:  # noqa: BLE001 - 审计绝不影响返回
            logger.warning("审计写入失败", exc_info=True)


executor = Executor()