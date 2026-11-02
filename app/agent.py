"""客服 Agent：LangGraph ReAct 循环，模型自主决定是否调用工具。

r15 起主力 agent 只处理业务数据类意图（知识检索收归图的知识节点），
工具集裁剪为业务四件。停止条件两条：LangGraph 原生 recursion_limit
管步数；token 上限由 usage_metadata 累计判断，两者触线都输出转人工
话术——顾客必须总能拿到一句回答。
"""

from __future__ import annotations

import logging

from langchain_core.messages import AIMessage
from langgraph.errors import GraphRecursionError
from langgraph.prebuilt import create_react_agent

from app.tools import AgentContext, agent_tools

logger = logging.getLogger(__name__)

# 设计文档定值：步数上限走 LangGraph recursion_limit；token 上限含
# 输入+输出累计，触线即中断。工具结果反复变大时 token 会先于步数触线。
STEP_LIMIT = 15
TOKEN_LIMIT = 8000

LIMIT_FALLBACK_TEXT = "这个问题比较复杂，我帮你转人工处理。"

BUSINESS_PROMPT = """你是店铺的在线客服，用简洁、礼貌、口语化的中文回复顾客。

可用工具：
1. lookup_product — 查商品的价格、颜色、库存、简介
2. list_my_orders — 列出当前顾客名下的订单（无需参数）
3. lookup_work_order — 查某个订单的工单处理进度
4. draft_work_order — 生成一张待顾客确认的工单草稿（**不会真正提交**）

工具使用规则：
- 顾客问某件商品有没有、多少钱、什么颜色、有没有货 → 先调 lookup_product
- 顾客提到「我的订单」「我买的东西」「收到的货」，或需要确定是哪一单时，
  **先调 list_my_orders**，不要让顾客凭记忆报订单号
- 顾客报了订单号 → 调 lookup_work_order 查该单的工单情况
- 顾客反映问题且需要跟进时，先 list_my_orders 定位订单，必要时再 lookup_work_order
- 判定「是否重复」只看**问题类型**是否相同，不看问题是否相关：
  已有「退换货」工单时，顾客要提「发货延迟」属于不同类型，应正常起草；
  已有「退换货」工单时，顾客再提「退换货」才是重复
- 工具返回结果后，基于结果回答，不要补充工具没给出的信息
- 订单列表为空时，说明未查到订单，请顾客提供订单号或联系人工
- 工具报参数不合法时按提示修正后重试，最多两次；仍失败则请顾客提供准确信息

工单草稿的规则（重要）：
- 顾客描述的问题需要跟进、信息又足够时，调用 draft_work_order 起草，
  不需要先问「要不要建单」——真正提交由顾客在界面上点按钮完成
- 信息不足时先追问：缺订单号就调 list_my_orders 找并让顾客确认是哪一单，
  描述太笼统就问现象、时间、部位
- 问题类型只能从「运输破损 / 发货延迟 / 质量问题 / 退换货 / 咨询 / 投诉建议 / 其他」中选
- 订单号规则：运输破损 / 发货延迟 / 质量问题 / 退换货 必须有订单号；
  咨询 / 投诉建议 / 其他 不需要订单号，顾客说不清是哪一单、或问题本来
  就与订单无关时（比如投诉服务态度、询问政策），直接用这三类起草，
  不要把「请提供订单号」当成必须的前置条件
- 起草后告诉顾客「核对下方卡片后点确认提交」，**绝对不能说「已提交」、
  不能说「已创建」、不能编造或猜测任何工单号**——工单此时还不存在
- 顾客点确认后系统会自动生成工单，你不需要也不能自己建单
- 若工具返回重复，告知该订单已有同类工单在跟进，不再起草

回复原则：
1. 直接回答，不堆砌客套话，一般三句话以内。
2. 绝不编造工具没有返回的价格、库存、工单状态、时效。工具查不到就说查不到。
3. 涉及退换货政策的具体条款，说明「具体以商品详情页为准」。
4. 顾客问政策规则（退货、运费、发票、物流时效等）时，这不属于你的职责，
   请顾客直接提问即可——系统会由知识库回答；你不要凭记忆编条款。
5. 不使用表情符号，不使用「亲」以外的网络用语。
"""


def build_business_agent(*, llm=None):
    """业务主力 agent：业务四件工具 + 缺信息反问的 prompt。"""
    from app.llm import get_llm

    return create_react_agent(
        model=llm or get_llm(),
        tools=agent_tools(),
        prompt=BUSINESS_PROMPT,
        context_schema=AgentContext,
    )


def _tokens_exceeded(used: int, incoming: int) -> bool:
    """token 上限判定：累计用量 + 本轮即将进入的量是否触线。"""
    return used + incoming > TOKEN_LIMIT


def _usage_of(msg) -> int:
    """一条 AI 消息的 input+output token 数；模型没报就按 0 处理。"""
    usage = getattr(msg, "usage_metadata", None)
    if not usage:
        return 0
    return int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))


async def run_business_agent_with_limit(
    inputs: dict, *, visitor_id: str, llm=None
) -> dict:
    """带双上限的 agent 执行。

    visitor_id 必须显式传入：context 决定业务工具能看到谁的订单/工单，
    硬编码占位符会让四件套全部查空。
    返回 {"final_text", "stopped_by": "step_limit"|"token_limit"|None,
    "tool_calls", "steps", "total_tokens"}。触线不抛异常，返回转人工话术。
    """
    agent = build_business_agent(llm=llm)
    tool_calls: list[str] = []
    steps = 0
    tokens_used = 0
    last_update: dict | None = None

    try:
        async for chunk in agent.astream(
            inputs,
            context=AgentContext(visitor_id=visitor_id),
            config={"recursion_limit": STEP_LIMIT},
            stream_mode="updates",
        ):
            for node_name, update in chunk.items():
                msgs = update.get("messages", []) if isinstance(update, dict) else []
                if node_name == "tools":
                    for msg in msgs:
                        name = getattr(msg, "name", "") or ""
                        if name:
                            tool_calls.append(name)
                if node_name == "agent":
                    steps += 1
                    for msg in msgs:
                        tokens_used += _usage_of(msg)
                    last_update = {node_name: update}

            if _tokens_exceeded(tokens_used, 0):
                logger.warning("主力 agent token 超限 %s，转人工", tokens_used)
                return {
                    "final_text": LIMIT_FALLBACK_TEXT,
                    "stopped_by": "token_limit",
                    "tool_calls": tool_calls,
                    "steps": steps,
                }
    except GraphRecursionError:
        logger.warning("主力 agent 达到步数上限 %s，转人工", STEP_LIMIT)
        return {
            "final_text": LIMIT_FALLBACK_TEXT,
            "stopped_by": "step_limit",
            "tool_calls": tool_calls,
            "steps": steps,
        }

    return {
        "final_text": _last_ai_text(last_update),
        "stopped_by": None,
        "tool_calls": tool_calls,
        "steps": steps,
        "total_tokens": tokens_used or None,
    }


def _last_ai_text(last_update: dict | None) -> str:
    """从最后一个 agent 节点更新里取 AI 文本；取不到给通用兜底。"""
    if last_update:
        for update in last_update.values():
            for msg in reversed(update.get("messages", [])):
                if isinstance(msg, AIMessage) and msg.content:
                    content = msg.content
                    return content if isinstance(content, str) else str(content)
    return "抱歉，我暂时无法回答这个问题，请稍后再试或转人工客服。"