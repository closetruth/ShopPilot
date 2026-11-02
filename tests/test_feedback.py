"""r20 Task 2：反馈落库与接口。

反馈必须绑定到具体回答，一条回答只留一行；标签与文字均可选。
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.core import store
from app.repo import feedback


@pytest.fixture
def visitor():
    return "fb-" + uuid.uuid4().hex[:8]


@pytest.fixture
def conv(visitor):
    conv_id = store.create_conversation(visitor)
    mid = store.append_message(conv_id, "assistant", "退货运费由买家承担")
    return conv_id, mid


def test_schema_created():
    """六张新表都在，且能查（空结果不算失败）。"""
    from app.core.db import connect

    tables = [
        "message_feedback",
        "llm_usage",
        "budget_events",
        "eval_cases",
        "eval_runs",
        "eval_run_items",
    ]
    with connect() as conn, conn.cursor() as cur:
        for t in tables:
            cur.execute(f"SELECT COUNT(*) AS n FROM {t}")
            assert cur.fetchone()["n"] >= 0


def test_upsert_overwrites_same_message(conv):
    """同一条回答重复提交只留一行——顾客连点不该产生重复记录。"""
    conv_id, mid = conv
    feedback.upsert(mid, conv_id, "v", -1, reason="irrelevant")
    feedback.upsert(mid, conv_id, "v", -1, reason="inaccurate", comment="答错了")
    rows = feedback.by_conversation(conv_id)
    assert len(rows) == 1
    assert rows[0]["reason"] == "inaccurate"
    assert rows[0]["comment"] == "答错了"


def test_remove_on_zero_rating(conv):
    conv_id, mid = conv
    feedback.upsert(mid, conv_id, "v", 1)
    feedback.remove(mid)
    assert feedback.by_conversation(conv_id) == []


def test_api_rejects_foreign_message(conv, visitor):
    """别人的消息不能打反馈，按 404 处理，不泄露存在性。"""
    from app.main import app

    conv_id, mid = conv
    with TestClient(app) as client:
        res = client.post(
            "/api/feedback",
            json={
                "message_id": mid,
                "visitor_id": "someone-else",
                "rating": -1,
            },
        )
    assert res.status_code == 404


def test_api_accepts_rating_without_reason_and_comment(conv, visitor):
    """标签与文字都可选：只给 rating 也必须成功。"""
    from app.main import app

    conv_id, mid = conv
    with TestClient(app) as client:
        res = client.post(
            "/api/feedback",
            json={"message_id": mid, "visitor_id": visitor, "rating": 1},
        )
        assert res.status_code == 200
        assert res.json()["rating"] == 1

        listed = client.get(
            "/api/feedback", params={"conversation_id": conv_id, "visitor_id": visitor}
        ).json()
    assert len(listed["items"]) == 1


def test_api_rejects_unknown_reason(conv, visitor):
    from app.main import app

    conv_id, mid = conv
    with TestClient(app) as client:
        res = client.post(
            "/api/feedback",
            json={
                "message_id": mid,
                "visitor_id": visitor,
                "rating": -1,
                "reason": "瞎编",
            },
        )
    assert res.status_code == 422