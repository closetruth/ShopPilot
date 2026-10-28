"""客服 Agent：LangGraph ReAct 循环，模型自主决定是否调用工具。"""

from __future__ import annotations

from langgraph.prebuilt import create_react_agent

from app.llm import get_llm
from app.prompt import SYSTEM_PROMPT
from app.tools import TOOLS, AgentContext


def build_agent():
    return create_react_agent(
        model=get_llm(),
        tools=TOOLS,
        prompt=SYSTEM_PROMPT,
        context_schema=AgentContext,
    )
