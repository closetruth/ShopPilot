"""知识库管理接口测试。

覆盖录入界面依赖的增删改查与索引联动。需要 MySQL 与嵌入 API。
"""

import os
import uuid

import pytest

from app import store


pytestmark = pytest.mark.skipif(
    not os.getenv("SILICONFLOW_API_KEY", "").strip(),
    reason="未配置 SILICONFLOW_API_KEY",
)


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def created(client):
    """新建一条知识，测试结束后删除。"""
    tag = "测试知识" + uuid.uuid4().hex[:6]
    resp = client.post(
        "/api/admin/faq",
        json={
            "title": tag,
            "section": "测试小节",
            "category": "测试分类",
            "content": f"这是关于{tag}的说明内容，需要足够长以通过二十字的校验门槛。",
        },
    )
    assert resp.status_code == 200, resp.text
    faq_id = resp.json()["id"]
    yield {"id": faq_id, "tag": tag}
    client.delete(f"/api/admin/faq/{faq_id}")


def test_create_returns_item(created):
    item = created
    row = store.faq_by_id(item["id"])
    assert row is not None
    assert row["source"] == "web"
    assert row["enabled"] == 1


def test_list_contains_created(client, created):
    resp = client.get("/api/admin/faq")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert any(i["id"] == created["id"] for i in items)


def test_categories_include_new(client, created):
    resp = client.get("/api/admin/faq/categories")
    assert resp.status_code == 200
    names = [c["category"] for c in resp.json()["categories"]]
    assert "测试分类" in names


def test_update_changes_content(client, created):
    resp = client.put(
        f"/api/admin/faq/{created['id']}",
        json={
            "title": created["tag"],
            "section": "改后小节",
            "category": "测试分类",
            "content": "内容已经被完全改写，这段文字用于验证更新是否真正落库生效。",
        },
    )
    assert resp.status_code == 200
    row = store.faq_by_id(created["id"])
    assert row["section"] == "改后小节"
    assert "完全改写" in row["content"]


def test_update_missing_returns_404(client):
    resp = client.put(
        "/api/admin/faq/does-not-exist",
        json={
            "title": "标题",
            "section": "小节",
            "category": "分类",
            "content": "内容足够长以通过校验的正文内容，这里再补几个字。",
        },
    )
    assert resp.status_code == 404


def test_toggle_disables_and_reenables(client, created):
    off = client.post(f"/api/admin/faq/{created['id']}/toggle?enabled=false")
    assert off.status_code == 200
    assert store.faq_by_id(created["id"])["enabled"] == 0

    on = client.post(f"/api/admin/faq/{created['id']}/toggle?enabled=true")
    assert on.status_code == 200
    assert store.faq_by_id(created["id"])["enabled"] == 1


def test_disabled_excluded_from_index_list(client, created):
    client.post(f"/api/admin/faq/{created['id']}/toggle?enabled=false")
    indexed = [r["id"] for r in store.all_faq_for_index()]
    assert created["id"] not in indexed


def test_delete_removes_row(client):
    resp = client.post(
        "/api/admin/faq",
        json={
            "title": "待删除知识",
            "section": "待删小节",
            "category": "测试分类",
            "content": "这条知识创建后立刻删除，用于验证删除接口是否真正移除记录。",
        },
    )
    faq_id = resp.json()["id"]
    assert client.delete(f"/api/admin/faq/{faq_id}").status_code == 200
    assert store.faq_by_id(faq_id) is None


def test_delete_missing_returns_404(client):
    assert client.delete("/api/admin/faq/does-not-exist").status_code == 404


@pytest.mark.parametrize(
    "payload,reason",
    [
        ({"title": "短", "section": "小节", "category": "分类",
          "content": "内容足够长以通过校验的正文内容，这里再补几个字。"}, "标题过短"),
        ({"title": "正常标题", "section": "短", "category": "分类",
          "content": "内容足够长以通过校验的正文内容，这里再补几个字。"}, "小节过短"),
        ({"title": "正常标题", "section": "正常小节", "category": "",
          "content": "内容足够长以通过校验的正文内容，这里再补几个字。"}, "分类为空"),
        ({"title": "正常标题", "section": "正常小节", "category": "分类",
          "content": "太短"}, "正文过短"),
    ],
)
def test_validation_rejects_bad_payload(client, payload, reason):
    resp = client.post("/api/admin/faq", json=payload)
    assert resp.status_code == 422, f"{reason} 应被拒绝，实际 {resp.status_code}"


def test_search_debug_endpoint_returns_scores(client, created):
    resp = client.get(
        "/api/admin/faq/search", params={"q": created["tag"], "top_k": 3}
    )
    assert resp.status_code == 200
    hits = resp.json()["hits"]
    assert hits, "新建的知识应能被调试端点检索到"
    assert any(h["source_id"] == created["id"] for h in hits)
    assert all(0 <= h["score"] <= 1.5 for h in hits)
