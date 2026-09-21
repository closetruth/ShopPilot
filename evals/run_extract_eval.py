import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.extract_service import extract_aftersales


DATASET_PATH = Path(__file__).with_name("aftersales_extract.jsonl")
PASS_THRESHOLD = 0.6


def load_cases() -> list[dict[str, Any]]:
    with DATASET_PATH.open(encoding="utf-8") as dataset:
        return [json.loads(line) for line in dataset if line.strip()]


async def run_eval() -> int:
    if os.getenv("SKIP_LIVE_EVAL") == "1":
        print(
            "SKIP_LIVE_EVAL=1: skipped live extraction evaluation; "
            "unset it and configure .env to run against the model."
        )
        return 0

    cases = load_cases()
    passed = 0

    for case in cases:
        actual = await extract_aftersales(case["text"])
        expected = case["expect"]
        is_pass = (
            actual.intent_type == expected["intent_type"]
            and actual.order_id == expected["order_id"]
        )
        passed += int(is_pass)
        print(
            f"{case['id']}: {'PASS' if is_pass else 'FAIL'} "
            f"expected={expected} "
            f"actual={{'order_id': {actual.order_id!r}, "
            f"'intent_type': {actual.intent_type!r}}}"
        )

    pass_rate = passed / len(cases) if cases else 0.0
    print(f"Result: {passed}/{len(cases)} passed ({pass_rate:.1%})")
    return 0 if pass_rate >= PASS_THRESHOLD else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run_eval()))
