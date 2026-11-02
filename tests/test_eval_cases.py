"""r20 Task 7：评估样本库——候选/采纳/淘汰与三路转候选。"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.repo import evals


def test_create_and_list_candidate():
    q = "样本-" + uuid.uuid4().hex[:8]
    case_id = evals.create_case(q, expected_intent="knowledge", source="manual")
    rows = evals.list_cases(status="candidate", limit=5)
    assert rows[0]["id"] == case_id
    assert rows[0]["expected_behavior"] == "answer"


def test_status_transition():
    case_id = evals.create_case("状态-" + uuid.uuid4().hex[:8])
    assert evals.set_case_status(case_id, "accepted") is True
    assert evals.get_case(case_id)["status"] == "accepted"
    assert evals.set_case_status(case_id, "retired") is True


def test_status_rejects_unknown():
    from app.main import app

    case_id = evals.create_case("非法-" + uuid.uuid4().hex[:8])
    with TestClient(app) as client:
        res = client.post(
            f"/api/admin/eval/cases/{case_id}/status", json={"status": "瞎写"}
        )
    assert res.status_code == 422


def test_from_feedback_requires_query():
    """负反馈没有可回溯的提问时不许造空样本。"""
    from app.main import app

    with TestClient(app) as client:
        res = client.post("/api/admin/eval/cases/from-feedback/999999999")
    assert res.status_code == 404