"""工具注册中心包。

对外只暴露三样：`AgentContext`（图运行时上下文）、`registry`（注册表）、
`agent_tools()`（给 agent 用的已包装工具列表）。导入本包即完成内置工具注册。
"""

from __future__ import annotations

from langchain_core.tools import BaseTool

from app.tools import builtin as _builtin  # noqa: F401 - 副作用：注册内置工具
from app.tools.context import AgentContext
from app.tools.permission import Caller, Denied, authorize
from app.tools.registry import ToolSpec, registered, registry

__all__ = [
    "AgentContext",
    "Caller",
    "Denied",
    "ToolSpec",
    "agent_tools",
    "authorize",
    "registered",
    "registry",
    "tool_label",
    "tool_labels",
]


def agent_tools() -> list[BaseTool]:
    """当前注册表中允许 agent 调用的工具，顺序即注册顺序。"""
    return registry.tools_for("agent")


def tool_labels() -> dict[str, str]:
    """工具名 → 中文短名，供 SSE 事件与前端显示。"""
    return {spec.name: spec.label for spec in registry.all()}


def tool_label(name: str) -> str:
    spec = registry.get(name)
    return spec.label if spec else name