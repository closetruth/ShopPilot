"""r20 Task 1：消息 id 必须能从 SSE 与历史接口拿到。

反馈要绑到具体某条回答，没有稳定 message_id 就没有锚点。
"""

import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    from langgraph.checkpoint.memory import MemorySaver

    from app.api import _graph as graph_singleton
    from app.chat.graph import build_graph

    graph = build_graph(checkpointer=MemorySaver(), llm=None, llm_quiet=None)
    monkeypatch.setattr(graph_singleton, "_compiled_graph", graph)

    from app.main import app

    with TestClient(app) as c:
        yield c


def _events(body: str) -> list[str]:
    return [line[6:] for line in body.splitlines() if line.startswith("data: ")]


def _chat(client, message: str, visitor_id: str) -> list[str]:
    with client.stream(
        "POST", "/api/chat", json={"message": message, "visitor_id": visitor_id}
    ) as resp:
        body = "".join(resp.iter_text())
    return _events(body)


def test_sse_pushes_message_id_before_done(client):
    events = _chat(client, "你好", "msg-id-v1")
    assert events[0].startswith("{")
    assert json.loads(events[0])["conversation_id"]
    assert events[-1] == "[DONE]"
    assert "message_id" in json.loads(events[-2])


def test_history_items_carry_id(client):
    events = _chat(client, "你好", "msg-id-v2")
    conv_id = json.loads(events[0])["conversation_id"]
    data = client.get(
        "/api/history", params={"conversation_id": conv_id, "visitor_id": "msg-id-v2"}
    ).json()
    assert data["messages"], "这一轮必须落了两条消息"
    assert all(isinstance(m["id"], int) for m in data["messages"])
    assert data["messages"][-1]["role"] == "assistant"