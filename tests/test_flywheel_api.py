"""r20 Task 9：飞轮看板数据接口。评估触发用假 runner，不真跑模型。"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.repo import evals


@pytest.fixture
def client(monkeypatch):
    async def fake_run_eval(run_id, *, limit=30, case_ids=None, judge=False, graph=None):
        evals.finish_run(run_id, metrics={"total": 0, "pass_rate": 0.0}, report_path=None)
        return {"run_id": run_id, "metrics": {"total": 0}, "items": []}

    from app.api import flywheel

    monkeypatch.setattr(flywheel, "run_eval", fake_run_eval)

    # 共享真库：清掉上次中断留下的 running 记录，否则触发接口一律 409
    from app.core.db import connect, now

    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE eval_runs SET status = 'failed', finished_at = %s"
            " WHERE status = 'running'",
            (now(),),
        )

    from app.main import app

    with TestClient(app) as c:
        yield c


def test_cost_endpoint_shape(client):
    data = client.get("/api/admin/flywheel/cost", params={"days": 7}).json()
    for key in ("today", "by_node", "by_source", "by_day", "budget_events", "price_configured"):
        assert key in data


def test_feedback_endpoint_shape(client):
    data = client.get("/api/admin/flywheel/feedback", params={"days": 7}).json()
    for key in ("total", "positive", "negative", "negative_rate", "by_reason", "items"):
        assert key in data


def test_trigger_rejects_oversized_limit(client):
    res = client.post("/api/admin/eval/run", json={"limit": 100})
    assert res.status_code == 422


def test_trigger_returns_run_id(client):
    res = client.post("/api/admin/eval/run", json={"limit": 5})
    assert res.status_code == 200
    run_id = res.json()["run_id"]
    detail = client.get(f"/api/admin/eval/runs/{run_id}").json()
    assert detail["run"]["run_id"] == run_id


def test_from_item_creates_candidate(client):
    run_id = uuid.uuid4().hex[:12]
    evals.create_run(run_id, sample_size=1)
    item_id = evals.add_item(
        run_id,
        0,
        "评估失败提问",
        intent="knowledge",
        answer="[P-FAKE-1]",
        cited_ids=["P-FAKE-1"],
        passed=False,
        refused=False,
        checks={"doc_hit": False},
        latency_ms=120,
        cost_micros=0,
    )
    res = client.post(f"/api/admin/eval/cases/from-item/{item_id}")
    assert res.status_code == 200
    assert res.json()["id"] > 0