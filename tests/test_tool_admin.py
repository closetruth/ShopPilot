"""r19 Task 8：注册清单与审计页接口。"""

import pytest
from fastapi.testclient import TestClient

from app.core import store
from app.main import app
from app.tools import AgentContext
from app.tools.executor import Executor
from app.tools.registry import registry


@pytest.fixture(scope="module", autouse=True)
def _ensure_schema():
    store.init_db()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_tools_page_is_served(client):
    res = client.get("/admin/tools")
    assert res.status_code == 200
    assert "工具注册与调用审计" in res.text


def test_tool_list_exposes_metadata(client):
    res = client.get("/api/admin/tools")
    assert res.status_code == 200
    items = {item["name"]: item for item in res.json()["items"]}

    assert "lookup_product" in items
    assert "create_work_order" in items

    work_order = items["create_work_order"]
    assert work_order["source"] == "builtin"
    assert work_order["risk"] == "write"
    assert work_order["requires_confirmation"] is True
    assert work_order["scope"] == "visitor"
    assert work_order["allowed_callers"] == ["agent"]

    product = items["lookup_product"]
    assert product["source"] == "builtin"
    assert product["risk"] == "read"
    assert product["allowed_callers"] == ["admin", "agent"]


@pytest.mark.anyio
async def test_audit_endpoint_filters_and_paginates():
    spec = registry.get("lookup_product")
    runtime = type("R", (), {
        "context": AgentContext(visitor_id="v-audit-page"),
        "config": {"configurable": {"thread_id": "conv-audit-page"}},
        "tool_call_id": "call-audit-page",
    })()

    await Executor().invoke(spec, runtime, {"keyword": "不存在的商品xyz"})

    res = TestClient(app).get(
        "/api/admin/audit",
        params={"tool": "lookup_product", "visitor_id": "v-audit-page", "limit": 5},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["limit"] == 5
    assert body["offset"] == 0
    assert body["items"]
    assert all(row["tool_name"] == "lookup_product" for row in body["items"])
    assert all(row["visitor_id"] == "v-audit-page" for row in body["items"])
    assert isinstance(body["items"][0]["args_json"], dict)


def test_audit_limit_is_clamped(client):
    body = client.get("/api/admin/audit", params={"limit": 9999}).json()
    assert body["limit"] == 200