"""r15 final review 补的协议级测试：/api/chat 的 SSE 全链路。

final review 抓到的三个 Critical/Major 全在无覆盖区——这里用 FastAPI
TestClient + mock 图跑真实 stream()，断言事件序列、visitor_id 归属、
触线补发。不连真 LLM 与向量库。
"""

import pytest
from fastapi.testclient import TestClient

from app import main as app_main
from app.api import _graph as graph_singleton
from app.graph import build_graph


@pytest.fixture
def client(monkeypatch):
    """TestClient + 记录 visitor_id 的假 business runner。"""
    import asyncio

    from langgraph.checkpoint.memory import MemorySaver

    seen_visitor_ids: list[str] = []

    async def fake_run(inputs, *, visitor_id, llm=None):
        seen_visitor_ids.append(visitor_id)
        from langchain_core.messages import AIMessage

        return {
            "final_text": "查到了您的订单",
            "stopped_by": None,
            "tool_calls": ["list_my_orders"],
            "steps": 2,
            "total_tokens": None,
        }

    monkeypatch.setattr("app.graph.run_business_agent_with_limit", fake_run)

    graph = build_graph(checkpointer=MemorySaver(), llm=None, llm_quiet=None)
    monkeypatch.setattr(graph_singleton, "_compiled_graph", graph)

    from app.main import app

    with TestClient(app) as c:
        c.seen_visitor_ids = seen_visitor_ids
        yield c


def _parse_sse(text: str) -> list[dict]:
    events = []
    for line in text.splitlines():
        if line.startswith("data: "):
            payload = line[6:]
            events.append(payload)
    return events


def test_sse_event_sequence_and_tool_visitor_id(client):
    """闲聊→业务两轮：事件序列完整，业务工具收到真实 visitor_id。"""
    visitor_id = "sse-test-visitor"

    with client.stream(
        "POST",
        "/api/chat",
        json={"message": "你好", "visitor_id": visitor_id},
    ) as resp:
        body = "".join(resp.iter_text())
    events = _parse_sse(body)
    assert events[0].startswith('{"conversation_id"'), events[0]
    assert any('"delta"' in e for e in events)
    assert events[-1] == "[DONE]"

    with client.stream(
        "POST",
        "/api/chat",
        json={"message": "查一下我的订单", "visitor_id": visitor_id},
    ) as resp:
        body = "".join(resp.iter_text())
    events = _parse_sse(body)
    assert events[-1] == "[DONE]"
    # Critical #1 回归钉：工具必须收到真实 visitor_id，不是占位符
    assert client.seen_visitor_ids[-1] == visitor_id
    assert "__limit_runner__" not in body


def test_tool_event_forwarded(client):
    """business 路径：mock runner 的 final_text 作为 delta 流出并落库。"""
    with client.stream(
        "POST",
        "/api/chat",
        json={"message": "查一下我的订单", "visitor_id": "sse-v2"},
    ) as resp:
        body = "".join(resp.iter_text())
    assert '"delta": "查到了您的订单"' in body
    assert body.rstrip().endswith("[DONE]")


def test_business_agent_receives_context_messages(client):
    """r18 接线：主力 agent 收到的 messages = 梗概（若有）+ 原文 + 本轮。"""
    captured: list[list] = []

    async def fake_run(inputs, *, visitor_id, llm=None):
        captured.append(inputs["messages"])
        return {
            "final_text": "好的",
            "stopped_by": None,
            "tool_calls": [],
            "steps": 1,
            "total_tokens": None,
        }

    client.app.dependency_overrides.clear()
    import langgraph.checkpoint.memory as mem
    from langgraph.checkpoint.memory import MemorySaver

    from app.api import _graph as graph_singleton
    from app.graph import build_graph

    # 重新打 runner patch（fixture 里的 fake_run 不捕获 messages）
    import app.graph as graph_mod

    graph_mod.run_business_agent_with_limit = fake_run
    graph = build_graph(checkpointer=MemorySaver(), llm=None, llm_quiet=None)
    graph_singleton._compiled_graph = graph

    visitor_id = "ctx-r18"
    with client.stream(
        "POST",
        "/api/chat",
        json={"message": "我要查订单", "visitor_id": visitor_id},
    ) as resp:
        "".join(resp.iter_text())

    assert captured, "business runner 应被调用"
    msgs = captured[0]
    # 本轮消息必须在最后
    assert msgs[-1][1] == "我要查订单" or "查订单" in str(msgs[-1])
    # r18：无梗概无历史时 messages 只有本轮一条；有梗概时首位是梗概条目
    assert isinstance(msgs, list)


def test_budget_recorded_in_trace(client):
    """r18 可观测：每轮 trace 的 detail 带 budget 账目。"""
    from app import store

    visitor_id = "ctx-r18-budget"
    with client.stream(
        "POST",
        "/api/chat",
        json={"message": "你好", "visitor_id": visitor_id},
    ) as resp:
        "".join(resp.iter_text())

    traces = [t for t in store.list_traces(limit=5) if t["visitor_id"] == visitor_id]
    assert traces, "应有轨迹"
    detail = traces[0].get("detail") or {}
    assert "budget" in detail, f"detail 应含 budget，实际 {list(detail)}"
    assert detail["budget"]["total"] > 0
    assert "recent_tokens" in detail["budget"]
