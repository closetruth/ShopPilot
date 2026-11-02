"""r20 Task 5：三层成本闸。全部软闸——降级话术，不返 4xx。"""

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app.core import store
from app.cost import budget
from app.repo import budget as budget_repo
from app.repo import usage as usage_repo


def test_limits_zero_means_off(monkeypatch):
    monkeypatch.setenv("CONVERSATION_DAILY_TOKEN_BUDGET", "0")
    monkeypatch.setenv("GLOBAL_DAILY_COST_YUAN", "0")
    assert budget.conversation_limit() == 0
    assert budget.global_limit_micros() == 0
    assert budget.global_blocked() is False
    assert budget.conversation_over_budget("any") is False


def test_global_blocked_by_accumulated_cost(monkeypatch):
    """今日成本累计达到上限即判定触闸。"""
    monkeypatch.setenv("GLOBAL_DAILY_COST_YUAN", "0.000001")
    usage_repo.record(None, None, "chat", "knowledge", "m", 100, 100, 2, 10)
    assert budget.global_blocked() is True


def test_conversation_budget_counts_today_only(monkeypatch):
    monkeypatch.setenv("CONVERSATION_DAILY_TOKEN_BUDGET", "50")
    conv = store.create_conversation("bg-" + uuid.uuid4().hex[:8])
    usage_repo.record(conv, "bg", "chat", "knowledge", "m", 30, 30, 0, 10)
    assert budget.conversation_over_budget(conv) is True


def test_agent_token_limit_reads_env(monkeypatch):
    monkeypatch.setenv("TURN_TOKEN_LIMIT", "6000")
    import importlib

    from app.chat import agent

    importlib.reload(agent)
    assert agent.TOKEN_LIMIT == 6000
    assert agent.STEP_LIMIT == 15

    # 还原模块级常量，避免污染同进程内后续用例
    monkeypatch.delenv("TURN_TOKEN_LIMIT")
    importlib.reload(agent)
    assert agent.TOKEN_LIMIT == 8000


def test_blocked_chat_returns_soft_text(monkeypatch):
    """触闸后照常回 200 + 话术，不返 429；事件必须落库。"""
    from langgraph.checkpoint.memory import MemorySaver

    from app.api import _graph as graph_singleton
    from app.chat.graph import build_graph
    from app.main import app

    graph_singleton.set_graph_for_testing(
        build_graph(checkpointer=MemorySaver(), llm=None, llm_quiet=None)
    )
    monkeypatch.setattr(budget, "global_blocked", lambda: True)

    visitor = "gate-" + uuid.uuid4().hex[:8]
    with TestClient(app) as client:
        with client.stream(
            "POST", "/api/chat", json={"message": "你好", "visitor_id": visitor}
        ) as resp:
            body = "".join(resp.iter_text())

    events = [line[6:] for line in body.splitlines() if line.startswith("data: ")]
    assert events[-1] == "[DONE]"
    payloads = [json.loads(e) for e in events if e != "[DONE]"]
    assert any(p.get("delta") == budget.GLOBAL_BLOCK_TEXT for p in payloads)
    assert any("message_id" in p for p in payloads)
    assert budget_repo.sum_by_scope(budget.day_start())["global"] >= 1