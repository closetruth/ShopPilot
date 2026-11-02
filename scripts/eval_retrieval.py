"""四策略对比评估。

对 473 条标注样本跑 vector / bm25 / hybrid / hybrid_rerank 四种策略，
按总体、业务大类、措辞分桶三个维度算 Recall@K 与 MRR，并统计延迟。

用法：
    uv run python -m scripts.eval_retrieval            # 全量
    uv run python -m scripts.eval_retrieval --limit 50 # 先试跑
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import time
from collections import defaultdict
from pathlib import Path

from app.retrieval import retrieval
from app.core import store

EVAL_CSV = Path("docs/eval/r13-queries.csv")
OUT_JSON = Path("docs/eval/r13-results.json")
KS = (1, 3, 5, 10)


def load_queries() -> list[dict]:
    rows = list(csv.DictReader(EVAL_CSV.open(encoding="utf-8")))
    usable = []
    for row in rows:
        expected = [
            d.strip() for d in row["expected_doc_id"].split("|") if d.strip()
        ]
        if not expected:
            continue  # none 的样本不参与指标计算
        usable.append(
            {
                "query": row["query"],
                "expected": expected,
                "bucket": row["bucket"],
                "family": row["family"],
            }
        )
    return usable


def evaluate(
    queries: list[dict], strategy: str, top_k: int = 10
) -> dict:
    """跑一种策略，返回指标与延迟。

    命中口径分两种：
    - any_hit@K  : topK 里至少出现一个期望条目
    - all_hit@K  : topK 里包含全部期望条目（多标签才有意义）
    MRR 用最早出现的那个期望条目的排名。
    """
    per_query = []
    latencies = []

    for item in queries:
        start = time.perf_counter()
        try:
            hits = retrieval.retrieve(item["query"], strategy=strategy, top_k=top_k)
        except Exception as exc:  # noqa: BLE001 - 单条失败不能中断整轮评估
            hits = []
            print(f"  [{strategy}] 检索失败：{item['query'][:24]} — {exc}")
        latencies.append((time.perf_counter() - start) * 1000)

        got = [h["id"] for h in hits]
        expected = set(item["expected"])

        first_rank = None
        for rank, doc_id in enumerate(got, start=1):
            if doc_id in expected:
                first_rank = rank
                break

        per_query.append(
            {
                "query": item["query"],
                "bucket": item["bucket"],
                "family": item["family"],
                "expected": item["expected"],
                "got": got,
                "first_rank": first_rank,
                "hit_count": len(expected & set(got)),
                "expected_count": len(expected),
            }
        )

    def recall(k: int) -> float:
        return sum(
            1 for p in per_query
            if p["first_rank"] is not None and p["first_rank"] <= k
        ) / len(per_query)

    def all_recall(k: int) -> float:
        return sum(
            1 for p in per_query if p["hit_count"] == p["expected_count"] > 0
        ) / len(per_query)

    def mrr(k: int = 10) -> float:
        total = 0.0
        for p in per_query:
            if p["first_rank"] is not None and p["first_rank"] <= k:
                total += 1.0 / p["first_rank"]
        return total / len(per_query)

    latencies.sort()
    return {
        "strategy": strategy,
        "n": len(per_query),
        "recall": {f"@{k}": round(recall(k), 4) for k in KS},
        "all_hit": {f"@{k}": round(all_recall(k), 4) for k in KS},
        "mrr@10": round(mrr(10), 4),
        "latency_ms": {
            "p50": round(statistics.median(latencies), 1),
            "p95": round(latencies[int(len(latencies) * 0.95) - 1], 1),
            "mean": round(statistics.mean(latencies), 1),
        },
        "per_query": per_query,
    }


def group_eval(result: dict, key: str) -> dict:
    """按 bucket 或 family 分组算 Recall@3 与 MRR。"""
    groups: dict[str, list[dict]] = defaultdict(list)
    for item in result["per_query"]:
        groups[item[key]].append(item)

    out = {}
    for name, items in sorted(groups.items()):
        hit3 = sum(
            1 for p in items
            if p["first_rank"] is not None and p["first_rank"] <= 3
        )
        mrr = sum(
            1.0 / p["first_rank"] for p in items
            if p["first_rank"] is not None and p["first_rank"] <= 10
        )
        out[name] = {
            "n": len(items),
            "recall@3": round(hit3 / len(items), 4),
            "mrr@10": round(mrr / len(items), 4),
        }
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    store.init_db()
    queries = load_queries()
    if args.limit:
        queries = queries[: args.limit]
    print(f"评估样本 {len(queries)} 条\n")

    results = {}
    for strategy in retrieval.STRATEGIES:
        print(f"跑 {strategy} …")
        result = evaluate(queries, strategy)
        results[strategy] = result
        r = result["recall"]
        print(
            f"  R@1 {r['@1']:.3f}  R@3 {r['@3']:.3f}  "
            f"R@10 {r['@10']:.3f}  MRR@10 {result['mrr@10']:.3f}  "
            f"P50 {result['latency_ms']['p50']:.0f}ms\n"
        )

    payload = {
        "n": len(queries),
        "results": {
            name: {
                k: v for k, v in res.items() if k != "per_query"
            }
            for name, res in results.items()
        },
        "by_bucket": {
            name: group_eval(res, "bucket") for name, res in results.items()
        },
        "by_family": {
            name: group_eval(res, "family") for name, res in results.items()
        },
        "failures": {
            name: [
                p for p in res["per_query"] if p["first_rank"] is None
            ][:20]
            for name, res in results.items()
        },
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"结果已写入 {OUT_JSON}")


if __name__ == "__main__":
    main()
