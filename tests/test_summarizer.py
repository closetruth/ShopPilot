"""r18 T4：摘要服务的验证测试。

提示词的编造禁令与四类事实约束用「钉住提示词文本存在」的方式守——
摘要质量本身靠验收轮的真实对话检验。水位线必须取本次读到的消息
max(id) 快照（防摘要进行中用户新发消息被错误标记已覆盖）。
"""

import uuid

import pytest

from app import store
from app.summarizer import SUMMARY_PROMPT, maybe_summarize


@pytest.fixture
def conv_id():
    visitor_id = "ctx-" + uuid.uuid4().hex[:8]
    conv = store.create_conversation(visitor_id)
    yield conv


class FakeLLM:
    def __init__(self, content: str):
        self._content = content
        self.prompts: list = []

    async def ainvoke(self, messages, **kwargs):
        self.prompts.append(messages)
        from langchain_core.messages import AIMessage

        return AIMessage(content=self._content)


def test_prompt_has_no_fabrication_rule():
    """编造禁令与四类事实必须写在提示词里（线上行为的第一道闸）。"""
    assert "不许编" in SUMMARY_PROMPT or "禁止编" in SUMMARY_PROMPT or "不要编" in SUMMARY_PROMPT
    for fact in ("商品", "订单号", "诉求"):
        assert fact in SUMMARY_PROMPT


@pytest.mark.anyio
async def test_below_threshold_no_summarize(conv_id):
    """原文未超阈值（budget×0.8）不触发摘要，不调 LLM。"""
    store.append_message(conv_id, "user", "短消息")
    llm = FakeLLM("梗概")
    result = await maybe_summarize(conv_id, llm=llm)
    assert result is None
    assert llm.prompts == []
    assert store.get_summary(conv_id) is None


@pytest.mark.anyio
async def test_over_threshold_summarizes_and_advances_watermark(conv_id):
    """超阈值：调 LLM 生成梗概，水位线前移到本次读到消息的 max(id)。"""
    from app.repo import chats

    for i in range(8):
        store.append_message(conv_id, "user", f"我买的杯子坏了订单号ORDER2026000{i} " * 10)
    ids = chats._message_ids(conv_id)

    llm = FakeLLM("顾客反映保温杯破损，订单号 ORDER20260000-7，要求退货。")
    result = await maybe_summarize(conv_id, llm=llm)
    assert result is not None
    assert llm.prompts, "应调一次 LLM"

    s = store.get_summary(conv_id)
    assert s["covered_until"] == ids[-1], "水位线应等于本次读到消息的最大 id"
    assert "ORDER20260000" in s["summary"]


@pytest.mark.anyio
async def test_watermark_is_read_snapshot_not_max(conv_id):
    """并发安全：摘要读完后用户新发消息，水位线不含新消息。

    mock 场景——llm.ainvoke 期间往库里塞一条新消息，模拟「摘要进行中
    用户继续聊」。水位线必须停在快照边界，新消息下轮正常计入。
    """
    from app.repo import chats

    class SlowLLM:
        def __init__(self):
            self.fired = False

        async def ainvoke(self, messages, **kwargs):
            if not self.fired:
                self.fired = True
                store.append_message(conv_id, "user", "摘要进行中的新消息")
            from langchain_core.messages import AIMessage

            return AIMessage(content="梗概")

    for i in range(8):
        store.append_message(conv_id, "user", f"长消息内容填充 " * 20)
    snapshot_ids = chats._message_ids(conv_id)

    llm = SlowLLM()
    await maybe_summarize(conv_id, llm=llm)

    s = store.get_summary(conv_id)
    assert s["covered_until"] == snapshot_ids[-1], "水位线是读快照，不含摘要期间的新消息"


@pytest.mark.anyio
async def test_llm_failure_leaves_no_summary(conv_id):
    """LLM 挂了：不落梗概、不前移水位线，下轮重新评估。"""
    class Boom:
        async def ainvoke(self, messages, **kwargs):
            raise RuntimeError("网络断了")

    for i in range(8):
        store.append_message(conv_id, "user", "长消息内容填充 " * 20)

    result = await maybe_summarize(conv_id, llm=Boom())
    assert result is None
    assert store.get_summary(conv_id) is None
