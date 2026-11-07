"""标注接口测试：候选缓存读取、待标集合计算、提交与校验。

候选缓存用 tmp_path 伪造，不打真实嵌入 API 也不碰 Milvus——标注接口本身
不负责算候选，它只该读文件。
"""

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app.api import finetune as finetune_api
from app.core import store
from app.main import app
from app.repo import finetune as finetune_repo


@pytest.fixture(scope="module", autouse=True)
def _ensure_schema():
    store.init_db()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def candidates(tmp_path, monkeypatch):
    """两行候选缓存，query 带随机后缀避免与真实标注冲突。"""
    suffix = uuid.uuid4().hex[:8]
    path = tmp_path / "candidates.jsonl"
    rows = [
        {
            "query": f"退货怎么弄-{suffix}",
            "source": "low_conf",
            "low_conf_id": 1,
            "retrieved": [],
            "expected_faq_id": None,
            "candidates": [
                {
                    "faq_id": "P-RET-001-1",
                    "title": "退货条件",
                    "category": "退货",
                    "section": "条件",
                    "content": "七天无理由退货，需保持商品完好。",
                    "score": 0.62,
                }
            ],
        },
        {
            "query": f"能便宜点吗-{suffix}",
            "source": "r13",
            "low_conf_id": None,
            "retrieved": [],
            "expected_faq_id": "P-PRI-001-1",
            "candidates": [],
        },
    ]
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(finetune_api, "_CANDIDATES", path)
    # 库里 `retrieved` 兜底会把真实的 300 多条低置信度问题并进来，这几条
    # 用例断言的是快照读取本身，所以把兜底关掉
    monkeypatch.setattr(finetune_api, "_candidates_from_retrieved", lambda: [])
    return rows


def test_page_is_served(client):
    res = client.get("/admin/finetune")
    assert res.status_code == 200
    assert "精排微调" in res.text


def test_pending_returns_unlabeled_with_candidates(client, candidates):
    data = client.get("/api/admin/finetune/labels/pending?limit=20").json()
    assert data["all"] == 2
    assert data["total"] == 2
    assert data["labeled"] == 0
    queries = [item["query"] for item in data["items"]]
    assert queries == [row["query"] for row in candidates]
    assert data["items"][0]["candidates"][0]["faq_id"] == "P-RET-001-1"


def test_pending_shrinks_after_labeling(client, candidates):
    query = candidates[0]["query"]
    res = client.post(
        "/api/admin/finetune/labels",
        json={"query": query, "label": "doc", "faq_id": "P-RET-001-1"},
    )
    assert res.status_code == 200
    assert res.json()["stats"]["doc"] >= 1

    data = client.get("/api/admin/finetune/labels/pending?limit=20").json()
    assert query not in [item["query"] for item in data["items"]]
    assert data["total"] == 1
    assert data["labeled"] == 1


def test_missing_label_needs_no_faq_id(client, candidates):
    res = client.post(
        "/api/admin/finetune/labels",
        json={"query": candidates[1]["query"], "label": "missing"},
    )
    assert res.status_code == 200


def test_doc_label_requires_existing_faq(client):
    res = client.post(
        "/api/admin/finetune/labels",
        json={"query": "测试-" + uuid.uuid4().hex[:6], "label": "doc"},
    )
    assert res.status_code == 400
    assert "faq_id" in res.json()["detail"]

    res = client.post(
        "/api/admin/finetune/labels",
        json={
            "query": "测试-" + uuid.uuid4().hex[:6],
            "label": "doc",
            "faq_id": "P-NOT-EXIST",
        },
    )
    assert res.status_code == 400
    assert "不存在" in res.json()["detail"]


def test_unknown_label_is_rejected(client):
    res = client.post(
        "/api/admin/finetune/labels",
        json={"query": "测试-" + uuid.uuid4().hex[:6], "label": "maybe"},
    )
    assert res.status_code == 400


def test_pending_without_cache_returns_actionable_409(client, tmp_path, monkeypatch):
    """快照缺失且库里也没有可兜底的问题时，报错要带补救指令。

    注意是「两者都空」才算错误：只要库里还有低置信度问题，候选就能从
    `retrieved` 还原出来，不该把标注页整个拦住。
    """
    monkeypatch.setattr(finetune_api, "_CANDIDATES", tmp_path / "missing.jsonl")
    monkeypatch.setattr(finetune_api, "_candidates_from_retrieved", lambda: [])
    res = client.get("/api/admin/finetune/labels/pending")
    assert res.status_code == 409
    assert "finetune_dataset" in res.json()["detail"]


def test_pending_falls_back_to_stored_retrieval(client, tmp_path, monkeypatch):
    """快照缺失时，候选从库里 `retrieved` 还原，标注页不被打死。

    快照要 embedding + Milvus 才算得出。少了它就把标注整页拦掉，等于要求
    标注的人先跑一次离线脚本——而 `retrieved` 早就在库里了。
    """
    monkeypatch.setattr(finetune_api, "_CANDIDATES", tmp_path / "missing.jsonl")
    monkeypatch.setattr(
        finetune_api,
        "_candidates_from_retrieved",
        lambda: [
            {
                "query": "退货怎么弄-" + uuid.uuid4().hex[:8],
                "source": "low_conf",
                "low_conf_id": 7,
                "retrieved": ["P-RET-001-1"],
                "expected_faq_id": None,
                "candidates": [
                    {
                        "faq_id": "P-RET-001-1",
                        "title": "退货条件",
                        "category": "退货",
                        "section": "条件",
                        "content": "七天无理由退货，需保持商品完好。",
                        # 兜底候选没有排序分：它是旧一次检索的结果
                        "score": None,
                    }
                ],
            }
        ],
    )
    data = client.get("/api/admin/finetune/labels/pending").json()
    assert data["total"] == 1
    assert data["items"][0]["candidates"][0]["score"] is None


def test_snapshot_rows_win_over_fallback(client, candidates, monkeypatch):
    """同一句问题同时出现在快照和库里时，用快照那份（它带排序分、候选更全）。"""
    fallback = [
        {
            "query": candidates[0]["query"],
            "source": "low_conf",
            "low_conf_id": 1,
            "retrieved": [],
            "expected_faq_id": None,
            "candidates": [
                {
                    "faq_id": "P-SHP-001-1",
                    "title": "运费规则",
                    "category": "物流",
                    "section": "运费",
                    "content": "满 99 包邮。",
                    "score": None,
                }
            ],
        }
    ]
    monkeypatch.setattr(finetune_api, "_candidates_from_retrieved", lambda: fallback)

    data = client.get("/api/admin/finetune/labels/pending").json()
    assert data["all"] == 2
    hit = [i for i in data["items"] if i["query"] == candidates[0]["query"]][0]
    assert hit["candidates"][0]["faq_id"] == "P-RET-001-1"
    assert hit["candidates"][0]["score"] == 0.62


def test_stats_reports_pool_size(client, candidates):
    data = client.get("/api/admin/finetune/labels/stats").json()
    assert data["pool"] == 2
    assert set(("doc", "missing", "noise", "total")).issubset(data)


def test_build_dataset_registers_and_reports_gate(client, candidates, monkeypatch):
    """构建转发给远端容器，本地只登记。

    这里假造训练服务的响应：真构建发生在远端，本地若也跑一遍就成了第二
    份实现，迟早与容器里的行为分叉。
    """
    from app.finetune import trainer as trainer_client

    stats = {
        "labeled": 2,
        "labels": {"doc": 1, "missing": 0, "noise": 1},
        "positive": 1,
        "splits": {"train": 1, "dev": 1, "test": 0},
        "pairs": {"train": 1, "dev": 0, "test": 0},
        "augment": {"positive": 1, "trace": 0, "synonym": 0,
                    "hard_neg": 0, "noise_neg": 1},
        "by_category": {"退货": {"doc": 1}, "unknown": {"noise": 1}},
        "gate": "blocked",
        "gate_min_positive": 80,
    }
    monkeypatch.setattr(
        trainer_client, "build_dataset_remote",
        lambda seed: {"stats": stats, "data_dir": "/app/data"},
    )

    client.post(
        "/api/admin/finetune/labels",
        json={"query": candidates[0]["query"], "label": "doc", "faq_id": "P-RET-001-1"},
    )
    name = "test-build-" + uuid.uuid4().hex[:6]
    res = client.post(
        "/api/admin/finetune/datasets/build", json={"seed": 20261104, "name": name}
    )
    assert res.status_code == 200, res.text

    data = res.json()
    assert data["name"] == name
    assert data["stats"]["labeled"] == 2
    assert data["stats"]["gate"] == "blocked"
    assert data["stats"]["gate_min_positive"] == 80

    listed = client.get("/api/admin/finetune/datasets?limit=5").json()["items"]
    assert any(item["name"] == name for item in listed)


def test_build_dataset_returns_503_when_trainer_unreachable(client, monkeypatch):
    """训练服务没起来要和代码出错区分开，否则会去翻错日志。"""
    from app.finetune import trainer as trainer_client

    def boom(seed):
        raise trainer_client.TrainerUnavailable("connection refused")

    monkeypatch.setattr(trainer_client, "build_dataset_remote", boom)
    res = client.post(
        "/api/admin/finetune/datasets/build", json={"seed": 20261104}
    )
    assert res.status_code == 503
    assert "不可达" in res.json()["detail"]


def test_evaluate_forwards_e2e_numbers(client, monkeypatch):
    """端到端两个数字必须原样带给远端。

    门禁判的是远端写下的 eval.json，这里若把数字吞掉，容器里就永远是
    「没跑端到端」，模型再怎么好也过不了门禁。
    """
    from app.finetune import trainer as trainer_client

    captured = {}

    def fake_eval(version, e2e=None):
        captured["version"] = version
        captured["e2e"] = e2e
        return {"eval": {"test": {"hit1": 0.8, "mrr3": 0.85}}, "gate": {"ok": True}}

    monkeypatch.setattr(trainer_client, "evaluate_remote", fake_eval)
    res = client.post(
        "/api/admin/finetune/models/r21-test/evaluate",
        json={"e2e_doc_hit_rate": 0.92, "e2e_baseline": 0.93},
    )
    assert res.status_code == 200, res.text
    assert captured["version"] == "r21-test"
    assert captured["e2e"] == {"doc_hit_rate": 0.92, "baseline": 0.93}
    assert res.json()["gate"]["ok"] is True


def test_evaluate_without_e2e_passes_none(client, monkeypatch):
    """没填端到端就是「没跑」，不能拿半截数字凑合，更不能默认填个 1.0。"""
    from app.finetune import trainer as trainer_client

    captured = {}
    monkeypatch.setattr(
        trainer_client,
        "evaluate_remote",
        lambda version, e2e=None: captured.update(e2e=e2e) or {"eval": {}, "gate": {}},
    )
    res = client.post("/api/admin/finetune/models/r21-test/evaluate", json={})
    assert res.status_code == 200, res.text
    assert captured["e2e"] is None


def test_activate_passes_through_gate(client, monkeypatch):
    """被门禁拒绝时本地不该记成已激活。

    远端 409 会抛 TrainerError，本地那句 activate_model 就不会执行——两边
    状态一致，页面上的「活跃」不会指向一个没真正上线的版本。
    """
    from app.finetune import trainer as trainer_client

    def refused(version):
        raise trainer_client.TrainerError("门禁未通过：test_hit1")

    monkeypatch.setattr(trainer_client, "activate_remote", refused)
    before = finetune_repo.list_models(limit=10)

    res = client.post("/api/admin/finetune/models/r21-test/activate")
    assert res.status_code == 409
    assert "门禁" in res.json()["detail"]

    assert finetune_repo.list_models(limit=10) == before