"""离线跑一轮评估。服务运行时 Milvus Lite 被独占，请先停服务。

阈值未达标退出码 1，可直接挂计划任务。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

THRESHOLDS = {"pass_rate": 0.8, "citation_valid_rate": 0.95}


def main() -> int:
    ap = argparse.ArgumentParser(description="跑一轮离线评估")
    ap.add_argument("--limit", type=int, default=30, help="样本数上限（1-50）")
    ap.add_argument("--judge", action="store_true", help="启用 LLM 裁判（可选增强）")
    args = ap.parse_args()

    if not 1 <= args.limit <= 50:
        print("样本数需为 1-50 的整数", file=sys.stderr)
        return 2

    from app.core import store
    from app.evals.runner import run_eval
    from app.repo import evals as evals_repo

    store.init_db()
    run_id = uuid.uuid4().hex[:12]
    evals_repo.create_run(run_id, sample_size=args.limit, config={"judge": args.judge})
    result = asyncio.run(run_eval(run_id, limit=args.limit, judge=args.judge))
    metrics = result["metrics"]

    print(f"run_id: {run_id}")
    for key, value in metrics.items():
        print(f"  {key}: {value}")

    failed = [k for k, floor in THRESHOLDS.items() if metrics.get(k, 0.0) < floor]
    if failed:
        print(f"阈值未达标：{', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())