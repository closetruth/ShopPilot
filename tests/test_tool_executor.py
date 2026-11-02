"""r19 Task 4：执行引擎。

五种结局（校验失败/鉴权拒绝/超时/异常/正常）都断言两件事：
模型拿到的返回值可读、审计表里恰好一行且 status 正确。
工具名带随机后缀，避免共享 MySQL 上与其他用例串味。
"""

import asyncio
import time
import uuid

import pytest
from langgraph.prebuilt import ToolRuntime
from pydantic import BaseModel

from app.core import store
from app.tools import AgentContext
from app.tools.executor import Executor, RuntimeMeta, runtime_meta
from app.tools.registry import ToolSpec, build_tool


class Args(BaseModel):
    text: str


class StrictArgs(BaseModel):
    model_config = {"extra": "forbid"}
    text: str


@pytest.fixture(scope="module", autouse=True)
def _ensure_schema():
    store.init_db()


def _name(base: str) -> str:
    return f"{base}_{uuid.uuid4().hex[:8]}"


def _runtime(visitor_id: str = "v-test", thread_id: str = "conv-test") -> ToolRuntime:
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
        "scope": "none",
        "risk": "read",
        "allowed_callers": ("agent",),
    }
    fields.update(kwargs)
    return ToolSpec(**fields)


def _only_row(tool_name: str) -> dict:
    rows = store.list_tool_audits(tool=tool_name, limit=5)
    assert len(rows) == 1, f"审计应有且仅有一行，实际 {len(rows)}"
    return rows[0]


def test_runtime_meta_extracts_conversation_and_visitor():
    meta = runtime_meta(_runtime("v1", "conv-1"))
    assert meta.conversation_id == "conv-1"
    assert meta.visitor_id == "v1"
    assert runtime_meta(None) == RuntimeMeta(None, None)


@pytest.mark.anyio
async def test_ok_path_returns_result_and_audits():
    name = _name("ok")

    def impl(runtime, *, text):
        return {"echo": text}

    spec = _spec(name, impl)
    result = await Executor().invoke(spec, _runtime("v-ok", "conv-ok"), {"text": "hi"})

    assert result == {"echo": "hi"}
    row = _only_row(name)
    assert row["status"] == "ok"
    assert row["visitor_id"] == "v-ok"
    assert row["conversation_id"] == "conv-ok"
    assert row["args_json"] == {"text": "hi"}
    assert row["duration_ms"] >= 0


@pytest.mark.anyio
async def test_async_impl_supported():
    name = _name("async")

    async def impl(runtime, *, text):
        return {"echo": text}

    spec = _spec(name, impl)
    result = await Executor().invoke(spec, _runtime("v-async"), {"text": "hi"})
    assert result == {"echo": "hi"}
    assert _only_row(name)["status"] == "ok"


@pytest.mark.anyio
async def test_validation_failure_is_model_readable_and_audited():
    name = _name("bad")
    spec = _spec(name, lambda runtime, *, text: {"echo": text})

    result = await Executor().invoke(spec, _runtime(), {})

    assert "error" in result
    assert "参数不合法" in result["error"]
    assert "text: Field required" in result["error"]
    row = _only_row(name)
    assert row["status"] == "error"
    assert row["result_digest"] is None
    assert row["error"].startswith("ValidationError")


@pytest.mark.anyio
async def test_unknown_field_rejected_when_schema_forbids_extra():
    """内置工具的 schema 都是 extra="forbid"，模型多传字段要被挡下来。"""
    name = _name("strict")
    spec = _spec(
        name, lambda runtime, *, text: {"echo": text}, args_schema=StrictArgs
    )

    result = await Executor().invoke(
        spec, _runtime(), {"text": "x", "unknown": "y"}
    )

    assert "参数不合法" in result["error"]
    assert "unknown" in result["error"]
    assert _only_row(name)["status"] == "error"


@pytest.mark.anyio
async def test_denied_when_visitor_scope_without_identity():
    name = _name("denied")
    spec = _spec(
        name,
        lambda runtime, *, text: {"echo": text},
        scope="visitor",
    )

    result = await Executor().invoke(spec, _runtime(None), {"text": "x"})

    assert result == {"error": "该操作不被允许：缺少访客身份"}
    row = _only_row(name)
    assert row["status"] == "denied"
    assert row["caller"] == "agent"


@pytest.mark.anyio
async def test_timeout_returns_readable_error():
    name = _name("slow")

    def impl(runtime, *, text):
        time.sleep(0.5)
        return {"echo": text}

    spec = _spec(name, impl, timeout_s=0.05)
    started = time.perf_counter()
    result = await Executor().invoke(spec, _runtime(), {"text": "x"})
    elapsed = time.perf_counter() - started

    assert result == {"error": "工具执行超时，请稍后重试"}
    assert elapsed < 0.5, "超时后不应继续等 impl 跑完"
    assert _only_row(name)["status"] == "error"


@pytest.mark.anyio
async def test_impl_exception_normalized_and_audited():
    name = _name("boom")

    def impl(runtime, *, text):
        raise RuntimeError("内部炸了")

    spec = _spec(name, impl)
    result = await Executor().invoke(spec, _runtime(), {"text": "x"})

    assert result == {"error": "工具暂时不可用，请稍后重试"}
    row = _only_row(name)
    assert row["status"] == "error"
    assert row["error"].startswith("RuntimeError")


@pytest.mark.anyio
async def test_audit_failure_never_breaks_tool(monkeypatch):
    """审计链路断了，工具照常返回。"""
    name = _name("auditfail")

    def impl(runtime, *, text):
        return {"echo": text}

    def boom(*args, **kwargs):
        raise RuntimeError("audit db down")

    monkeypatch.setattr(store, "record_tool_audit", boom)
    spec = _spec(name, impl)
    result = await Executor().invoke(spec, _runtime(), {"text": "hi"})
    assert result == {"echo": "hi"}


@pytest.mark.anyio
async def test_tool_wrapper_routes_through_executor():
    """包装工具（模型真正调用的那个）也走执行引擎，不是直接调 impl。"""
    name = _name("wrapped")

    def impl(runtime, *, text):
        return {"echo": text}

    spec = _spec(name, impl)
    tool = build_tool(spec)
    result = await tool.ainvoke({"text": "hi", "runtime": _runtime("v-wrap")}, config={})

    assert result == {"echo": "hi"}
    row = _only_row(name)
    assert row["status"] == "ok"
    assert row["visitor_id"] == "v-wrap"


@pytest.mark.anyio
async def test_concurrent_calls_do_not_block_loop():
    """同步 impl 走线程池：两次调用在时间上重叠，而不是排队串行。"""
    name = _name("conc")
    spans: list[tuple[float, float]] = []

    def impl(runtime, *, text):
        started = time.perf_counter()
        time.sleep(0.2)
        spans.append((started, time.perf_counter()))
        return {"echo": text}

    spec = _spec(name, impl)
    await asyncio.gather(
        Executor().invoke(spec, _runtime(), {"text": "a"}),
        Executor().invoke(spec, _runtime(), {"text": "b"}),
    )

    assert len(spans) == 2
    first, second = sorted(spans)
    assert second[0] < first[1], "两次执行没有重叠，说明退化成串行"