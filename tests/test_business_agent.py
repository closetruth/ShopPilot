"""r15 Task 6：主力 agent 裁剪与上限的验证测试。

主力 agent 只拿业务四件工具（知识检索已收归图节点），停止条件有
步数（recursion_limit）与 token 两条，触线输出转人工话术而不是死循环。
全部用 mock LLM，不调真模型。
"""

import pytest

from app.agent import STEP_LIMIT, TOKEN_LIMIT, build_business_agent

BUSINESS_FOUR = {
    "lookup_product",
    "list_my_orders",
    "lookup_work_order",
    "draft_work_order",
}


def test_tools_trimmed_to_business_four():
    """工具集必须恰好是业务四件，不得混入检索等其他工具。

    prebuilt 要求模型支持 bind_tools，用假的工具调用模型替换。
    """
    agent = build_business_agent(llm=_always_tool_llm())
    # ToolNode.bound._tools_by_name 是工具名 → 工具的映射
    names = set(agent.nodes["tools"].bound._tools_by_name)
    assert names == BUSINESS_FOUR


def test_limits_are_the_designed_values():
    """上限常量照设计文档：步数 15、token 8000。"""
    assert STEP_LIMIT == 15
    assert TOKEN_LIMIT == 8000


# ---------- 上限触线的验证 ----------

def _always_tool_llm():
    """永远要求调工具的 LLM：构造无限循环，逼出 recursion_limit。

    用 langchain 标准测试替身 GenericFakeChatModel 加一个返回自身的
    bind_tools——prebuilt 构造时会先 bind_tools，替身不需要真绑定。
    """
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    def gen():
        for i in range(50):
            yield AIMessage(
                content="",
                tool_calls=[{"name": "list_my_orders", "args": {}, "id": f"c{i}"}],
            )

    class Bindable(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    return Bindable(messages=gen())


@pytest.mark.anyio
async def test_recursion_limit_stops_loop():
    """模型陷入工具循环时按步数上限中断，输出转人工话术。"""
    from langchain_core.messages import HumanMessage

    from app.agent import run_business_agent_with_limit

    result = await run_business_agent_with_limit(
        {"messages": [HumanMessage(content="查订单")]},
        llm=_always_tool_llm(),
    )
    assert "人工" in result["final_text"]
    assert result["stopped_by"] == "step_limit"


def test_token_limit_exceeded_detected():
    """token 累计超过上限时判定触线。"""
    from app.agent import _tokens_exceeded

    assert _tokens_exceeded(used=7900, incoming=200) is True
    assert _tokens_exceeded(used=7900, incoming=100) is False
    assert _tokens_exceeded(used=0, incoming=0) is False
