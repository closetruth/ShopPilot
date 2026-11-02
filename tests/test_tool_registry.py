"""r19 Task 2：注册中心与三层权限的验证。

注册表本身用独立实例测（不污染全局），内置工具的元数据测全局注册表。
"""

import pytest
from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from app.tools import agent_tools, registry, tool_label
from app.tools.permission import Caller, Denied, authorize
from app.tools.registry import Registry, ToolSpec

BUILTIN_FOUR = {
    "lookup_product",
    "list_my_orders",
    "lookup_work_order",
    "draft_work_order",
}


class EchoArgs(BaseModel):
    text: str


def _spec(name: str = "echo", **kwargs) -> ToolSpec:
    def impl(runtime, *, text: str = "") -> dict:
        """回显文本。"""
        return {"echo": text}

    fields = {
        "name": name,
        "label": "回显",
        "description": "回显文本",
        "args_schema": EchoArgs,
        "impl": impl,
    }
    fields.update(kwargs)
    return ToolSpec(**fields)


def test_builtin_tools_registered_with_metadata():
    """四个内置工具都在注册表里，元数据可被程序读取。"""
    names = {spec.name for spec in registry.all()}
    assert names == BUILTIN_FOUR

    product = registry.get("lookup_product")
    assert product.scope == "none"
    assert product.risk == "read"
    assert product.source == "builtin"
    assert product.requires_confirmation is False

    order = registry.get("list_my_orders")
    assert order.scope == "visitor"

    draft = registry.get("draft_work_order")
    assert draft.risk == "write"
    assert draft.label == "工单草稿"


def test_tool_label_lookup():
    assert tool_label("lookup_product") == "商品"
    assert tool_label("no_such_tool") == "no_such_tool"


def test_agent_tools_are_wrapped_tools():
    tools = agent_tools()
    assert len(tools) == 4
    assert all(isinstance(t, StructuredTool) for t in tools)
    assert {t.name for t in tools} == BUILTIN_FOUR


def test_model_visible_schema_has_no_runtime():
    """注入参数不能出现在模型可见 schema 里。"""
    product = registry.tool("lookup_product")
    schema = product.tool_call_schema.model_json_schema()
    assert set(schema["properties"]) == {"keyword", "product_id"}
    assert product._injected_args_keys == frozenset({"runtime"})


def test_duplicate_name_rejected_unless_override():
    reg = Registry()
    reg.register(_spec("dup"))
    with pytest.raises(ValueError, match="重复"):
        reg.register(_spec("dup"))

    reg.register(_spec("dup", label="覆盖"), override=True)
    assert reg.get("dup").label == "覆盖"


def test_tools_for_filters_by_caller():
    reg = Registry()
    reg.register(_spec("public", allowed_callers=("agent", "admin")))
    reg.register(_spec("admin_only", allowed_callers=("admin",)))

    agent_names = {t.name for t in reg.tools_for("agent")}
    admin_names = {t.name for t in reg.tools_for("admin")}
    assert agent_names == {"public"}
    assert admin_names == {"public", "admin_only"}


def test_empty_callers_rejected():
    with pytest.raises(ValueError, match="白名单"):
        _spec("nobody", allowed_callers=())


def test_destructive_must_require_confirmation():
    """配置错误在注册期就炸，不留到运行期。"""
    with pytest.raises(ValueError, match="destructive"):
        _spec("danger", risk="destructive", requires_confirmation=False)

    spec = _spec("danger", risk="destructive", requires_confirmation=True)
    assert spec.requires_confirmation is True


@pytest.mark.parametrize(
    "scope,kind,visitor,ok",
    [
        ("none", "agent", None, True),
        ("none", "admin", None, True),
        ("visitor", "agent", "v1", True),
        ("visitor", "agent", None, False),
        ("visitor", "admin", None, False),
        ("none", "system", None, False),
    ],
)
def test_authorize_matrix(scope, kind, visitor, ok):
    spec = _spec(
        scope=scope,
        allowed_callers=("agent", "admin") if kind != "system" else ("agent",),
    )
    caller = Caller(kind=kind, visitor_id=visitor)
    if ok:
        authorize(spec, caller)
    else:
        with pytest.raises(Denied):
            authorize(spec, caller)


def test_denied_messages_are_model_safe():
    """拒绝原因不含内部细节，可以直接回给模型。"""
    spec = _spec(scope="visitor", allowed_callers=("agent",))
    with pytest.raises(Denied) as exc:
        authorize(spec, Caller(kind="agent", visitor_id=None))
    assert "访客" in str(exc.value)