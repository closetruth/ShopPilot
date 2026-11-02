"""r15 Task 4：指代消解模块。

对话进入意图识别之前，若消息含代词或极短且有未决话题，就用近几轮历史
把它改写成独立问题；否则原样透传，不增加延迟。

两个入口：
- needs_resolution: 纯函数，判断是否需要消解
- resolve_reference: 异步函数，调 LLM 改写；任何异常返回原消息
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 代词列表：消息中出现这些词视为需要消解
_PRONOUNS = ("它", "这个", "那个", "该", "上述", "他", "她")
# 极短阈值：字符数 ≤ 此值视为极短
_SHORT_THRESHOLD = 6

# 改写 prompt：嵌在模块里，不依赖外部文件
_RESOLVE_PROMPT = (
    "你是一个指代消解助手。根据对话历史，把用户最新消息中的代词或省略部分\n"
    "替换为具体实体，输出一个可以独立理解的问题。\n"
    "规则：\n"
    "- 只输出改写后的问题，不要任何解释\n"
    "- 如果消息已经独立清晰，原样输出\n"
    "- 历史格式为 role: content，每轮一行\n\n"
    "对话历史：\n{history}\n\n"
    "用户最新消息：{message}\n\n"
    "改写结果："
)


def needs_resolution(message: str, pending_topic: str) -> bool:
    """判断消息是否需要指代消解。

    触发条件（满足任一）：
    - 消息含代词（它/这个/那个/该/上述/他/她）
    - 消息极短（≤6 字）且有未决话题
    """
    if any(p in message for p in _PRONOUNS):
        return True
    if len(message) <= _SHORT_THRESHOLD and pending_topic:
        return True
    return False


async def resolve_reference(
    message: str, history: list[dict], llm
) -> str:
    """调 LLM 把消息改写成独立问题；任何异常返回原消息。

    Args:
        message: 用户最新消息
        history: 最近几轮对话，每条为 {"role": ..., "content": ...}
        llm: 带 ainvoke 方法的 LLM 对象（返回 AIMessage）

    Returns:
        改写后的消息；失败时返回原 message
    """
    # 只取最近 4 轮
    recent = history[-4:] if len(history) > 4 else history
    history_text = "\n".join(f"{h['role']}: {h['content']}" for h in recent)
    prompt = _RESOLVE_PROMPT.format(history=history_text, message=message)

    try:
        from langchain_core.messages import HumanMessage

        result = await llm.ainvoke([HumanMessage(content=prompt)])
        rewritten = result.content.strip()
        return rewritten if rewritten else message
    except Exception as e:
        logger.warning("指代消解失败，返回原消息: %s", e)
        return message
