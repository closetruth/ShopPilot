"""精排微调的数据资产：标注、数据集、训练任务、模型版本。

四张表各有明确职责，不混用：

- `finetune_labels`  人工标注结论，是训练集的唯一真值来源
- `finetune_datasets` 一次数据集构建的产物与统计，可复算
- `finetune_jobs`    训练任务状态，远端容器是执行者，这里是账本
- `finetune_models`  训练产物版本，`active` 决定线上用哪个

标注不复用 `eval_cases`：评估基线要稳定，微调标注会频繁推翻重标，
两者放一起会让 r20 的回归数字变得不可解释。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from app.core.db import connect, now

LABELS = ("doc", "missing", "noise")

_PUNCT_RE = re.compile(r"[^\w\u4e00-\u9fff]+")


def normalize_query(query: str) -> str:
    """标注去重用的归一化：去掉标点与所有空白、统一小写。

    只服务 `query_hash` 唯一键，不改变送进模型的原文——训练样本仍用原话，
    否则会把口语特征洗掉。空白一并去掉，是因为中文里「能 退 吗」和
    「能退吗」是同一个问题，留着空格会让同一句话标两次。
    """
    return _PUNCT_RE.sub("", (query or "").lower())


def query_hash(query: str) -> str:
    return hashlib.md5(normalize_query(query).encode("utf-8")).hexdigest()


def _dump(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def _load(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def upsert_label(
    query: str,
    label: str,
    *,
    faq_id: str | None = None,
    low_conf_id: int | None = None,
    note: str | None = None,
    annotator: str = "human",
) -> int:
    """写一条标注。同一句话重复标注覆盖旧结论，不产生第二行。

    覆盖而不是追加，是因为标注会改主意（先标 missing，扩了知识库后改标
    doc）。保留多行会让「当前真值是哪条」变成需要额外规则推断的问题。
    """
    if label not in LABELS:
        raise ValueError(f"未知标注类型：{label}")
    if label == "doc" and not faq_id:
        raise ValueError("标为 doc 时必须给出 faq_id")
    stamp = now()
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO finetune_labels
                (query, query_hash, low_conf_id, faq_id, label, note,
                 annotator, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                low_conf_id = VALUES(low_conf_id),
                faq_id      = VALUES(faq_id),
                label       = VALUES(label),
                note        = VALUES(note),
                annotator   = VALUES(annotator),
                updated_at  = VALUES(updated_at)
            """,
            (
                query[:512],
                query_hash(query),
                low_conf_id,
                faq_id if label == "doc" else None,
                label,
                note,
                annotator,
                stamp,
                stamp,
            ),
        )
        cur.execute(
            "SELECT id FROM finetune_labels WHERE query_hash = %s",
            (query_hash(query),),
        )
        return int(cur.fetchone()["id"])


def get_label(label_id: int) -> dict | None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, query, query_hash, low_conf_id, faq_id, label, note,
                   annotator, created_at, updated_at
              FROM finetune_labels
             WHERE id = %s
            """,
            (label_id,),
        )
        return cur.fetchone()


def list_labels(
    label: str | None = None,
    *,
    limit: int = 200,
    offset: int = 0,
) -> list[dict]:
    sql = (
        "SELECT id, query, low_conf_id, faq_id, label, note, annotator,"
        "       updated_at FROM finetune_labels"
    )
    params: tuple = ()
    if label is not None:
        sql += " WHERE label = %s"
        params = (label,)
    sql += " ORDER BY id LIMIT %s OFFSET %s"
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params + (limit, offset))
        return list(cur.fetchall())


def label_stats() -> dict:
    """标注进度：三类各多少、总多少。

    前端进度条只认这个接口，不在浏览器里累加，避免分页导致数字对不上。
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT label, COUNT(*) AS n FROM finetune_labels GROUP BY label"
        )
        rows = cur.fetchall()
    counts = {row["label"]: int(row["n"]) for row in rows}
    return {
        "doc": counts.get("doc", 0),
        "missing": counts.get("missing", 0),
        "noise": counts.get("noise", 0),
        "total": sum(counts.values()),
    }


def create_dataset(
    name: str,
    path: str,
    *,
    split: dict | None = None,
    stats: dict | None = None,
) -> int:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO finetune_datasets
                (name, path, split_json, stats_json, created_at)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (name[:64], path[:255], _dump(split), _dump(stats), now()),
        )
        return int(cur.lastrowid)


def get_dataset(dataset_id: int) -> dict | None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, name, path, split_json, stats_json, created_at
              FROM finetune_datasets
             WHERE id = %s
            """,
            (dataset_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    row["split_json"] = _load(row["split_json"])
    row["stats_json"] = _load(row["stats_json"])
    return row


def list_datasets(limit: int = 20) -> list[dict]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, name, path, stats_json, created_at
              FROM finetune_datasets
             ORDER BY id DESC
             LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
    for row in rows:
        row["stats_json"] = _load(row["stats_json"])
    return list(rows)


def create_job(
    job_id: str,
    dataset_id: int,
    *,
    params: dict | None = None,
    status: str = "queued",
    log_path: str | None = None,
) -> str:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO finetune_jobs
                (id, dataset_id, status, params_json, log_path, started_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                job_id[:32],
                dataset_id,
                status[:16],
                _dump(params),
                log_path,
                now(),
            ),
        )
    return job_id


def update_job(
    job_id: str,
    *,
    status: str | None = None,
    metrics: dict | None = None,
    log_path: str | None = None,
    error: str | None = None,
) -> None:
    fields: list[str] = []
    params: list[Any] = []
    if status is not None:
        fields.append("status = %s")
        params.append(status[:16])
    if metrics is not None:
        fields.append("metrics_json = %s")
        params.append(_dump(metrics))
    if log_path is not None:
        fields.append("log_path = %s")
        params.append(log_path[:255])
    if error is not None:
        fields.append("error = %s")
        params.append(error[:255])
    if status in ("done", "failed"):
        fields.append("finished_at = %s")
        params.append(now())
    if not fields:
        return
    params.append(job_id)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"UPDATE finetune_jobs SET {', '.join(fields)} WHERE id = %s",
            tuple(params),
        )


def get_job(job_id: str) -> dict | None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, dataset_id, status, params_json, metrics_json,
                   log_path, error, started_at, finished_at
              FROM finetune_jobs
             WHERE id = %s
            """,
            (job_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    row["params_json"] = _load(row["params_json"])
    row["metrics_json"] = _load(row["metrics_json"])
    return row


def list_jobs(limit: int = 20) -> list[dict]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, dataset_id, status, metrics_json, error,
                   started_at, finished_at
              FROM finetune_jobs
             ORDER BY started_at DESC
             LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
    for row in rows:
        row["metrics_json"] = _load(row["metrics_json"])
    return list(rows)


def running_job() -> dict | None:
    """当前是否已有训练在跑。

    同一时间只允许一个训练任务：远端只有一份模型卷，并发写会互相覆盖版本
    目录，失败后无法判断坏的是哪次。
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, dataset_id, status, started_at
              FROM finetune_jobs
             WHERE status IN ('queued', 'running')
             ORDER BY started_at DESC
             LIMIT 1
            """
        )
        return cur.fetchone()


def add_model(
    version: str,
    base_model: str,
    model_dir: str,
    onnx_path: str,
    *,
    metrics: dict | None = None,
) -> int:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO finetune_models
                (version, base_model, model_dir, onnx_path, metrics_json,
                 active, created_at)
            VALUES (%s, %s, %s, %s, %s, 0, %s)
            """,
            (
                version[:32],
                base_model[:64],
                model_dir[:255],
                onnx_path[:255],
                _dump(metrics),
                now(),
            ),
        )
        return int(cur.lastrowid)


def list_models(limit: int = 20) -> list[dict]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, version, base_model, model_dir, onnx_path, metrics_json,
                   active, created_at
              FROM finetune_models
             ORDER BY id DESC
             LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
    for row in rows:
        row["metrics_json"] = _load(row["metrics_json"])
    return list(rows)


def activate_model(version: str) -> bool:
    """把某个版本设为唯一活跃版本。

    先全置 0 再置 1，两步在同一事务里：只置 1 不清 0 会出现两个 active，
    推理服务重启后加载哪个取决于查询顺序，等于埋随机故障。
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM finetune_models WHERE version = %s", (version,)
        )
        row = cur.fetchone()
        if row is None:
            return False
        cur.execute("UPDATE finetune_models SET active = 0 WHERE active = 1")
        cur.execute(
            "UPDATE finetune_models SET active = 1 WHERE id = %s", (row["id"],)
        )
    return True


def active_model() -> dict | None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, version, base_model, model_dir, onnx_path, metrics_json,
                   active, created_at
              FROM finetune_models
             WHERE active = 1
             LIMIT 1
            """
        )
        row = cur.fetchone()
    if row is None:
        return None
    row["metrics_json"] = _load(row["metrics_json"])
    return row