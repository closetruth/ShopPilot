"""评估 runner：跑一轮样本、落库、写 Markdown 报告。

服务内后台任务优先——Milvus Lite 独占数据文件，服务运行时 CLI 打不开。
graph 可注入，测试用假图，不连模型。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from app.core import store
from app.evals.metrics import aggregate, score_case
from app.observability.usage import build_usage_handler
from app.repo import evals as evals_repo

logger = logging.getLogger(__name__)

REPORT_DIR = Path(__file__).resolve().parents[2] / "docs" / "eval"


async def run_eval(
    run_id: str,
    *,
    limit: int = 30,
    case_ids: list[int] | None = None,
    judge: bool = False,
    graph=None,
) -> dict:
    """跑一轮评估并落库 + 写 Markdown 报告。"""
    cases = _select_cases(limit=limit, case_ids=case_ids)
    if not cases:
        metrics = aggregate([])
        evals_repo.finish_run(run_id, metrics=metrics, report_path=None)
        return {"run_id": run_id, "metrics": metrics, "items": []}

    if graph is None:
        from app.api._graph import get_graph
        from app.core.llm import get_llm_quiet

        graph = await get_graph(get_llm_quiet)

    items: list[dict] = []
    for case in cases:
        items.append(await _run_case(run_id, case, graph=graph))

    metrics = aggregate(items)
    report_path = _write_report(run_id, metrics, items)
    evals_repo.finish_run(run_id, metrics=metrics, report_path=report_path)
    return {"run_id": run_id, "metrics": metrics, "items": items}


def _select_cases(*, limit: int, case_ids: list[int] | None) -> list[dict]:
    if case_ids:
        picked = [store.get_eval_case(cid) for cid in case_ids]
        return [c for c in picked if c]
    return store.list_eval_cases(status="accepted", limit=limit)


async def _run_case(run_id: str, case: dict, *, graph) -> dict:
    """单样本异常只记该 item 的 error，不中断整轮。"""
    case_id = case["id"]
    query = case["query"]
    handler = build_usage_handler(conversation_id=None, visitor_id="eval", source="eval")
    config = {
        "configurable": {"thread_id": f"eval-{run_id}-{case_id}"},
        "callbacks": [handler],
        "metadata": {"shoppilot_source": "eval", "shoppilot_visitor_id": "eval"},
    }
    started = time.monotonic()

    try:
        result = await graph.ainvoke(
            {"message": query, "history": [], "visitor_id": "eval"}, config=config
        )
    except Exception as exc:  # noqa: BLE001 - 单样本失败不拖垮整轮
        latency_ms = int((time.monotonic() - started) * 1000)
        item_id = evals_repo.add_item(
            run_id,
            case_id,
            query,
            latency_ms=latency_ms,
            cost_micros=handler.total_cost_micros,
            error=str(exc)[:255],
        )
        return _item_payload(item_id, case_id, query, error=str(exc))

    latency_ms = int((time.monotonic() - started) * 1000)
    answer = _text_of(result.get("final_text"))
    intent = result.get("intent")
    insufficient = bool(result.get("insufficient"))

    scored = score_case(
        case,
        intent=intent,
        answer=answer,
        insufficient=insufficient,
        faq_lookup=store.faq_by_id,
    )
    item_id = evals_repo.add_item(
        run_id,
        case_id,
        query,
        intent=intent,
        answer=answer,
        cited_ids=scored["cited_ids"],
        passed=scored["passed"],
        refused=scored["refused"],
        checks=scored["checks"],
        latency_ms=latency_ms,
        cost_micros=handler.total_cost_micros,
    )
    return _item_payload(
        item_id,
        case_id,
        query,
        passed=scored["passed"],
        checks=scored["checks"],
        latency_ms=latency_ms,
        cost_micros=handler.total_cost_micros,
    )


def _item_payload(item_id, case_id, query, **kwargs) -> dict:
    return {"id": item_id, "case_id": case_id, "query": query, **kwargs}


def _text_of(value) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _write_report(run_id: str, metrics: dict, items: list[dict]) -> str | None:
    """报告是给人看的产物，写失败只记日志，不影响评估结果落库。"""
    try:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        path = REPORT_DIR / f"r20-{run_id}.md"
        lines = [
            f"# 评估报告 {run_id}",
            "",
            "## 指标",
            "",
            "| 指标 | 值 |",
            "| --- | --- |",
        ]
        for key in (
            "total",
            "passed",
            "error_count",
            "pass_rate",
            "intent_accuracy",
            "doc_hit_rate",
            "citation_valid_rate",
            "refusal_accuracy",
            "latency_p50_ms",
            "cost_micros_total",
        ):
            lines.append(f"| {key} | {metrics.get(key)} |")

        failed = [it for it in items if not it.get("passed")]
        lines.extend(["", "## 失败项", ""])
        if not failed:
            lines.append("本轮无失败项。")
        for item in failed:
            lines.extend(
                [
                    f"### {item['query']}",
                    "",
                    f"- case_id: {item['case_id']}",
                    f"- checks: {item.get('checks')}",
                    f"- error: {item.get('error') or '无'}",
                ]
            )
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(path)
    except Exception:  # noqa: BLE001 - 报告失败不影响主流程
        logger.warning("评估报告写入失败", exc_info=True)
        return None