"""r15 Task 4：指代消解模块的验证测试。

纯函数 needs_resolution 钉住触发边界：有代词 / 极短+有未决话题 → 触发；
无代词且长度足 / 极短但无未决话题 → 跳过。
resolve_reference 用 FakeLLM 钉住两件事：正常改写、异常时原样返回。
"""

import pytest
from langchain_core.messages import AIMessage

from app.resolve import needs_resolution, resolve_reference


# ── Step 1: 触发条件测试 ─────────────────────────────────────────────

@pytest.mark.parametrize("message,pending,expected", [
    ("它怎么样了", "knowledge", True),
    ("这个能退吗", "business", True),
    ("我的包裹怎么还没到", "", False),        # 无代词且长度足
    ("嗯", "", False),                        # 极短但无未决话题
    ("嗯", "knowledge", True),                # 极短 + 有未决话题
])
def test_trigger(message, pending, expected):
    assert needs_resolution(message, pending) is expected


# ── Step 4: LLM 改写测试 ─────────────────────────────────────────────

class FakeLLM:
    """mock 的 llm 对象：ainvoke 返回给定文本的 AIMessage，不调真模型。"""

    def __init__(self, content: str):
        self._content = content

    async def ainvoke(self, messages):
        return AIMessage(content=self._content)


class FakeLLMThrows:
    """mock 的 llm 对象：ainvoke 始终抛异常。"""

    async def ainvoke(self, messages):
        raise RuntimeError("网络断了")


@pytest.mark.anyio
async def test_resolve_rewrites():
    out = await resolve_reference(
        "它怎么样了",
        [{"role": "user", "content": "我的订单 ORDER20260001 到哪了"}],
        FakeLLM("ORDER20260001 的工单处理到哪一步了"),
    )
    assert out == "ORDER20260001 的工单处理到哪一步了"


@pytest.mark.anyio
async def test_resolve_failure_returns_original():
    out = await resolve_reference("它怎么样了", [], FakeLLMThrows())
    assert out == "它怎么样了"
