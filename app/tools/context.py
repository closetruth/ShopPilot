"""工具运行时上下文：模型不可见，由 ToolRuntime 注入。"""

from dataclasses import dataclass


@dataclass
class AgentContext:
    visitor_id: str