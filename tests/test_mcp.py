"""r19 Task 7：MCP 接入。

用真实 stdio 子进程跑一遍「拉工具 → 注册 → 调用 → 审计」，
再验证配置写错时服务照常启动。
"""

import json

import pytest
from fastapi.testclient import TestClient
from langgraph.prebuilt import ToolRuntime

from app import store
from app.tools import AgentContext
from app.tools import mcp as mcp_module
from app.tools.executor import Executor
from app.tools.registry import Registry


@pytest.fixture(scope="module", autouse=True)
def _ensure_schema():
    store.init_db()


@pytest.fixture
def isolated_registry(monkeypatch):
    """MCP 注册到临时注册表，避免污染内置工具用例。"""
    reg = Registry()
    monkeypatch.setattr(mcp_module, "registry", reg)
    return reg


def _runtime(visitor_id: str = "v-mcp") -> ToolRuntime:
    return ToolRuntime(
        state={},
        context=AgentContext(visitor_id=visitor_id),
        config={"configurable": {"thread_id": "conv-mcp"}},
        stream_writer=None,
        tool_call_id="call-mcp",
        store=None,
    )


def test_enabled_reads_env(monkeypatch):
    monkeypatch.setenv("MCP_ENABLED", "1")
    assert mcp_module.enabled() is True
    monkeypatch.setenv("MCP_ENABLED", "0")
    assert mcp_module.enabled() is False


def test_missing_config_is_not_fatal(tmp_path):
    missing = tmp_path / "nope.json"
    assert mcp_module.load_config(missing) == {}


@pytest.mark.anyio
async def test_loads_real_server_and_call_goes_through_executor(
    monkeypatch, isolated_registry
):
    monkeypatch.setenv("MCP_ENABLED", "1")
    monkeypatch.delenv("MCP_CONFIG", raising=False)

    async with mcp_module.AsyncExitStack() as stack:
        specs = await mcp_module.load_servers(stack)

        assert [s.name for s in specs] == ["logistics__query_shipping_eta"]
        spec = specs[0]
        assert spec.source == "mcp"
        assert spec.server == "logistics"
        assert spec.scope == "none"
        assert spec.risk == "read"
        assert "order_no" in spec.args_schema.model_fields

        result = await Executor().invoke(
            spec, _runtime(), {"order_no": "ORDER20990001", "carrier": "SF"}
        )

    assert result["found"] is True
    assert result["eta_days"] == 2

    row = store.list_tool_audits(tool=spec.name, limit=1)[0]
    assert row["status"] == "ok"
    assert row["source"] == "mcp"


@pytest.mark.anyio
async def test_bad_args_are_rejected_before_touching_server(
    monkeypatch, isolated_registry
):
    monkeypatch.setenv("MCP_ENABLED", "1")
    monkeypatch.delenv("MCP_CONFIG", raising=False)

    async with mcp_module.AsyncExitStack() as stack:
        specs = await mcp_module.load_servers(stack)
        spec = specs[0]

        result = await Executor().invoke(spec, _runtime(), {"carrier": "SF"})

    assert "参数不合法" in result["error"]
    assert "order_no" in result["error"]


def test_broken_server_does_not_break_startup(tmp_path, monkeypatch, caplog):
    cfg = tmp_path / "mcp_broken.json"
    cfg.write_text(
        json.dumps(
            {
                "servers": {
                    "broken": {
                        "transport": "stdio",
                        "command": "no-such-command-r19",
                        "args": [],
                        "enabled": True,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MCP_ENABLED", "1")
    monkeypatch.setenv("MCP_CONFIG", str(cfg))

    from app.main import app

    with caplog.at_level("WARNING"):
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200

    warnings = [rec.getMessage() for rec in caplog.records if rec.levelname == "WARNING"]
    assert any("broken" in msg for msg in warnings), warnings
    assert all(not s.name.startswith("broken__") for s in mcp_module.registry.all())