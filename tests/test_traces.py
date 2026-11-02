"""agent_traces 轨迹表测试：写入、倒序读取、意图统计与 detail JSON 往返。

连共享远端 MySQL，与 test_store.py 相同约定：独立 visitor_id 防污染，
统计断言用前后差值而非绝对值，避免其他测试/历史数据干扰。
"""

import uuid

import pytest

from app.core import store


@pytest.fixture(scope="module", autouse=True)
def _ensure_schema():
    """r15 新表靠 init_db 幂等建出；共享库上首次运行必须先跑一次。"""
    store.init_db()


@pytest.fixture
def visitor():
    """每个测试用独立 visitor_id，避免共享 MySQL 库时相互污染。"""
    return "test-" + uuid.uuid4().hex[:12]


def _conv() -> str:
    return "conv-" + uuid.uuid4().hex[:12]


def test_record_and_list(visitor):
    store.record_trace(_conv(), visitor, "你好", intent="chitchat",
                       intent_method="rule", node_path="classify>chitchat",
                       latency_ms=12)
    traces = store.list_traces(10)
    assert traces[0]["intent"] == "chitchat"
    assert traces[0]["intent_method"] == "rule"
    assert traces[0]["node_path"] == "classify>chitchat"
    assert traces[0]["latency_ms"] == 12

    # total 恒等于各意图计数之和（intent 非空）
    stats = store.trace_stats()
    assert stats["total"] == sum(stats["by_intent"].values())


def test_list_newest_first(visitor):
    store.record_trace(_conv(), visitor, "第一条", intent="chitchat",
                       intent_method="rule", node_path="classify>chitchat",
                       latency_ms=5)
    store.record_trace(_conv(), visitor, "第二条", intent="business",
                       intent_method="llm", node_path="classify>business",
                       latency_ms=40)

    traces = store.list_traces(10)
    assert traces[0]["message"] == "第二条"
    assert traces[1]["message"] == "第一条"


def test_detail_json_roundtrip(visitor):
    detail = {"classify_ms": 3, "generate_ms": 40, "nodes": {"knowledge": 55}}
    store.record_trace(_conv(), visitor, "退货运费谁出", intent="knowledge",
                       intent_method="rule", node_path="classify>knowledge",
                       latency_ms=98, detail=detail,
                       resolved_query="退货运费谁出", confidence=0.9,
                       top_score=0.83, tool_calls=["search_knowledge"],
                       steps=1, total_tokens=512)

    trace = store.list_traces(5)[0]
    assert trace["detail"] == detail
    assert trace["resolved_query"] == "退货运费谁出"
    assert trace["confidence"] == pytest.approx(0.9)
    assert trace["top_score"] == pytest.approx(0.83)
    assert trace["tool_calls"] == "search_knowledge"  # list → 逗号分隔
    assert trace["steps"] == 1
    assert trace["total_tokens"] == 512


def test_optional_fields_default_to_none(visitor):
    store.record_trace(_conv(), visitor, "你好", intent="chitchat",
                       intent_method="rule", node_path="classify>chitchat",
                       latency_ms=12)

    trace = store.list_traces(5)[0]
    assert trace["detail"] is None
    assert trace["resolved_query"] is None
    assert trace["confidence"] is None
    assert trace["tool_calls"] is None
    assert trace["steps"] is None
    assert trace["total_tokens"] is None


def test_stats_by_intent_counts(visitor):
    """共享库上无法断言绝对值，用写入前后的差值钉住统计行为。"""
    before = store.trace_stats()["by_intent"].get("complaint", 0)
    store.record_trace(_conv(), visitor, "我要投诉", intent="complaint",
                       intent_method="rule", node_path="classify>complaint",
                       latency_ms=8)
    store.record_trace(_conv(), visitor, "再投诉一次", intent="complaint",
                       intent_method="rule", node_path="classify>complaint",
                       latency_ms=8)

    after = store.trace_stats()["by_intent"]["complaint"]
    assert after - before == 2


def test_write_failure_does_not_raise(visitor, monkeypatch):
    """轨迹是观测数据，写入失败只记日志，不能拖垮对话。"""

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr("app.repo.insights.connect", _boom)
    store.record_trace(_conv(), visitor, "你好", intent="chitchat",
                       intent_method="rule", node_path="classify>chitchat",
                       latency_ms=1)