"""r15 Task 8：LangGraph StateGraph 组装。

指代消解 → 意图识别 → 四路分流（知识/业务/投诉/闲聊）→ 日志汇合。
会话工作状态经 checkpoint 持久化（只存工作字段，不含消息——历史仍在
MySQL 消息表，每轮作为输入传入）。
"""

from __future__ import annotations

import logging
import time
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages

from app import store
from app.agent import run_business_agent_with_limit
from app.intent import classify_intent
from app.knowledge_node import run_knowledge_retrieval
from app.resolve import needs_resolution, resolve_reference
from app.templates import chitchat_reply, complaint_reply

logger = logging.getLogger(__name__)


class ConversationState(TypedDict, total=False):
    """会话工作状态。消息历史不进 state（设计定稿：MySQL 存消息）。

    - input: 每轮输入（原消息 + 历史摘要），不持久化
    - resolved_query / intent / final_text: 本轮工作产物
    - pending_topic / turn_count / collected_slots: 跨轮槽位，checkpoint 持久化
    """

    message: str
    history: list[dict]
    visitor_id: str
    resolved_query: str
    intent: str
    intent_confidence: float
    intent_method: str
    final_text: str
    node_path: str
    pending_topic: str
    collected_slots: dict
    turn_count: int
    top_score: float | None
    tool_calls: list[str]
    steps: int
    # add_messages 通道：主力 agent 的事件流里 SSE 需要它，但不进持久化判断
    messages: Annotated[list, add_messages]


def build_graph(*, checkpointer: BaseCheckpointSaver | None = None, llm=None):
    """构建对话主图。生产传 get_checkpointer()；测试传 MemorySaver。"""

    # ---------- 节点 ----------

    async def resolve_node(state: ConversationState) -> dict:
        message = state["message"]
        pending = state.get("pending_topic", "")
        if not needs_resolution(message, pending):
            return {
                "resolved_query": message,
                "node_path": "resolve(skip)",
                "turn_count": state.get("turn_count", 0) + 1,
            }
        resolved = await resolve_reference(message, state.get("history", []), llm)
        return {
            "resolved_query": resolved,
            "node_path": "resolve",
            "turn_count": state.get("turn_count", 0) + 1,
        }

    async def classify_node(state: ConversationState) -> dict:
        result = await classify_intent(state["resolved_query"], llm)
        return {
            "intent": result.intent,
            "intent_confidence": result.confidence,
            "intent_method": result.method,
        }

    def _route(state: ConversationState) -> str:
        return state["intent"]

    async def knowledge_node(state: ConversationState) -> dict:
        outcome = await run_knowledge_retrieval(
            state["resolved_query"], state["visitor_id"], llm=llm
        )
        update: dict[str, Any] = {
            "final_text": outcome.final_text,
            "top_score": outcome.max_score,
            "node_path": "knowledge",
        }
        if outcome.insufficient:
            update["pending_topic"] = ""
        else:
            # 未决话题 = 消解后的知识话题，供短消息延续判断
            update["pending_topic"] = state["resolved_query"]
        return update

    async def business_node(state: ConversationState) -> dict:
        result = await run_business_agent_with_limit(
            {"messages": state.get("messages", []) or [("user", state["resolved_query"])]},
            llm=llm,
        )
        return {
            "final_text": result["final_text"],
            "tool_calls": result["tool_calls"],
            "steps": result["steps"],
            "node_path": "business",
            "pending_topic": state["resolved_query"],
        }

    async def complaint_node(state: ConversationState) -> dict:
        text = await complaint_reply(state["visitor_id"])
        return {"final_text": text, "node_path": "complaint", "pending_topic": ""}

    async def chitchat_node(state: ConversationState) -> dict:
        return {"final_text": chitchat_reply(), "node_path": "chitchat", "pending_topic": ""}

    async def log_node(state: ConversationState) -> dict:
        return {"node_path": state.get("node_path", "") + ">log"}

    # ---------- 组装 ----------

    g = StateGraph(ConversationState)
    g.add_node("resolve", resolve_node)
    g.add_node("classify", classify_node)
    g.add_node("knowledge", knowledge_node)
    g.add_node("business", business_node)
    g.add_node("complaint", complaint_node)
    g.add_node("chitchat", chitchat_node)
    g.add_node("log", log_node)

    g.set_entry_point("resolve")
    g.add_edge("resolve", "classify")
    g.add_conditional_edges("classify", _route, {
        "knowledge": "knowledge",
        "business": "business",
        "complaint": "complaint",
        "chitchat": "chitchat",
    })
    for node in ("knowledge", "business", "complaint", "chitchat"):
        g.add_edge(node, "log")
    g.add_edge("log", END)

    return g.compile(checkpointer=checkpointer)
