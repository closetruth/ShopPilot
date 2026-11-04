"""精排客户端测试：自建服务与托管服务走同一段代码，只有地址不同。

这里断言三件事：地址可配、失败一定降级、空输入不发起请求。精排是锦上
添花，任何情况下都不该让检索报错。
"""

import pytest

from app.retrieval import rerank


@pytest.fixture(autouse=True)
def _clear_key_cache():
    # _api_key 有 lru_cache，跨用例会串环境变量
    rerank._api_key.cache_clear()
    yield
    rerank._api_key.cache_clear()


@pytest.fixture
def faq_rows(monkeypatch):
    rows = [
        {"id": "P-RET-001-1", "title": "退货条件", "section": "条件",
         "content": "七天无理由退货。", "category": "退货", "doc_id": "P-RET-001"},
        {"id": "P-SHIP-001-1", "title": "运费规则", "section": "承担方",
         "content": "质量问题运费由商家承担。", "category": "运费", "doc_id": "P-SHIP-001"},
    ]
    monkeypatch.setattr(rerank.store, "all_faq_for_index", lambda: rows)
    return rows


def test_endpoint_defaults_to_siliconflow(monkeypatch):
    monkeypatch.delenv("RERANK_BASE_URL", raising=False)
    assert rerank.endpoint() == "https://api.siliconflow.cn/v1/rerank"


def test_endpoint_accepts_base_url_without_v1(monkeypatch):
    monkeypatch.setenv("RERANK_BASE_URL", "http://192.168.6.23:8010/")
    assert rerank.endpoint() == "http://192.168.6.23:8010/v1/rerank"


def test_endpoint_does_not_double_v1(monkeypatch):
    monkeypatch.setenv("RERANK_BASE_URL", "http://192.168.6.23:8010/v1")
    assert rerank.endpoint() == "http://192.168.6.23:8010/v1/rerank"


def test_self_hosted_needs_no_api_key(monkeypatch, faq_rows):
    """自建服务不该被硅基流动的 key 卡住，否则部署完还要配一个用不上的密钥。"""
    monkeypatch.setenv("RERANK_BASE_URL", "http://127.0.0.1:8010")
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)

    captured = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": [{"index": 1, "relevance_score": 0.9}]}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs.get("headers")
        captured["json"] = kwargs.get("json")
        return _Resp()

    monkeypatch.setattr(rerank.httpx, "post", fake_post)
    out = rerank.rerank("运费谁出", ["P-RET-001-1", "P-SHIP-001-1"], top_n=2)

    assert captured["url"] == "http://127.0.0.1:8010/v1/rerank"
    assert captured["headers"] == {}
    assert captured["json"]["model"] == rerank.DEFAULT_MODEL
    assert out == [("P-SHIP-001-1", 0.9)]


def test_hosted_without_key_degrades_without_calling(monkeypatch, faq_rows):
    monkeypatch.delenv("RERANK_BASE_URL", raising=False)
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)

    called = {"count": 0}

    def fake_post(*args, **kwargs):
        called["count"] += 1
        raise AssertionError("没有 key 时不该发请求")

    monkeypatch.setattr(rerank.httpx, "post", fake_post)
    out = rerank.rerank("退货怎么弄", ["P-RET-001-1"], top_n=1)

    assert called["count"] == 0
    assert out == [("P-RET-001-1", 0.0)]


def test_service_failure_falls_back_to_original_order(monkeypatch, faq_rows):
    monkeypatch.setenv("RERANK_BASE_URL", "http://127.0.0.1:8010")
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)

    def fake_post(*args, **kwargs):
        raise rerank.httpx.ConnectError("connection refused")

    monkeypatch.setattr(rerank.httpx, "post", fake_post)
    ids = ["P-RET-001-1", "P-SHIP-001-1"]
    out = rerank.rerank("退货怎么弄", ids, top_n=2)

    assert [doc_id for doc_id, _ in out] == ids
    assert all(score == 0.0 for _, score in out)


def test_strict_mode_raises_on_service_failure(monkeypatch, faq_rows):
    """strict 下失败必须抛，不能返回一堆 0 分。

    0 分在降级路径里只是「没有重排」的占位；如果调用方拿它去过阈值闸，
    每条都会判为不相关，本来能答的问题会全部被拒掉。
    """
    monkeypatch.setenv("RERANK_BASE_URL", "http://127.0.0.1:8010")
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)

    def fake_post(*args, **kwargs):
        raise rerank.httpx.ConnectError("connection refused")

    monkeypatch.setattr(rerank.httpx, "post", fake_post)
    with pytest.raises(rerank.RerankUnavailable):
        rerank.rerank("退货怎么弄", ["P-RET-001-1"], top_n=1, strict=True)


def test_strict_mode_raises_on_missing_hosted_key(monkeypatch, faq_rows):
    monkeypatch.delenv("RERANK_BASE_URL", raising=False)
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)

    with pytest.raises(rerank.RerankUnavailable):
        rerank.rerank("退货怎么弄", ["P-RET-001-1"], top_n=1, strict=True)


def test_empty_docs_returns_empty(monkeypatch):
    monkeypatch.setenv("RERANK_BASE_URL", "http://127.0.0.1:8010")

    def fake_post(*args, **kwargs):
        raise AssertionError("空输入不该发请求")

    monkeypatch.setattr(rerank.httpx, "post", fake_post)
    assert rerank.rerank("退货", [], top_n=3) == []


def test_unknown_doc_ids_are_skipped(monkeypatch, faq_rows):
    monkeypatch.setenv("RERANK_BASE_URL", "http://127.0.0.1:8010")

    def fake_post(*args, **kwargs):
        raise AssertionError("文档全都不存在时不该发请求")

    monkeypatch.setattr(rerank.httpx, "post", fake_post)
    assert rerank.rerank("退货", ["P-NOT-EXIST"], top_n=1) == []


def test_rerank_model_env_is_passed_through(monkeypatch, faq_rows):
    """切到微调模型只改环境变量，代码里不该有版本分支。"""
    monkeypatch.setenv("RERANK_BASE_URL", "http://127.0.0.1:8010")
    monkeypatch.setenv("RERANK_MODEL", "r21-20261104-120000")
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)

    captured = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": [{"index": 0, "relevance_score": 0.5}]}

    def fake_post(url, **kwargs):
        captured.update(kwargs.get("json"))
        return _Resp()

    monkeypatch.setattr(rerank.httpx, "post", fake_post)
    rerank.rerank("退货怎么弄", ["P-RET-001-1"], top_n=1)

    assert captured["model"] == "r21-20261104-120000"