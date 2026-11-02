"""r20 Task 8：评估 runner。假图注入，不连模型。"""

import uuid

import pytest

from app.evals.runner import run_eval
from app.repo import evals as evals_repo


@pytest.mark.anyio
async def test_run_with_empty_case_set_finishes():
    # 共享真库：先把历史遗留的已采纳样本退役，确保「无样本」这一路真被覆盖
    for case in evals_repo.list_cases(status="accepted", limit=200):
        evals_repo.set_case_status(case["id"], "retired")

    run_id = uuid.uuid4().hex[:12]
    evals_repo.create_run(run_id, sample_size=0)
    out = await run_eval(run_id, limit=10)
    assert out["metrics"]["total"] == 0
    assert evals_repo.get_run(run_id)["status"] == "done"


@pytest.mark.anyio
async def test_item_error_does_not_abort_run():
    class BoomGraph:
        async def ainvoke(self, inputs, config=None):
            raise RuntimeError("模型挂了")

    case_id = evals_repo.create_case("失败样本-" + uuid.uuid4().hex[:8])
    evals_repo.set_case_status(case_id, "accepted")
    run_id = uuid.uuid4().hex[:12]
    evals_repo.create_run(run_id, sample_size=1)
    out = await run_eval(run_id, case_ids=[case_id], graph=BoomGraph())
    assert out["metrics"]["error_count"] == 1
    assert evals_repo.get_run(run_id)["status"] == "done"