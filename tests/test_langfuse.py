"""r20 Task 6：Langfuse 可选件——缺配置、装配失败都不得影响对话。"""

from app.observability import langfuse as lf


def test_disabled_when_keys_missing(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    assert lf.enabled() is False
    assert lf.build_handler() is None


def test_metadata_shape(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    monkeypatch.setenv("LANGFUSE_HOST", "http://127.0.0.1:3005")
    meta = lf.trace_metadata("conv-1", "visitor-1")
    assert meta["langfuse_session_id"] == "conv-1"
    assert meta["langfuse_user_id"] == "visitor-1"
    assert meta["langfuse_tags"] == ["chat"]
    assert meta["visitor_id"] == "visitor-1"


def test_build_handler_failure_is_silent(monkeypatch):
    """导入/构造失败只吞掉，返回 None。"""
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    monkeypatch.setenv("LANGFUSE_HOST", "http://127.0.0.1:3005")
    monkeypatch.setattr(lf, "_make_handler", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert lf.build_handler() is None