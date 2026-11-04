"""微调四张表的仓储测试：标注覆盖、数据集往返、任务状态、版本唯一活跃。

连共享远端 MySQL，与 test_traces.py 相同约定：标注用带随机后缀的 query
防污染（`query_hash` 是全局唯一键），统计断言用前后差值。
"""

import uuid

import pytest

from app.core import store
from app.repo import finetune


@pytest.fixture(scope="module", autouse=True)
def _ensure_schema():
    store.init_db()


def _q(text: str = "退货怎么弄") -> str:
    return f"{text}-{uuid.uuid4().hex[:8]}"


def _version() -> str:
    return "v-" + uuid.uuid4().hex[:8]


def test_upsert_label_overwrites_same_query():
    query = _q()
    first = finetune.upsert_label(query, "doc", faq_id="P-RET-001-1")
    second = finetune.upsert_label(query, "missing", note="知识库没有这条")

    # 同一句话改主意不该产生第二行，否则「当前真值」要靠猜
    assert first == second
    row = finetune.get_label(first)
    assert row["label"] == "missing"
    assert row["faq_id"] is None
    assert row["note"] == "知识库没有这条"


def test_upsert_label_rejects_invalid_combinations():
    with pytest.raises(ValueError):
        finetune.upsert_label(_q(), "doc")
    with pytest.raises(ValueError):
        finetune.upsert_label(_q(), "unknown")


def test_label_stats_counts_by_class():
    before = finetune.label_stats()
    finetune.upsert_label(_q(), "doc", faq_id="P-LOG-001-1")
    finetune.upsert_label(_q(), "noise")
    after = finetune.label_stats()

    assert after["doc"] == before["doc"] + 1
    assert after["noise"] == before["noise"] + 1
    assert after["total"] == before["total"] + 2


def test_list_labels_filters_by_class():
    query = _q()
    finetune.upsert_label(query, "noise")
    rows = finetune.list_labels("noise", limit=500)
    assert any(r["query"] == query for r in rows)
    assert all(r["label"] == "noise" for r in rows)


def test_query_hash_ignores_punctuation_and_case():
    a = finetune.query_hash("能退吗？")
    b = finetune.query_hash("能退吗")
    assert a == b
    assert finetune.query_hash("能 退 吗") == a


def test_dataset_round_trip():
    dataset_id = finetune.create_dataset(
        "r21-test",
        "data/finetune/r21-test.jsonl",
        split={"train": 10, "dev": 2, "test": 2},
        stats={"cleaned": 40, "noise": 7},
    )
    row = finetune.get_dataset(dataset_id)
    assert row["name"] == "r21-test"
    assert row["split_json"]["train"] == 10
    assert row["stats_json"]["cleaned"] == 40

    listed = finetune.list_datasets(5)
    assert any(r["id"] == dataset_id for r in listed)


def test_job_status_flow():
    dataset_id = finetune.create_dataset("r21-job", "data/finetune/x.jsonl")
    job_id = "job-" + uuid.uuid4().hex[:10]
    finetune.create_job(job_id, dataset_id, params={"epochs": 3})

    row = finetune.get_job(job_id)
    assert row["status"] == "queued"
    assert row["finished_at"] is None
    assert row["params_json"]["epochs"] == 3

    finetune.update_job(job_id, status="running")
    finetune.update_job(job_id, status="done", metrics={"hit1": 0.78})

    row = finetune.get_job(job_id)
    assert row["status"] == "done"
    assert row["metrics_json"]["hit1"] == 0.78
    assert row["finished_at"] is not None


def test_running_job_prefers_latest():
    dataset_id = finetune.create_dataset("r21-run", "data/finetune/y.jsonl")
    job_id = "job-" + uuid.uuid4().hex[:10]
    finetune.create_job(job_id, dataset_id, status="running")

    assert finetune.running_job()["id"] == job_id

    finetune.update_job(job_id, status="failed", error="cuda oom")


def test_only_one_active_model():
    first = _version()
    second = _version()
    finetune.add_model(first, "hfl/chinese-roberta-wwm-ext", "/m/1", "/m/1.onnx")
    finetune.add_model(second, "hfl/chinese-roberta-wwm-ext", "/m/2", "/m/2.onnx")

    assert finetune.activate_model(first) is True
    assert finetune.activate_model(second) is True

    # 两个 active 会让推理服务加载哪个取决于查询顺序，等于随机故障
    actives = [m for m in finetune.list_models(200) if m["active"] == 1]
    assert len(actives) == 1
    assert actives[0]["version"] == second
    assert finetune.active_model()["version"] == second

    assert finetune.activate_model("v-not-exist") is False