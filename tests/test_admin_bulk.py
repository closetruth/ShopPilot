"""批量导入接口测试。"""

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
def cleanup_ids(client):
    created: list[str] = []
    yield created
    for faq_id in created:
        client.delete(f"/api/admin/faq/{faq_id}")


SAMPLE_MD = """# 售后手册

## 退货流程

客户提交退货申请后，需在七日内寄回商品，逾期申请自动关闭。仓库签收后
48 小时内完成质检，通过后 1 至 3 个工作日原路退款。

### 运费承担

无理由退货的退回运费由客户承担，商家原因则由商家承担。

## 发票开具

订单完成后 90 天内可申请电子发票，金额为实付金额，不含优惠券抵扣。
"""


# ---------- preview ----------


def test_preview_returns_chunks(client):
    resp = client.post(
        "/api/admin/faq/bulk/preview",
        json={"markdown": SAMPLE_MD, "title": "售后手册", "category": "售后"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["title"] == "售后手册"
    assert data["summary"]["chunks"] >= 3
    sections = [c["section"] for c in data["chunks"]]
    assert "运费承担" in sections
    assert "发票开具" in sections


def test_preview_does_not_write(client):
    before = len(store.list_faq())
    resp = client.post(
        "/api/admin/faq/bulk/preview",
        json={"markdown": SAMPLE_MD, "title": "不写库", "category": "测试"},
    )
    assert resp.status_code == 200
    assert len(store.list_faq()) == before, "preview 不应写库"


def test_preview_path_mode(client):
    resp = client.post(
        "/api/admin/faq/bulk/preview",
        json={
            "markdown": SAMPLE_MD,
            "title": "售后手册",
            "category": "售后",
            "section_mode": "path",
        },
    )
    assert resp.status_code == 200
    sections = [c["section"] for c in resp.json()["chunks"]]
    assert any("/" in s for s in sections), f"路径模式应含层级：{sections}"


def test_preview_rejects_empty(client):
    resp = client.post(
        "/api/admin/faq/bulk/preview",
        json={"markdown": "", "title": "x", "category": "y"},
    )
    assert resp.status_code == 422


def test_preview_rejects_oversized(client):
    resp = client.post(
        "/api/admin/faq/bulk/preview",
        json={"markdown": "字" * 100_001, "title": "x", "category": "y"},
    )
    assert resp.status_code == 422


def test_preview_reports_short_chunks(client):
    md = "## 短节\n\n短。\n\n## 长节\n\n这一节的内容足够长，可以正常入库不需要补充。\n"
    resp = client.post(
        "/api/admin/faq/bulk/preview",
        json={"markdown": md, "title": "测试", "category": "测试"},
    )
    data = resp.json()
    assert data["summary"]["too_short"] == 1
    short = [c for c in data["chunks"] if c["chars"] < 20]
    assert short and short[0]["warnings"]


# ---------- bulk ----------


def test_bulk_creates_and_indexes(client, cleanup_ids):
    tag = "批量导入" + uuid.uuid4().hex[:6]
    resp = client.post(
        "/api/admin/faq/bulk",
        json={
            "title": tag,
            "category": "测试分类",
            "items": [
                {"section": "第一节", "content": "这是批量导入的第一条内容，长度足够通过校验。"},
                {"section": "第二节", "content": "这是批量导入的第二条内容，同样足够长，能通过校验。"},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    cleanup_ids.extend(c["id"] for c in data["created"])

    assert data["total"] == 2
    assert data["indexed"] == 2
    assert len(data["created"]) == 2

    for c in data["created"]:
        row = store.faq_by_id(c["id"])
        assert row is not None
        assert row["source"] == "web"
        assert row["title"] == tag


def test_bulk_rejects_invalid_item_with_index(client):
    resp = client.post(
        "/api/admin/faq/bulk",
        json={
            "title": "校验测试",
            "category": "测试",
            "items": [
                {"section": "正常", "content": "这一条是合法的，内容长度足够通过校验要求。"},
                {"section": "太短", "content": "短"},
            ],
        },
    )
    assert resp.status_code == 422
    assert "第 2 条" in resp.json()["detail"]


def test_bulk_all_or_nothing(client):
    before = len(store.list_faq())
    resp = client.post(
        "/api/admin/faq/bulk",
        json={
            "title": "原子性测试",
            "category": "测试",
            "items": [
                {"section": "正常", "content": "这一条是合法的，内容长度足够通过校验要求。"},
                {"section": "坏", "content": "太短"},
            ],
        },
    )
    assert resp.status_code == 422
    assert len(store.list_faq()) == before, "校验失败时不应写入任何条目"


def test_bulk_rejects_too_many_items(client):
    items = [
        {"section": f"节{i}", "content": f"第 {i} 条的内容，长度足够通过校验要求。"}
        for i in range(201)
    ]
    resp = client.post(
        "/api/admin/faq/bulk",
        json={"title": "超量测试", "category": "测试", "items": items},
    )
    assert resp.status_code == 422


def test_bulk_requires_title_and_category(client):
    resp = client.post(
        "/api/admin/faq/bulk",
        json={
            "title": "x",
            "category": "y",
            "items": [{"section": "节", "content": "内容长度足够通过校验的正文内容。"}],
        },
    )
    assert resp.status_code == 422
