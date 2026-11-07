"""训练任务接口测试：提交、轮询、切活跃。

远端训练服务用假响应替代——这些用例验的是本地编排逻辑（状态映射、模型
登记、故障降级），真训练在远端容器里跑。
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.core import store
from app.finetune import trainer as trainer_client
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
def dataset_id():
    name = "test-jobs-" + uuid.uuid4().hex[:8]
    return finetune_repo.create_dataset(
        name,
        "/app/data",
        split={"seed": 20261104, "sizes": {"train": 1, "dev": 1, "test": 0}},
        stats={"labeled": 2, "gate": "blocked"},
    )


@pytest.fixture
def fake_submit(monkeypatch):
    """假远端提交：返回一个远端 job id。"""
    monkeypatch.setattr(
        trainer_client,
        "submit",
        lambda params: {"id": "remote-" + uuid.uuid4().hex[:8], "status": "queued"},
    )


def test_submit_creates_local_job(client, dataset_id, fake_submit):
    res = client.post("/api/admin/finetune/jobs", json={"epochs": 2, "lr": 1e-5})
    assert res.status_code == 200, res.text

    job_id = res.json()["id"]
    local = finetune_repo.get_job(job_id)
    assert local is not None
    assert local["status"] == "queued"
    assert local["dataset_id"] == dataset_id
    assert local["params_json"] == {"epochs": 2, "lr": 1e-5}


def test_submit_without_dataset_is_rejected(client, monkeypatch):
    """没有数据集记录时先拒绝，否则任务会挂在一个不存在的 dataset_id 上。"""
    monkeypatch.setattr(finetune_repo, "list_datasets", lambda limit=10: [])
    res = client.post("/api/admin/finetune/jobs", json={})
    assert res.status_code == 409
    assert "数据集" in res.json()["detail"]


def test_submit_when_trainer_down_returns_503(client, dataset_id, monkeypatch):
    """远端没起来是 503 而不是 500：这不是代码 bug，是部署状态。"""

    def boom(params):
        raise trainer_client.TrainerUnavailable("connection refused")

    monkeypatch.setattr(trainer_client, "submit", boom)
    res = client.post("/api/admin/finetune/jobs", json={})
    assert res.status_code == 503
    assert "不可达" in res.json()["detail"]


def test_poll_unknown_job_returns_404(client):
    res = client.get("/api/admin/finetune/jobs/not-a-real-job")
    assert res.status_code == 404


def test_poll_syncs_running_status(client, dataset_id, fake_submit, monkeypatch):
    job_id = client.post("/api/admin/finetune/jobs", json={}).json()["id"]
    monkeypatch.setattr(
        trainer_client,
        "remote_job",
        lambda jid, tail=40: {"id": jid, "status": "running", "log_tail": ["epoch 1"]},
    )

    res = client.get(f"/api/admin/finetune/jobs/{job_id}")
    assert res.status_code == 200
    assert res.json()["status"] == "running"
    assert res.json()["log_tail"] == ["epoch 1"]


def test_poll_success_registers_model(client, dataset_id, fake_submit, monkeypatch):
    """训练成功后要登记模型版本，否则发布页看不到这个版本，也就无从激活。"""
    job_id = client.post("/api/admin/finetune/jobs", json={}).json()["id"]
    version = "r21-test-" + uuid.uuid4().hex[:6]
    monkeypatch.setattr(
        trainer_client,
        "remote_job",
        lambda jid, tail=40: {
            "id": jid,
            "status": "succeeded",
            "log_tail": ["done"],
            "result": {
                "version": version,
                "path": f"/app/models/{version}",
                "onnx": f"/app/models/{version}/model.onnx",
                "base_model": "hfl/chinese-roberta-wwm-ext",
                "best": {"mrr": 0.81, "top1": 0.75, "epoch": 3},
                "test": {"mrr": 0.79, "top1": 0.72},
                "rows": {"train": 120, "dev": 12, "test": 12},
            },
        },
    )

    res = client.get(f"/api/admin/finetune/jobs/{job_id}")
    assert res.status_code == 200
    assert res.json()["status"] == "done"

    versions = {row["version"] for row in finetune_repo.list_models(limit=20)}
    assert version in versions


def test_poll_success_twice_does_not_duplicate_model(
    client, dataset_id, fake_submit, monkeypatch
):
    """轮询是幂等的：页面每秒拉一次，不该每秒插一行模型记录。"""
    job_id = client.post("/api/admin/finetune/jobs", json={}).json()["id"]
    version = "r21-idem-" + uuid.uuid4().hex[:6]
    monkeypatch.setattr(
        trainer_client,
        "remote_job",
        lambda jid, tail=40: {
            "id": jid,
            "status": "succeeded",
            "result": {"version": version, "path": "/app/models/x"},
        },
    )

    client.get(f"/api/admin/finetune/jobs/{job_id}")
    client.get(f"/api/admin/finetune/jobs/{job_id}")

    hits = [
        row for row in finetune_repo.list_models(limit=50)
        if row["version"] == version
    ]
    assert len(hits) == 1


def test_poll_failure_records_error(client, dataset_id, fake_submit, monkeypatch):
    job_id = client.post("/api/admin/finetune/jobs", json={}).json()["id"]
    monkeypatch.setattr(
        trainer_client,
        "remote_job",
        lambda jid, tail=40: {
            "id": jid,
            "status": "failed",
            "error": "CUDA out of memory",
            "log_tail": [],
        },
    )

    res = client.get(f"/api/admin/finetune/jobs/{job_id}")
    assert res.json()["status"] == "failed"
    assert "memory" in res.json()["error"]


def test_poll_when_trainer_down_keeps_local_state(client, dataset_id, fake_submit, monkeypatch):
    """远端连不上时返回本地已有状态，不让页面白屏。"""
    job_id = client.post("/api/admin/finetune/jobs", json={}).json()["id"]

    def boom(jid, tail=40):
        raise trainer_client.TrainerUnavailable("timeout")

    monkeypatch.setattr(trainer_client, "sync_job", boom)
    res = client.get(f"/api/admin/finetune/jobs/{job_id}")
    assert res.status_code == 200
    assert res.json()["status"] == "queued"
    assert res.json()["log_tail"] == []


def test_activate_sets_single_active(client, dataset_id, fake_submit, monkeypatch):
    version = "r21-act-" + uuid.uuid4().hex[:6]
    finetune_repo.add_model(version, "hfl/chinese-roberta-wwm-ext", "/app/models/x", "/app/models/x/model.onnx")
    monkeypatch.setattr(
        trainer_client, "activate_remote", lambda v: {"active": v}
    )

    res = client.post(f"/api/admin/finetune/models/{version}/activate")
    assert res.status_code == 200
    assert res.json()["active"] == version

    active = finetune_repo.active_model()
    assert active is not None and active["version"] == version


def test_activate_unknown_version_is_rejected(client, monkeypatch):
    monkeypatch.setattr(
        trainer_client,
        "activate_remote",
        lambda v: (_ for _ in ()).throw(trainer_client.TrainerError("模型版本不存在")),
    )
    res = client.post("/api/admin/finetune/models/r21-nope/activate")
    assert res.status_code == 409
    assert "不存在" in res.json()["detail"]


def test_trainer_status_passes_through_device(client, monkeypatch):
    """远端有没有卡只有它自己知道，接口如实透传，不猜。"""
    monkeypatch.setattr(
        trainer_client,
        "health",
        lambda: {"status": "ok", "device": {"device": "cpu", "cuda": False}},
    )
    res = client.get("/api/admin/finetune/trainer")
    assert res.status_code == 200
    assert res.json()["device"]["cuda"] is False