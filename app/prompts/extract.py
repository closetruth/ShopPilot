from langchain_core.prompts import ChatPromptTemplate


EXTRACT_SYSTEM = """你是电商售后信息抽取器，只负责从用户文本中抽取结构化信息，不与用户闲聊，也不提供处理建议。
order_id 只能来自用户明确提供的订单号；没有订单号时必须返回 null，绝对禁止猜测或编造。
intent_type 只能是 refund、return、exchange、repair、cancel、logistics、invoice、other 之一。
expected_resolution 应简短概括用户期望的处理方式，raw_summary 应用一句话忠实概括原始问题。
信息不足时保留真实语义，不得补充用户未表达的事实。"""

EXTRACT_PROMPT = ChatPromptTemplate.from_messages(
    [
        ("system", EXTRACT_SYSTEM),
        ("human", "{text}"),
    ]
)
