"""工具注册中心：ToolSpec 元数据 + 进程内注册表 + 包装工具构造。

模型能看到的所有工具都由这里生成：包装函数保留 `runtime` 签名让
ToolNode 照常注入，`args_schema` 显式传入让模型可见 schema 与业务参数
完全一致。调用统一落到执行引擎。
"""

import inspect
import logging
from dataclasses import dataclass
from typing import Any, Callable, Literal

from langchain_core.tools import BaseTool, StructuredTool
from langgraph.prebuilt import ToolRuntime
from pydantic import BaseModel

from app.tools.context import AgentContext
from app.tools.permission import CallerKind

logger = logging.getLogger(__name__)

Scope = Literal["none", "visitor", "admin"]
Risk = Literal["read", "write", "destructive"]
Source = Literal["builtin", "mcp"]

DEFAULT_TIMEOUT_S = 10.0


@dataclass
class ToolSpec:
    name: str
    label: str
    description: str
    args_schema: type[BaseModel]
    impl: Callable[..., Any]
    scope: Scope = "none"
    risk: Risk = "read"
    requires_confirmation: bool = False
    allowed_callers: frozenset[CallerKind] = frozenset({"agent"})
    preview: Callable[..., Any] | None = None
    summarize: Callable[[dict], dict] | None = None
    source: Source = "builtin"
    server: str | None = None
    timeout_s: float = DEFAULT_TIMEOUT_S

    def __post_init__(self) -> None:
        self.allowed_callers = frozenset(self.allowed_callers)
        if not self.allowed_callers:
            raise ValueError(f"工具 {self.name} 的调用者白名单不能为空")
        if self.risk == "destructive" and not self.requires_confirmation:
            raise ValueError(
                f"工具 {self.name} 标记为 destructive，必须同时要求顾客确认"
            )


class RegisteredTool(StructuredTool):
    """在 LangChain 默认解析上加两处修正。

    1. 注入参数先摘出来再校验：内置工具的 args_schema 用 `extra="forbid"`，
       若把 ToolNode 注入的 `runtime` 一起送进 pydantic 会被判成多余字段。
    2. 空入参工具（`NoArgs`）不能走默认短路：默认实现在 args_schema 没有
       字段时直接返回空 kwargs，注入的 `runtime` 会一起丢掉。
    """

    def _to_args_and_kwargs(self, tool_input, tool_call_id=None):
        if isinstance(tool_input, dict):
            injected = {
                k: v for k, v in tool_input.items() if k in self._injected_args_keys
            }
            clean = {
                k: v for k, v in tool_input.items() if k not in injected
            }
            parsed = self._parse_input(clean, tool_call_id)
            if isinstance(parsed, dict):
                return (), {**parsed, **injected}
        return super()._to_args_and_kwargs(tool_input, tool_call_id)


def build_tool(spec: ToolSpec) -> BaseTool:
    async def entry(runtime: ToolRuntime[AgentContext], **kwargs) -> dict:
        # 延迟导入：执行引擎需要 ToolSpec，顶层导入会形成循环
        from app.tools.executor import executor

        return await executor.invoke(spec, runtime, kwargs)

    return RegisteredTool.from_function(
        coroutine=entry,
        name=spec.name,
        description=spec.description,
        args_schema=spec.args_schema,
    )


class Registry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._tools: dict[str, BaseTool] = {}

    def register(self, spec: ToolSpec, *, override: bool = False) -> ToolSpec:
        if spec.name in self._specs and not override:
            raise ValueError(f"工具名重复：{spec.name}")
        self._specs[spec.name] = spec
        self._tools[spec.name] = build_tool(spec)
        return spec

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def tool(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def all(self) -> list[ToolSpec]:
        return list(self._specs.values())

    def tools_for(self, caller: CallerKind) -> list[BaseTool]:
        return [
            self._tools[spec.name]
            for spec in self.all()
            if caller in spec.allowed_callers
        ]

    def clear(self) -> None:
        self._specs.clear()
        self._tools.clear()


registry = Registry()


def registered(
    *,
    name: str,
    label: str,
    args_schema: type[BaseModel],
    scope: Scope = "none",
    risk: Risk = "read",
    requires_confirmation: bool = False,
    allowed_callers: tuple[CallerKind, ...] = ("agent",),
    preview: Callable[..., Any] | None = None,
    summarize: Callable[[dict], dict] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    source: Source = "builtin",
    server: str | None = None,
) -> Callable[[Callable[..., Any]], BaseTool]:
    """把实现函数登记成工具，返回模型可见的包装工具。"""

    def decorate(impl: Callable[..., Any]) -> BaseTool:
        spec = ToolSpec(
            name=name,
            label=label,
            description=(inspect.getdoc(impl) or name).strip(),
            args_schema=args_schema,
            impl=impl,
            scope=scope,
            risk=risk,
            requires_confirmation=requires_confirmation,
            allowed_callers=allowed_callers,
            preview=preview,
            summarize=summarize,
            timeout_s=timeout_s,
            source=source,
            server=server,
        )
        registry.register(spec)
        return registry.tool(name)

    return decorate