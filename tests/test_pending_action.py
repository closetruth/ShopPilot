"""r19 Task 5：待确认动作（模型提议、顾客点击后才执行）。

重点验证三件事：挂起时工具没跑、确认后参数从库里重放、终态不可重复操作。
工具名带随机后缀，避免共享 MySQL 上与其他用例串味。
"""

import asyncio
import json
import uuid
from datetime import timedelta

import pytest
from langgraph.prebuilt import ToolRuntime
from pydantic import BaseModel

from app import store
from app.db import connect, now
from app.tools import AgentContext
from app.tools.executor import ActionError, Executor
from app.tools.registry import ToolSpec, registry


class Args(BaseModel):
    text: str


@pytest.fixture(scope="module", autouse=True)
def _ensure_schema():
    store.init_db()


def _name(base: str) -> str:
    return f"{base}_{uuid.uuid4().hex[:8]}"


def _runtime(visitor_id: str = "v-pending", thread_id: str = "conv-pending") -> ToolRuntime:
    return ToolRuntime(
        state={},
        context=AgentContext(visitor_id=visitor_id),
        config={"configurable": {"thread_id": thread_id}},
        stream_writer=None,
        tool_call_id="call-1",
        store=None,
    )


def _spec(name: str, impl, **kwargs) -> ToolSpec:
    fields = {
        "name": name,
        "label": "测试",
        "description": "测试工具",
        "args_schema": Args,
        "impl": impl,
        "scope": "visitor",
        "risk": "write",
        "requires_confirmation": True,
        "allowed_callers": ("agent",),
    }
    fields.update(kwargs)
    spec = ToolSpec(**fields)
    registry.register(spec, override=True)
    return spec


def _audits(tool_name: str) -> list[dict]:
    return store.list_tool_audits(tool=tool_name, limit=5)


def _force_expired(action_id: str) -> None:
    """把过期时间改到过去，模拟卡片放太久。"""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE pending_actions SET expires_at = %s WHERE id = %s",
            (now() - timedelta(minutes=1), action_id),
        )


@pytest.mark.anyio
async def test_suspend_does_not_run_impl_and_audits_pending():
    name = _name("suspend")
    calls: list[str] = []

    def impl(runtime, *, text):
        calls.append(text)
        return {"created": True, "work_order_no": "WO-1"}

    spec = _spec(name, impl)
    result = await Executor().invoke(spec, _runtime("v-suspend"), {"text": "hi"})

    assert calls == [], "挂起阶段不能真的执行工具"
    action = result["pending_action"]
    assert action["tool"] == name
    assert action["summary"]["title"] == "测试"
    assert action["summary"]["fields"] == [{"k": "text", "v": "hi"}]

    rows = _audits(name)
    assert [r["status"] for r in rows] == ["pending"]
    assert rows[0]["action_id"] == action["id"]

    stored = store.get_pending_action(action["id"])
    assert stored["status"] == "pending"
    assert stored["args_json"] == {"text": "hi"}
    assert stored["visitor_id"] == "v-suspend"


@pytest.mark.anyio
async def test_confirm_replays_args_from_db_and_fills_result():
    name = _name("confirm")
    calls: list[str] = []

    def impl(runtime, *, text):
        calls.append(text)
        return {"created": True, "work_order_no": "WO-2"}

    spec = _spec(name, impl)
    runtime = _runtime("v-confirm", "conv-confirm")
    action = (await Executor().invoke(spec, runtime, {"text": "replay"}))["pending_action"]

    result = await Executor().confirm(action["id"], visitor_id="v-confirm")

    assert calls == ["replay"]
    assert result == {"created": True, "work_order_no": "WO-2"}

    stored = store.get_pending_action(action["id"])
    assert stored["status"] == "confirmed"
    assert stored["result_json"] == {"created": True, "work_order_no": "WO-2"}

    rows = _audits(name)
    assert [r["status"] for r in rows] == ["ok", "pending"]
    assert rows[0]["caller"] == "confirm"
    assert rows[0]["action_id"] == action["id"]


@pytest.mark.anyio
async def test_cancel_marks_terminal_without_running_impl():
    name = _name("cancel")
    calls: list[str] = []

    def impl(runtime, *, text):
        calls.append(text)
        return {"created": True}

    spec = _spec(name, impl)
    action = (await Executor().invoke(spec, _runtime("v-cancel"), {"text": "x"}))["pending_action"]

    out = await Executor().cancel(action["id"], visitor_id="v-cancel")

    assert calls == []
    assert out == {"status": "cancelled", "action_id": action["id"]}
    assert store.get_pending_action(action["id"])["status"] == "cancelled"
    assert [r["status"] for r in _audits(name)] == ["cancelled", "pending"]


@pytest.mark.anyio
async def test_confirm_twice_is_rejected():
    name = _name("twice")
    spec = _spec(name, lambda runtime, *, text: {"created": True})
    action = (await Executor().invoke(spec, _runtime("v-twice"), {"text": "x"}))["pending_action"]

    await Executor().confirm(action["id"], visitor_id="v-twice")

    with pytest.raises(ActionError) as exc:
        await Executor().confirm(action["id"], visitor_id="v-twice")
    assert exc.value.status_code == 409


@pytest.mark.anyio
async def test_other_visitor_sees_not_found():
    """不存在与不属于本人统一 404，避免探测他人动作。"""
    name = _name("owner")
    spec = _spec(name, lambda runtime, *, text: {"created": True})
    action = (await Executor().invoke(spec, _runtime("v-owner"), {"text": "x"}))["pending_action"]

    with pytest.raises(ActionError) as exc:
        await Executor().confirm(action["id"], visitor_id="v-stranger")
    assert exc.value.status_code == 404
    assert store.get_pending_action(action["id"])["status"] == "pending"


@pytest.mark.anyio
async def test_expired_action_is_rejected():
    name = _name("expire")
    spec = _spec(name, lambda runtime, *, text: {"created": True})
    action = (await Executor().invoke(spec, _runtime("v-expire"), {"text": "x"}))["pending_action"]

    _force_expired(action["id"])

    with pytest.raises(ActionError) as exc:
        await Executor().confirm(action["id"], visitor_id="v-expire")
    assert exc.value.status_code == 410
    assert store.get_pending_action(action["id"])["status"] == "expired"


@pytest.mark.anyio
async def test_preview_failure_skips_card_and_returns_reason():
    name = _name("preview")
    calls: list[str] = []

    def preview(runtime, *, text):
        return {"ok": False, "reason": "duplicate", "note": "已有同类工单"}

    def impl(runtime, *, text):
        calls.append(text)
        return {"created": True}

    spec = _spec(name, impl, preview=preview)
    result = await Executor().invoke(spec, _runtime("v-preview"), {"text": "x"})

    assert calls == []
    assert result["reason"] == "duplicate"
    assert "pending_action" not in result
    assert [r["status"] for r in _audits(name)] == ["ok"]
    assert store.list_pending_actions("v-preview") == []


@pytest.mark.anyio
async def test_preview_passing_still_suspends():
    name = _name("preview_ok")
    spec = _spec(
        name, lambda runtime, *, text: {"created": True}, preview=lambda runtime, *, text: {"ok": True}
    )
    result = await Executor().invoke(spec, _runtime("v-preview-ok"), {"text": "x"})

    assert result["pending_action"]["tool"] == name
    assert [r["status"] for r in _audits(name)] == ["pending"]


def test_sse_event_carries_pending_action():
    """SSE 转发的是工具输出里的 pending_action，不做任何字段翻译。"""
    from langchain_core.messages import ToolMessage

    from app.api.customer import _pending_from_tool_event

    payload = {"pending_action": {"id": "pa_x", "summary": {"title": "待确认工单"}}}
    event = {"data": {"output": ToolMessage(content=json.dumps(payload), tool_call_id="c1")}}
    assert _pending_from_tool_event(event) == payload["pending_action"]

    plain = {"data": {"output": ToolMessage(content='{"echo": "hi"}', tool_call_id="c2")}}
    assert _pending_from_tool_event(plain) is None


def _create_action(visitor_id: str, description: str) -> str:
    spec = registry.get("create_work_order")
    result = asyncio.run(
        Executor().invoke(
            spec,
            _runtime(visitor_id),
            {"issue_type": "咨询", "description": description},
        )
    )
    assert "pending_action" in result, result
    return result["pending_action"]["id"]


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


def test_confirm_endpoint_rejects_other_visitor(client):
    action_id = _create_action("v-api-owner", "想了解售后退换流程的具体步骤")

    resp = client.post(
        "/api/tool/confirm", json={"action_id": action_id, "visitor_id": "v-api-stranger"}
    )
    assert resp.status_code == 404
    assert store.get_pending_action(action_id)["status"] == "pending"


def test_confirm_endpoint_creates_work_order(client):
    visitor_id = f"v-api-confirm-{uuid.uuid4().hex[:8]}"
    action_id = _create_action(visitor_id, "想了解售后退换流程的具体步骤")
    assert store.list_work_orders(visitor_id) == []

    resp = client.post(
        "/api/tool/confirm", json={"action_id": action_id, "visitor_id": visitor_id}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["result"]["created"] is True
    assert data["result"]["work_order_no"].startswith("WO")
    assert len(store.list_work_orders(visitor_id)) == 1


def test_cancel_endpoint_creates_nothing(client):
    visitor_id = f"v-api-cancel-{uuid.uuid4().hex[:8]}"
    action_id = _create_action(visitor_id, "想了解售后退换流程的具体步骤")

    resp = client.post(
        "/api/tool/cancel", json={"action_id": action_id, "visitor_id": visitor_id}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"
    assert store.list_work_orders(visitor_id) == []