"""后台异步摘要：水位线后原文超预算时压成事实型梗概。

摘要在响应完成后由 asyncio.create_task 触发，不阻塞对话；失败只记
日志，下一轮重新评估。梗概只允许四类事实（问过的商品、报过的
订单号/手机号、明确诉求、未解决的问题），编造禁令写在提示词里并
有测试钉住——客服场景里梗概喂给模型当上下文，编出来的「历史」
会被当成事实继续放大。

水位线取本次读到的消息 max(id) 快照：摘要进行中用户继续聊天时，
新消息不会被错误标记为已覆盖。
"""

from __future__ import annotations

import logging

from app.context.context_builder import context_window
from app.context.tokens import count_messages_tokens, count_tokens
from app.core import store

logger = logging.getLogger(__name__)

SUMMARY_PROMPT = """把下面的客服对话压缩成事实梗概。只提炼四类内容：

1. 顾客问过的商品
2. 顾客报过的订单号、手机号
3. 顾客的明确诉求（想退、想换、要发票、要补偿等）
4. 尚未解决的问题

硬约束：
- 对话里没出现的内容一个字不许编，禁止推测
- 寒暄、闲聊、客套一律不留
- 某类没有就不写该类，宁缺勿滥
{tighten}

对话：
{dialog}

只输出梗概文本，不要任何解释。"""


def _threshold() -> int:
    """摘要触发线：历史预算的 80%。紧凑窗口下就是保底 400 的 320。"""
    window = context_window()
    total = window // 4
    # 与 context_builder 同口径：保底后的实际历史预算
    from app.context.context_builder import HISTORY_BUDGET_FLOOR

    rough_budget = max(total - 3500, HISTORY_BUDGET_FLOOR)  # 3500 ≈ system+tools+输出实测
    return int(rough_budget * 0.8)


async def maybe_summarize(conversation_id: str, *, llm=None) -> dict | None:
    """评估并执行摘要。返回 {"summary", "covered_until"} 或 None。

    llm 可注入（测试）；生产由调用方传 get_llm_quiet()。
    """
    summary = store.get_summary(conversation_id)
    covered_until = summary["covered_until"] if summary else 0
    rows = store.load_history_after(conversation_id, covered_until)
    if not rows:
        return None

    tokens = count_messages_tokens(
        [{"role": r["role"], "content": r["content"]} for r in rows]
    )
    if tokens <= _threshold():
        return None

    # 关键：先读快照再调 LLM，水位线 = 本次读到的 max(id)。
    # 摘要调用耗时数秒，期间用户新发的消息 id 更大，不能被本次覆盖
    watermark = rows[-1]["id"]

    old_summary = summary["summary"] if summary else ""
    dialog = "\n".join(f"{r['role']}: {r['content']}" for r in rows)
    if old_summary:
        dialog = f"【此前梗概】\n{old_summary}\n\n【新增对话】\n{dialog}"

    tighten = ""
    summary_tokens = count_tokens(old_summary) if old_summary else 0
    if summary_tokens > _threshold() * 0.3:
        # 梗概自身超占比上限：重写收紧而非截断（截断可能正好截掉订单号）
        tighten = "\n- 现有梗概过长，压缩到 300 字以内，保留全部订单号与诉求"

    if llm is None:
        from app.core.llm import get_llm_quiet

        llm = get_llm_quiet()

    try:
        response = await llm.ainvoke(
            [{"role": "user", "content": SUMMARY_PROMPT.format(dialog=dialog, tighten=tighten)}]
        )
        content = response.content
        text = content if isinstance(content, str) else str(content)
        text = text.strip()
        if not text:
            logger.warning("摘要 LLM 返回空文本，本轮跳过")
            return None
    except Exception as exc:  # noqa: BLE001 - 摘要失败不阻断，下轮重评
        logger.warning("摘要生成失败：%s", exc)
        return None

    store.upsert_summary(
        conversation_id, text, covered_until=watermark, token_count=count_tokens(text)
    )
    return {"summary": text, "covered_until": watermark}