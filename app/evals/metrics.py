"""确定性评估指标。

引用命中、引用合规、拒答准确、意图准确——四项全部可复算，不依赖
模型裁判。LLM 裁判只做可选增强，不进默认判定。
"""

from __future__ import annotations

import re

_CITATION = re.compile(r"\[([A-Za-z0-9\-]+)\]")
# 条目 id 形态：P-RET-001（文档）+ 可选版本段 -3
_DOC_ID = re.compile(r"^(P-[A-Z]+-\d{3})(?:-\d+)?$")


def extract_citations(text: str) -> list[str]:
    """按出现顺序去重提取引用标记。"""
    seen: list[str] = []
    for match in _CITATION.finditer(text or ""):
        cid = match.group(1)
        if cid not in seen:
            seen.append(cid)
    return seen


def doc_id_of(faq_id: str) -> str:
    """条目 id 去掉末段版本号：P-RET-001-3 → P-RET-001。

    不能简单砍掉最后一个数字段——P-RET-001 的 001 是文档序号，不是版本。
    """
    if not faq_id:
        return faq_id
    match = _DOC_ID.match(faq_id)
    return match.group(1) if match else faq_id


def score_case(
    case: dict,
    *,
    intent: str | None,
    answer: str,
    insufficient: bool,
    faq_lookup,
) -> dict:
    """只在 case 声明了对应期望时才打分；缺什么不算什么。"""
    checks: dict[str, bool] = {}
    cited = extract_citations(answer)

    expected_intent = case.get("expected_intent")
    if expected_intent:
        checks["intent"] = intent == expected_intent

    behavior = case.get("expected_behavior") or "answer"
    if behavior == "refuse":
        checks["refusal"] = bool(insufficient)
        checks["no_false_citation"] = not cited
    else:
        expected_docs = case.get("expected_doc_ids") or []
        if expected_docs:
            cited_docs = {doc_id_of(c) for c in cited}
            checks["doc_hit"] = any(doc_id_of(d) in cited_docs for d in expected_docs)
        if cited:
            checks["citation_valid"] = all(faq_lookup(c) is not None for c in cited)

    return {
        "passed": all(checks.values()),
        "checks": checks,
        "cited_ids": cited,
        "refused": bool(insufficient),
    }


def aggregate(items: list[dict]) -> dict:
    """比率在分母为 0 时返回 0.0，不许除零。"""
    total = len(items)
    passed = sum(1 for it in items if it.get("passed"))
    error_count = sum(1 for it in items if it.get("error"))
    latencies = sorted(it["latency_ms"] for it in items if it.get("latency_ms"))
    cost_total = sum(it.get("cost_micros") or 0 for it in items)

    return {
        "total": total,
        "passed": passed,
        "error_count": error_count,
        "pass_rate": _rate(passed, total),
        "intent_accuracy": _check_rate(items, "intent"),
        "doc_hit_rate": _check_rate(items, "doc_hit"),
        "citation_valid_rate": _check_rate(items, "citation_valid"),
        "refusal_accuracy": _check_rate(items, "refusal"),
        "latency_p50_ms": _p50(latencies),
        "cost_micros_total": cost_total,
    }


def _check_rate(items: list[dict], key: str) -> float:
    """分母只算真正打了这项分的样本——没声明期望的不该拉低比率。"""
    scored = [it for it in items if key in (it.get("checks") or {})]
    ok = sum(1 for it in scored if it["checks"][key])
    return _rate(ok, len(scored))


def _rate(numerator: int, denominator: int) -> float:
    return (numerator / denominator) if denominator else 0.0


def _p50(sorted_values: list[int]) -> int | None:
    if not sorted_values:
        return None
    return int(sorted_values[len(sorted_values) // 2])