"""r15 Task 3：意图分类模块的验证测试。

规则层用参数化用例钉住四类边界（闲聊 → 投诉 → 业务 → 知识的匹配顺序）。
控制器修正了两处 brief 用例：
- 「买一送一能退一件吗」含「退」但无订单引用，应判知识而非业务——
  裸「退/换/物流/发货」不触发业务，业务必须显式订单引用；
- 「这个商品我能用吗」无任何规则命中，钉住 LLM 兜底路径。
"""

import pytest
from langchain_core.messages import AIMessage

from app.intent import IntentResult, classify_by_rules, classify_intent


@pytest.mark.parametrize("message,expected", [
    ("你好", "chitchat"), ("在吗", "chitchat"), ("谢谢啦", "chitchat"),
    ("我要投诉你们", "complaint"), ("你们太差了我要曝光", "complaint"),
    ("投诉的流程是什么", "knowledge"),          # 带「投诉」但问流程 → 知识
    ("查一下我的订单", "business"), ("ORDER20260001 到哪了", "business"),
    ("退货运费谁出", "knowledge"), ("发票能开吗", "knowledge"),
    ("买一送一能退一件吗", "knowledge"),        # 控制器修正：含「退」但无订单引用 + 疑问模式
    ("这个商品我能用吗", None),                 # 控制器新增：无规则命中 → LLM 兜底路径
    ("包装破了", "knowledge"),                  # r16：售后诉求词钉 knowledge——LLM 兜底曾把
                                                # 「破了」理解成情绪误判 complaint(0.75)
    ("杯子摔坏了想退", "knowledge"),            # 同族：摔坏是售后陈述
    ("快递好久没动", "knowledge"),              # r16 验收暴露：LLM 兜底曾误判 business(查件)
    ("物流太慢了", "knowledge"),                # 同族：物流慢是延迟补偿问题
])
def test_rules(message, expected):
    intent, confident = classify_by_rules(message)
    if expected is None:
        assert intent is None and not confident
    else:
        assert (intent, confident) == (expected, True)


class FakeLLM:
    """mock 的 llm 对象：ainvoke 返回给定文本的 AIMessage，不调真模型。"""

    def __init__(self, content: str):
        self._content = content

    async def ainvoke(self, messages):
        return AIMessage(content=self._content)


@pytest.mark.anyio
async def test_llm_fallback_parses_json():
    result = await classify_intent("这个商品我能用吗", FakeLLM('{"intent":"knowledge","confidence":0.8}'))
    assert result.intent == "knowledge" and result.method == "llm"
    assert result.confidence == 0.8


@pytest.mark.anyio
async def test_llm_unparseable_falls_to_business():
    result = await classify_intent("???", FakeLLM("我说不清"))
    assert result.intent == "business" and result.method == "fallback"


@pytest.mark.anyio
async def test_llm_low_confidence_falls_to_business():
    """置信度低于 0.5 按 business 兜底：宁可进主力 agent 反问，不可漏单。"""
    result = await classify_intent("???", FakeLLM('{"intent":"chitchat","confidence":0.4}'))
    assert result.intent == "business" and result.method == "fallback"


class FakeLLMThrows:
    """mock 的 llm 对象：ainvoke 始终抛异常。"""
    async def ainvoke(self, messages):
        raise RuntimeError("网络断了")


@pytest.mark.anyio
async def test_llm_throws_falls_to_business():
    """LLM 调用本身失败时降级为 business（Critical：此前无异常保护）。"""
    result = await classify_intent("帮我查一下", FakeLLMThrows())
    assert result.intent == "business"
    assert result.confidence == 0.0
    assert result.method == "fallback"


@pytest.mark.anyio
async def test_unparseable_logs_warning(caplog):
    """解析失败时记 warning（不是 info）——线上要能一眼看出。"""
    import logging
    caplog.set_level(logging.WARNING)
    await classify_intent("???", FakeLLM("纯乱码"))
    assert any(
        r.levelno == logging.WARNING and "无法解析" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.anyio
async def test_rule_hit_skips_llm():
    """反目标「规则能定的不调模型」：规则命中时 llm 不应被调用。"""
    llm = FakeLLM('{"intent":"chitchat","confidence":0.9}')
    llm.ainvoke = lambda messages: (_ for _ in ()).throw(AssertionError("规则命中不应调 LLM"))
    result = await classify_intent("你好", llm)
    assert result == IntentResult(intent="chitchat", confidence=1.0, method="rule")


def test_intent_result_shape():
    """IntentResult 的字段与取值范围照 brief 契约。"""
    result = IntentResult(intent="knowledge", confidence=0.8, method="rule")
    assert result.intent == "knowledge"
    assert result.confidence == 0.8
    assert result.method == "rule"
