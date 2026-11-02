"""工具执行引擎：校验 → 鉴权 → 预览/挂起 → 执行 → 审计。

对模型承诺「永不抛异常」：任何失败都归一成 `{"error": ...}`，
调用方（ToolNode）永远拿到一个可读的 dict。确认/取消走 HTTP 接口，
失败用 `ActionError` 带状态码，由接口层翻译成 HTTPException。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from langgraph.prebuilt import ToolRuntime
from pydantic import ValidationError

from app.core import store
from app.tools.context import AgentContext
from app.tools.permission import Caller, Denied, authorize
from app.tools.registry import ToolSpec, registry

logger = logging.getLogger(__name__)


class ActionError(Exception):
    """待确认动作的状态错误，带建议的 HTTP 状态码。"""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


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


def minimal_runtime(visitor_id: str, conversation_id: str = "") -> ToolRuntime:
    """确认路径没有图运行时，构造一个只带访客身份的最小 runtime。"""
    return ToolRuntime(
        state={},
        context=AgentContext(visitor_id=visitor_id),
        config={"configurable": {"thread_id": conversation_id}},
        stream_writer=None,
        tool_call_id="confirm",
        store=None,
    )


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
            try:
                preview = await asyncio.wait_for(
                    self._call(spec.preview, runtime, payload), spec.timeout_s
                )
            except Exception as exc:  # noqa: BLE001 - 预览失败也不能抛给模型
                logger.warning("工具 %s 预览失败", spec.name, exc_info=True)
                self._audit(spec, meta, "error", started, args=payload, error=exc)
                return {"error": "工具暂时不可用，请稍后重试"}
            if isinstance(preview, dict) and preview.get("ok") is False:
                self._audit(spec, meta, "ok", started, args=payload, result=preview)
                return preview

        if spec.requires_confirmation:
            return self._suspend(spec, meta, payload, started)

        return await self._execute(spec, runtime, payload, meta, started, caller="agent")

    async def _execute(
        self,
        spec: ToolSpec,
        runtime: ToolRuntime | None,
        payload: dict,
        meta: RuntimeMeta,
        started: float,
        *,
        caller: str,
        action_id: str | None = None,
    ) -> dict:
        try:
            result = await asyncio.wait_for(
                self._call(spec.impl, runtime, payload), spec.timeout_s
            )
        except (TimeoutError, asyncio.TimeoutError):
            self._audit(
                spec, meta, "error", started, args=payload, caller=caller,
                action_id=action_id, error="TimeoutError: 工具执行超时",
            )
            return {"error": "工具执行超时，请稍后重试"}
        except Exception as exc:  # noqa: BLE001 - 工具异常对模型不可见
            logger.warning("工具 %s 执行失败", spec.name, exc_info=True)
            self._audit(
                spec, meta, "error", started, args=payload, caller=caller,
                action_id=action_id, error=exc,
            )
            return {"error": "工具暂时不可用，请稍后重试"}

        normalized = result if isinstance(result, dict) else {"result": result}
        self._audit(
            spec, meta, "ok", started, args=payload, result=normalized,
            caller=caller, action_id=action_id,
        )
        return normalized

    def _suspend(
        self, spec: ToolSpec, meta: RuntimeMeta, payload: dict, started: float
    ) -> dict:
        """需确认的工具：建待确认动作，不执行。"""
        summary = spec.summarize(payload) if spec.summarize else default_summary(spec, payload)
        action = store.create_pending_action(
            visitor_id=meta.visitor_id or "",
            tool_name=spec.name,
            args=payload,
            summary=summary,
            conversation_id=meta.conversation_id or "",
        )
        self._audit(
            spec, meta, "pending", started, args=payload,
            result={"action_id": action["id"]},
            action_id=action["id"],
        )
        return {
            "pending_action": {
                "id": action["id"],
                "tool": spec.name,
                "label": spec.label,
                "summary": summary,
                "expires_at": action["expires_at"],
            },
            "note": "已生成待确认卡片，请顾客核对后点击确认。"
            "此时尚未提交，不要宣称已完成。",
        }

    async def confirm(self, action_id: str, *, visitor_id: str) -> dict:
        """顾客点确认：重放工具实现，结果回填。"""
        action = self._load(action_id, visitor_id)
        spec = registry.get(action["tool_name"])
        if spec is None:
            raise ActionError(409, "该动作对应的工具已下线")

        meta = RuntimeMeta(action["conversation_id"] or None, visitor_id)
        started = time.perf_counter()
        runtime = minimal_runtime(visitor_id, action["conversation_id"] or "")
        result = await self._execute(
            spec, runtime, action["args_json"], meta, started,
            caller="confirm", action_id=action_id,
        )
        if "error" in result:
            # 执行失败不改终态，顾客可再点一次；错误已进审计
            return result

        store.resolve_pending_action(action_id, "confirmed", result=result)
        return result

    async def cancel(self, action_id: str, *, visitor_id: str) -> dict:
        action = self._load(action_id, visitor_id)
        spec = registry.get(action["tool_name"])
        if spec is None:
            raise ActionError(409, "该动作对应的工具已下线")

        store.resolve_pending_action(action_id, "cancelled")
        meta = RuntimeMeta(action["conversation_id"] or None, visitor_id)
        self._audit(
            spec, meta, "cancelled", time.perf_counter(), args=action["args_json"],
            caller="confirm", action_id=action_id,
        )
        return {"status": "cancelled", "action_id": action_id}

    def _load(self, action_id: str, visitor_id: str) -> dict:
        """取动作并做归属与状态校验。不存在与不属于本人合并成 404，防探测。"""
        action = store.get_pending_action(action_id)
        if action is None or action["visitor_id"] != visitor_id:
            raise ActionError(404, "待确认动作不存在")
        if action["status"] != "pending":
            raise ActionError(409, f"该动作已{_status_text(action['status'])}，无需再次操作")
        if datetime.fromisoformat(action["expires_at"]) <= store.now():
            store.resolve_pending_action(action_id, "expired")
            raise ActionError(410, "待确认动作已过期，请重新发起")
        return action

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
        caller: str = "agent",
        action_id: str | None = None,
    ) -> None:
        try:
            store.record_tool_audit(
                spec.name,
                spec.source,
                caller,
                status,
                conversation_id=meta.conversation_id,
                visitor_id=meta.visitor_id,
                args=args,
                result=result,
                error=error,
                duration_ms=int((time.perf_counter() - started) * 1000),
                action_id=action_id,
            )
        except Exception:  # noqa: BLE001 - 审计绝不影响返回
            logger.warning("审计写入失败", exc_info=True)


def _status_text(status: str) -> str:
    return {
        "confirmed": "确认",
        "cancelled": "取消",
        "expired": "过期",
    }.get(status, "处理")


def default_summary(spec: ToolSpec, payload: dict) -> dict:
    """没有自定义 summary 时的兜底卡片：字段名直接用入参键。"""
    return {
        "title": spec.label,
        "fields": [{"k": k, "v": str(v)} for k, v in payload.items() if v is not None],
        "note": "确认后才会真正提交，可随时取消",
    }


executor = Executor()