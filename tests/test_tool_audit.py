"""r19 Task 3：工具审计表与脱敏。

脱敏与截断是纯函数，单独测；落库与分页连共享 MySQL，用独立 visitor_id
防污染，断言用前后差值。
"""

import uuid

import pytest

from app import store
from app.repo import audit


@pytest.fixture(scope="module", autouse=True)
def _ensure_schema():
    store.init_db()


@pytest.fixture
def visitor():
    return "test-" + uuid.uuid4().hex[:12]


@pytest.fixture
def tool_name():
    return "test_tool_" + uuid.uuid4().hex[:8]


# ---------- 纯函数 ----------

def test_mask_args_masks_sensitive_keys():
    masked = audit.mask_args(
        {
            "phone": "13800000000",
            "real_name": "张三",
            "id_card": "110101199001011234",
            "email": "a@b.com",
            "address": "某小区 1 号",
            "order_no": "ORDER20261101",
        }
    )
    assert masked["phone"] == "***"
    assert masked["real_name"] == "***"
    assert masked["id_card"] == "***"
    assert masked["email"] == "***"
    assert masked["address"] == "***"
    assert masked["order_no"] == "ORDER20261101"


def test_mask_args_truncates_long_values():
    long_text = "长" * 400
    masked = audit.mask_args({"description": long_text})
    assert len(masked["description"]) <= audit.ARGS_VALUE_LIMIT + 1
    assert masked["description"].endswith("…")


def test_mask_args_handles_none():
    assert audit.mask_args(None) == {}


def test_digest_truncates():
    assert audit.digest(None) is None
    assert audit.digest({"a": 1}) == '{"a": 1}'
    big = {"k": "x" * 1000}
    assert len(audit.digest(big)) == audit.RESULT_DIGEST_LIMIT


def test_error_text_keeps_type_and_first_line():
    exc = ValueError("第一行\n第二行")
    text = audit._error_text(exc)
    assert text == "ValueError: 第一行"
    assert "\n" not in text


# ---------- 落库与查询 ----------

def test_record_and_list(visitor, tool_name):
    before = len(store.list_tool_audits(tool=tool_name))
    store.record_tool_audit(
        tool_name,
        "builtin",
        "agent",
        "ok",
        visitor_id=visitor,
        args={"keyword": "杯子", "phone": "13800000000"},
        result={"count": 2},
        duration_ms=12,
    )
    logs = store.list_tool_audits(tool=tool_name)
    assert len(logs) == before + 1
    row = logs[0]
    assert row["status"] == "ok"
    assert row["source"] == "builtin"
    assert row["caller"] == "agent"
    assert row["duration_ms"] == 12
    assert row["args_json"]["phone"] == "***"
    assert '"count": 2' in row["result_digest"]


def test_list_filters_by_status_and_visitor(visitor, tool_name):
    store.record_tool_audit(
        tool_name, "builtin", "agent", "denied",
        visitor_id=visitor, args={"a": 1}, error="Denied: 不在白名单",
    )
    store.record_tool_audit(
        tool_name, "builtin", "agent", "ok",
        visitor_id=visitor, args={"a": 2},
    )
    denied = store.list_tool_audits(tool=tool_name, status="denied")
    assert len(denied) == 1
    assert denied[0]["args_json"] == {"a": 1}
    assert "Denied" in denied[0]["error"]

    other = store.list_tool_audits(visitor_id="no-such-visitor")
    assert other == []


def test_list_pagination(visitor, tool_name):
    for i in range(3):
        store.record_tool_audit(
            tool_name, "builtin", "agent", "ok", visitor_id=visitor, args={"i": i}
        )
    first = store.list_tool_audits(tool=tool_name, limit=2, offset=0)
    second = store.list_tool_audits(tool=tool_name, limit=2, offset=2)
    assert len(first) == 2
    assert len(second) == 1
    assert {r["id"] for r in first}.isdisjoint({r["id"] for r in second})
    # 倒序：offset 0 是最新的
    assert first[0]["args_json"]["i"] == 2


def test_write_failure_does_not_raise(monkeypatch):
    """审计写库失败只记日志，调用方不受影响。"""
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(audit, "connect", boom)
    audit.record("x", "builtin", "agent", "ok")  # 不应抛异常