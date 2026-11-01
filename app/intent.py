"""意图分类：规则先行，LLM 兜底。

规则层是纯函数，命中即返回、零 LLM 调用；规则分不出的消息才交一次
LLM 判类。解析失败或置信度过低时按业务数据处理——宁可进主力 agent
反问顾客，不可漏单。

规则顺序照设计文档：闲聊 → 投诉 → 业务 → 知识。

与 brief 原词表的差异（控制器裁定）：业务意图要求**显式订单引用**
（ORDER 号正则或「订单/工单」字样），裸「退/换/物流/发货」不再触发
业务——实测会把「退货运费谁出」这类知识问题误判成业务；含这些词但
无订单引用的消息落入知识规则或 LLM 兜底。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# LLM 兜底的置信度阈值：低于它按业务数据处理（宁可多问，不可漏单）
_CONFIDENCE_THRESHOLD = 0.5

INTENT_PROMPT = """你是电商客服的消息分类器。把顾客消息归入四类之一：

- knowledge（知识检索）：问政策、规则、时效——退换货流程、运费谁出、
  发票怎么开、物流几天到、商品怎么保养
- business（业务数据）：查订单状态、工单进度、商品价格库存这类要查
  数据库才能回答的事
- complaint（投诉）：情绪激烈，要投诉、曝光、举报、讨说法
- chitchat（闲聊）：问候、寒暄、感谢，没有实质诉求

顾客消息：{message}

只输出一个 JSON 对象，不要输出其他内容：
{{"intent": "knowledge/business/complaint/chitchat 之一", "confidence": 0.0 到 1.0 的数}}"""


@dataclass(frozen=True)
class IntentResult:
    """一次分类的完整结果：意图、置信度、判定来源。"""

    intent: str
    confidence: float
    method: str  # rule / llm / fallback

# 寒暄词：问候、致谢、道别这类零实质诉求的开场
_CHITCHAT_RE = re.compile(r"你好|您好|在吗|在么|谢谢|感谢|再见|晚安|早上好|下午好|辛苦了")

# 投诉要求情绪性动作：纯「投诉的流程是什么」是在问知识，不能进投诉模板
_COMPLAINT_RE = re.compile(
    r"我要投诉|投诉你们|我要曝光|曝光你们|要曝光|举报|12315|工商局|态度差|客服态度|给个说法|投诉到底"
)

# 业务必须显式订单引用：ORDER 开头订单号，或「订单/工单」操作词
_ORDER_NO_RE = re.compile(r"ORDER[A-Z0-9]{4,}")
_ORDER_WORD_RE = re.compile(r"订单|工单")

# 知识 = 政策话题词 + 疑问模式同时命中；只有疑问词没有政策话题
# （如「这个商品我能用吗」）分不出类，交给 LLM 兜底
_KNOWLEDGE_TOPIC_RE = re.compile(
    r"退|换|退款|退货|换货|运费|邮费|快递|物流|发货|发票|价保|保修|"
    r"保养|存放|清洗|会员|积分|优惠券|投诉|七天|无理由|赔偿|退货政策"
)
_KNOWLEDGE_QUESTION_RE = re.compile(
    r"多少钱|有没有|是什么|是什么意思|退不退|能不能|可不可以|可以吗|"
    r"行吗|好吗|怎么办|怎么退|怎么换|如何|多久|几天|几号|什么时候|"
    r"谁出|谁付|谁来|吗|呢"
)

# 售后诉求描述词：商品/包装/物流出状况的客观陈述，是售后问题不是投诉
# 或查件。LLM 兜底会把「包装破了」判成投诉（0.75）、「快递好久没动」
# 判成查订单（business）——这类词在投诉规则之后、业务/知识规则之前
# 直接钉进 knowledge
_AFTERSALE_RE = re.compile(
    r"坏了|破了|破损|摔坏|压坏|漏了|开裂|掉漆|失灵|用不了|"
    r"没动静|没更新|没动|不动了|迟迟不到|太慢了|丢件|没收到货"
)


def classify_by_rules(message: str) -> tuple[str, bool]:
    """规则分类：返回 (意图, 是否高置信)；无规则命中时返回 (None, False)。"""
    if _CHITCHAT_RE.search(message):
        return "chitchat", True
    if _COMPLAINT_RE.search(message):
        return "complaint", True
    if _ORDER_NO_RE.search(message) or _ORDER_WORD_RE.search(message):
        return "business", True
    if _AFTERSALE_RE.search(message):
        return "knowledge", True
    if _KNOWLEDGE_TOPIC_RE.search(message) and _KNOWLEDGE_QUESTION_RE.search(message):
        return "knowledge", True
    return None, False


async def classify_intent(message: str, llm) -> IntentResult:
    """完整分类：规则先行，分不出才调一次 LLM 兜底。

    LLM 返回解析失败或置信度低于阈值时按 business 兜底——设计文档
    「解析失败按业务数据处理（宁可进 agent 反问，不可漏）」。
    """
    intent, confident = classify_by_rules(message)
    if confident:
        return IntentResult(intent=intent, confidence=1.0, method="rule")

    try:
        raw = await _ask_llm(message, llm)
    except Exception as exc:  # noqa: BLE001 - 网络/模型故障降级为业务，不断对话
        logger.warning("意图分类 LLM 调用失败：%s", exc)
        return IntentResult(intent="business", confidence=0.0, method="fallback")

    parsed = _parse_llm_json(raw)
    if parsed is not None and parsed["confidence"] >= _CONFIDENCE_THRESHOLD:
        return IntentResult(
            intent=parsed["intent"], confidence=parsed["confidence"], method="llm"
        )

    if parsed is None:
        logger.warning("意图 LLM 返回无法解析：%s", raw[:120])
        reason = "返回无法解析"
    else:
        logger.info("意图 LLM 置信度过低：%s < %.2f", parsed["confidence"], _CONFIDENCE_THRESHOLD)
        reason = "置信度过低"
    return IntentResult(intent="business", confidence=0.0, method="fallback")


async def _ask_llm(message: str, llm) -> str:
    """调一次 LLM 做分类，返回其文本输出。"""
    response = await llm.ainvoke(INTENT_PROMPT.format(message=message))
    content = response.content
    return content if isinstance(content, str) else str(content)


def _parse_llm_json(raw: str) -> dict | None:
    """从 LLM 输出中提取 {"intent", "confidence"}；不合法时返回 None。

    模型可能裹 markdown 代码围栏，先剥掉再解析；intent 不在四类
    之内或 confidence 不是数值时同样视为解析失败。
    """
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("intent") not in (
        "knowledge", "business", "complaint", "chitchat",
    ):
        return None
    confidence = data.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        return None
    return {"intent": data["intent"], "confidence": float(confidence)}
