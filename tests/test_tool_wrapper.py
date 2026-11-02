"""r19 Task 1：包装工具保留 runtime 签名的可行性 spike。

create_react_agent 不暴露 wrap_tool_call，统一拦截只能靠「包装函数保留
ToolRuntime 签名 + 显式 args_schema」。这里用最小回显工具钉住四条事实，
任一条不成立就必须退回备选方案（工具体内首行调 executor）。
"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import StructuredTool
from langgraph.prebuilt import ToolRuntime, create_react_agent
from pydantic import BaseModel, Field

from app.tools import AgentContext


class EchoArgs(BaseModel):
    text: str = Field(description="要回显的文本")


CAPTURED: dict = {}


def _echo_impl(*, text: str) -> dict:
    return {"echo": text, "len": len(text)}


def _echo_wrapper(runtime: ToolRuntime[AgentContext], **kwargs) -> dict:
    CAPTURED["visitor_id"] = runtime.context.visitor_id
    return _echo_impl(**kwargs)


def _wrapped_tool() -> StructuredTool:
    return StructuredTool.from_function(
        func=_echo_wrapper,
        name="echo",
        description="回显文本",
        args_schema=EchoArgs,
    )


def test_schema_seen_by_model_excludes_runtime():
    """模型可见的调用 schema 只有业务参数，注入参数不外泄。"""
    tool = _wrapped_tool()
    schema = tool.tool_call_schema.model_json_schema()
    assert set(schema["properties"]) == {"text"}
    assert tool._injected_args_keys == frozenset({"runtime"})


def test_tool_node_injects_runtime_into_wrapper():
    """ToolNode 能把 runtime 注进包装函数，包装内读得到访客身份。"""
    CAPTURED.clear()
    agent = create_react_agent(
        model=_one_tool_call_llm(),
        tools=[_wrapped_tool()],
        context_schema=AgentContext,
    )
    state = agent.invoke(
        {"messages": [HumanMessage(content="回显 hi")]},
        context=AgentContext(visitor_id="visitor-abc"),
    )
    assert CAPTURED["visitor_id"] == "visitor-abc"

    tool_msgs = [m for m in state["messages"] if getattr(m, "name", "") == "echo"]
    assert tool_msgs, "包装工具没有被真正执行"
    assert "hi" in str(tool_msgs[-1].content)


@pytest.mark.anyio
async def test_wrapper_result_matches_direct_impl():
    """包装层只做拦截，不改返回值。"""
    runtime = ToolRuntime(
        state={},
        context=AgentContext(visitor_id="v2"),
        config={},
        stream_writer=None,
        tool_call_id="c1",
        store=None,
    )
    result = await _wrapped_tool().ainvoke(
        {"text": "abc", "runtime": runtime}, config={}
    )
    assert result == _echo_impl(text="abc")


def _one_tool_call_llm():
    """第一次要求调 echo，第二次给出最终文本。"""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    def gen():
        yield AIMessage(
            content="",
            tool_calls=[{"name": "echo", "args": {"text": "hi"}, "id": "c1"}],
        )
        yield AIMessage(content="done")

    class Bindable(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    return Bindable(messages=gen())