"""r15 Task 8：LangGraph StateGraph 组装。

指代消解 → 意图识别 → 四路分流（知识/业务/投诉/闲聊）→ 日志汇合。
会话工作状态经 checkpoint 持久化（只存工作字段，不含消息——历史仍在
MySQL 消息表，每轮作为输入传入）。
"""

from __future__ import annotations

import logging
from typing import Any, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, StateGraph

from app import store
from app.agent import run_business_agent_with_limit
from app.intent import classify_intent
from app.knowledge_node import run_knowledge_retrieval
from app.resolve import needs_resolution, resolve_reference
from app.templates import chitchat_reply, complaint_reply

logger = logging.getLogger(__name__)


class _TurnInput(TypedDict, total=False):
    """每轮输入。LangGraph 的 state 是「本轮全量输入+产出」的通道集合，
    这些字段由 invoke 传入、被节点读取，不跨轮累积。"""

    message: str
    history: list[dict]
    visitor_id: str


class _CrossTurnSlots(TypedDict, total=False):
    """跨轮槽位。checkpoint 持久化——这是会话恢复的唯一来源。"""

    pending_topic: str
    collected_slots: dict
    turn_count: int


class _TurnOutput(TypedDict, total=False):
    """本轮工作产物，节点逐级写入。"""

    resolved_query: str
    intent: str
    intent_confidence: float
    intent_method: str
    final_text: str
    node_path: str


class _Observability(TypedDict, total=False):
    """观测数据，喂 agent_traces（数据飞轮），不参与业务流程。"""

    top_score: float | None
    tool_calls: list[str]
    steps: int
    total_tokens: int | None
    stopped_by: str | None
    # r16 改写观测：词条与是否回退
    rewritten_terms: list[str]
    rewritten_query: str
    fallback_used: bool


# 多继承只做代码组织：LangGraph 按 __annotations__ 解析 schema，合并后
# 仍是 flat channel，节点读写方式与分组前完全一致
class ConversationState(
    _TurnInput, _CrossTurnSlots, _TurnOutput, _Observability, total=False
):
    """会话工作状态。消息历史不进 state（设计定稿：MySQL 存消息）。"""


def build_graph(
    *,
    checkpointer: BaseCheckpointSaver | None = None,
    llm=None,
    llm_quiet=None,
):
    """构建对话主图。

    llm：流式实例，供主力 agent（SSE 逐 token 转发）。
    llm_quiet：非流式实例，供消解/意图兜底/知识生成——这些调用的中间
    输出不能冒泡成 delta（见 app/llm.py get_llm_quiet）。
    生产传 get_checkpointer()；测试传 MemorySaver。
    """

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
        resolved = await resolve_reference(
            message, state.get("history", []), llm_quiet or llm
        )
        return {
            "resolved_query": resolved,
            "node_path": "resolve",
            "turn_count": state.get("turn_count", 0) + 1,
        }

    async def classify_node(state: ConversationState) -> dict:
        result = await classify_intent(state["resolved_query"], llm_quiet or llm)
        return {
            "intent": result.intent,
            "intent_confidence": result.confidence,
            "intent_method": result.method,
        }

    def _route(state: ConversationState) -> str:
        return state["intent"]

    async def knowledge_node(state: ConversationState) -> dict:
        outcome = await run_knowledge_retrieval(
            state["resolved_query"], state["visitor_id"], llm=llm_quiet or llm
        )
        update: dict[str, Any] = {
            "final_text": outcome.final_text,
            "top_score": outcome.max_score,
            "node_path": "knowledge",
            "rewritten_terms": outcome.rewritten_terms,
            "rewritten_query": outcome.rewritten_query,
            "fallback_used": outcome.fallback_used,
        }
        if outcome.insufficient:
            update["pending_topic"] = ""
        else:
            # 未决话题 = 消解后的知识话题，供短消息延续判断
            update["pending_topic"] = state["resolved_query"]
        return update

    async def business_node(state: ConversationState) -> dict:
        # r18 接线：梗概 + 预算内原文 + 本轮，作为完整对话历史给主力 agent。
        # history 已由入口组装（首位可能是梗概条目），本轮消息放最后
        messages = [
            (m["role"], m["content"]) for m in state.get("history", [])
        ]
        messages.append(("user", state["resolved_query"]))
        result = await run_business_agent_with_limit(
            {"messages": messages},
            visitor_id=state["visitor_id"],
            llm=llm,
        )
        return {
            "final_text": result["final_text"],
            "tool_calls": result["tool_calls"],
            "steps": result["steps"],
            "total_tokens": result.get("total_tokens"),
            "stopped_by": result["stopped_by"],
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
