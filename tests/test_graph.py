"""r15 Task 8：StateGraph 组装的验证测试。

MemorySaver 构造（不连真 checkpoint），llm 与向量层全部 mock——
验证的是分流正确性、state 流转与 node_path 记录，不是检索质量。
"""

import pytest

from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver

from app.graph import build_graph


class ScriptedLLM:
    """按调用顺序回放预设回复的 LLM 替身。

    create_react_agent 要求 Runnable + bind_tools，用 GenericFakeChatModel
    变体；消解与意图分类只需要 ainvoke。
    """

    def __init__(self, replies: list[str]):
        self._replies = list(replies)
        self.prompts: list = []

    def bind_tools(self, tools, **kwargs):
        return self

    async def ainvoke(self, messages, **kwargs):
        from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

        self.prompts.append(messages)
        content = self._replies.pop(0) if self._replies else "{}"

        # 走一遍 GenericFakeChatModel 以拿到 AIMessage——但要支持内容注入，
        # 直接构造 AIMessage 更简单
        return AIMessage(content=content)


def _mock_vector(monkeypatch, hits: list[dict]):
    from app import vector_store

    def fake_search(query, visitor_id, top_k=3):
        return hits

    monkeypatch.setattr(vector_store, "search", fake_search)


@pytest.fixture
def memory_graph():
    return build_graph(checkpointer=MemorySaver())


@pytest.mark.anyio
async def test_chitchat_route_no_llm(memory_graph):
    """「你好」走闲聊模板：零 LLM 分类调用，node_path 含 chitchat。"""
    config = {"configurable": {"thread_id": "t-chitchat"}}
    result = await memory_graph.ainvoke(
        {"message": "你好", "history": [], "visitor_id": "v1"}, config=config
    )
    assert "订单" in result["final_text"]
    assert "chitchat" in result["node_path"]


@pytest.mark.anyio
async def test_complaint_route(memory_graph):
    """「我要投诉你们」走投诉模板，node_path 含 complaint。"""
    config = {"configurable": {"thread_id": "t-complaint"}}
    result = await memory_graph.ainvoke(
        {"message": "我要投诉你们", "history": [], "visitor_id": "v1"}, config=config
    )
    assert "人工" in result["final_text"]
    assert "complaint" in result["node_path"]


@pytest.mark.anyio
async def test_knowledge_route(memory_graph, monkeypatch):
    """知识类消息检索命中后带引用编号。"""
    _mock_vector(monkeypatch, [
        {"source_id": "P-RET-001-3", "score": 0.83, "text": "退回运费由消费者承担"}
    ])

    # 规则直接命中 knowledge，不走 LLM 分类
    config = {"configurable": {"thread_id": "t-knowledge"}}
    result = await memory_graph.ainvoke(
        {"message": "退货运费谁出", "history": [], "visitor_id": "v1"}, config=config
    )
    assert "[P-" in result["final_text"]
    assert "knowledge" in result["node_path"]


@pytest.mark.anyio
async def test_state_persisted_via_checkpoint(memory_graph):
    """同一 thread 跑两轮，第二轮能读到第一轮写的 turn_count。"""
    config = {"configurable": {"thread_id": "t-persist"}}
    r1 = await memory_graph.ainvoke(
        {"message": "你好", "history": [], "visitor_id": "v1"}, config=config
    )
    r2 = await memory_graph.ainvoke(
        {"message": "在吗", "history": [], "visitor_id": "v1"}, config=config
    )
    assert r1["turn_count"] == 1
    assert r2["turn_count"] == 2
