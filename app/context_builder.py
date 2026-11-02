"""token 预算的上下文组装。

每轮给模型看的上下文 = system 提示词 + 工具定义 + 梗概（水位线前）
+ 预算内原文（水位线后）。预算从模型窗口倒推：窗口 × 0.25，减去
system/tools/输出余量/本轮输入，剩余分给历史（保底 400 token——
紧凑窗口下 system+工具就可能吃满预算，无保底会每轮全截光）。

账目随 bundle 返回，落 trace 后上下文构成全程可观测。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from app import store
from app.tokens import count_messages_tokens, count_tokens

# 输出余量：给模型回复留的空间，不参与历史分配
OUTPUT_RESERVE = 1024

# 历史预算保底：低于此值按此值算（紧凑窗口的兜底）
HISTORY_BUDGET_FLOOR = 400

# 梗概占比上限（超出触发摘要重写收紧）
SUMMARY_SHARE = 0.3


@dataclass(frozen=True)
class ContextBundle:
    """一轮对话的上下文构成。

    summary_text 是水位线前历史的梗概（无梗概为 None）；
    recent_messages 是水位线后的预算内原文（含 id，供后续水位线计算）。
    """

    summary_text: str | None
    recent_messages: list[dict] = field(default_factory=list)
    budget: dict = field(default_factory=dict)


def context_window() -> int:
    """配置的模型窗口。默认 8192（紧凑，让压缩机制可触发可验收）；
    生产配真实窗口（如 deepseek 的 131072）。"""
    return int(os.getenv("CONTEXT_WINDOW", "8192"))


def _system_tokens() -> int:
    from app.agent import BUSINESS_PROMPT

    return count_tokens(BUSINESS_PROMPT)


def _tools_tokens() -> int:
    """工具定义的 token 实测：四件工具的名称+描述+参数说明拼接。

    用 tool_call_schema（排除 ToolRuntime 注入字段）——args_schema
    含 callable 字段，model_json_schema 会炸。
    """
    from app.tools import agent_tools

    import json

    parts = []
    for t in agent_tools():
        parts.append(t.name)
        parts.append(t.description or "")
        try:
            parts.append(json.dumps(t.tool_call_schema.model_json_schema(), ensure_ascii=False))
        except Exception:  # noqa: BLE001 - schema 导不出就只数名称与描述
            pass
    return count_tokens("\n".join(parts))


def build_context(conversation_id: str, current_message: str) -> ContextBundle:
    """组装本轮上下文并出账。先组装后 append_message——本轮输入作为
    扣减项参与预算，不与原文窗口重复计费。"""
    window = context_window()
    total = window // 4
    system = _system_tokens()
    tools = _tools_tokens()
    turn_input = count_tokens(current_message) + 4

    history_budget = max(
        total - system - tools - OUTPUT_RESERVE - turn_input, HISTORY_BUDGET_FLOOR
    )

    summary = store.get_summary(conversation_id)
    summary_text = summary["summary"] if summary else None
    covered_until = summary["covered_until"] if summary else 0

    # 梗概占比约束（观测用；重写收紧在 summarizer 里处理）
    summary_tokens = count_tokens(summary_text) if summary_text else 0

    # 原文窗口：预算减去梗概占用后的部分给原文
    recent_budget = max(history_budget - summary_tokens, HISTORY_BUDGET_FLOOR // 2)

    rows = store.load_history_after(conversation_id, covered_until)
    dropped = 0
    kept: list[dict] = []
    kept_tokens = 0
    # 从新往旧收，收满预算为止；剩下的记 dropped
    for row in reversed(rows):
        cost = count_tokens(row["content"]) + 4
        if kept and kept_tokens + cost > recent_budget:
            break
        kept.append(row)
        kept_tokens += cost
    kept.reverse()
    dropped = len(rows) - len(kept)

    budget = {
        "window": window,
        "total": total,
        "system": system,
        "tools": tools,
        "output_reserve": OUTPUT_RESERVE,
        "turn_input": turn_input,
        "history_budget": history_budget,
        "summary_tokens": summary_tokens,
        "recent_tokens": kept_tokens,
        "dropped": dropped,
        "summary_covered_until": covered_until,
    }

    return ContextBundle(
        summary_text=summary_text,
        recent_messages=[{"role": r["role"], "content": r["content"]} for r in kept],
        budget=budget,
    )