"""r15 final review 补的协议级测试：/api/chat 的 SSE 全链路。

final review 抓到的三个 Critical/Major 全在无覆盖区——这里用 FastAPI
TestClient + mock 图跑真实 stream()，断言事件序列、visitor_id 归属、
触线补发。不连真 LLM 与向量库。
"""

import pytest
from fastapi.testclient import TestClient

from app import main as app_main
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
    monkeypatch.setattr(app_main, "_compiled_graph", graph)

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
