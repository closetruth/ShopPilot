"""精排微调的标注与数据集接口。

候选条款由离线脚本 `scripts/finetune_dataset.py` 算好落盘，这里只读缓存：
标注是纯人工动作，翻页不该触发嵌入调用——265 条每题算 5 个候选，每翻一页
就是一次外部 API 账单。

管理接口沿用 `/api/admin/*` 无鉴权约定（仅本地开发）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.core import store
from app.core.db import connect
from app.repo import finetune as finetune_repo

logger = logging.getLogger(__name__)

router = APIRouter()

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "finetune"
_CANDIDATES = _DATA_DIR / "candidates.jsonl"


@router.get("/admin/finetune")
async def finetune_page() -> FileResponse:
    return FileResponse(_STATIC_DIR / "admin_finetune.html")


def _read_snapshot() -> list[dict]:
    """读候选快照。缺失返回空列表，由调用方决定是否兜底。"""
    if not _CANDIDATES.exists():
        return []
    rows: list[dict] = []
    with _CANDIDATES.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _candidates_from_retrieved() -> list[dict]:
    """用库里 `retrieved` 兜底：线上检索当时真实返回的那几条。

    快照要 embedding + Milvus 才算得出，是一次性的离线性产物；忘了重跑就
    会出现「新攒的低置信度问题压根标不了」。而 `retrieved` 本来就是检索
    落库时写下的，直接拿来当候选，标注不必先付一次嵌入账单。

    代价是候选只有 3 条左右、也没有排序分（`score` 置空），所以它是**兜底**
    而不是替代：有快照时仍以快照为准。
    """
    faq = {row["id"]: row for row in store.list_faq(None)}
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, query, retrieved FROM low_confidence_queries ORDER BY id DESC"
        )
        rows = cur.fetchall()

    out: list[dict] = []
    for row in rows:
        try:
            retrieved = json.loads(row["retrieved"] or "[]")
        except (TypeError, ValueError):
            continue
        cands = []
        for faq_id in retrieved:
            item = faq.get(faq_id)
            # 停用条款线上拿不到，展示出来只会让人标一个线上永远不成立的答案
            if not item or not item.get("enabled"):
                continue
            cands.append(
                {
                    "faq_id": item["id"],
                    "title": item.get("title") or "",
                    "category": item.get("category") or "",
                    "section": item.get("section") or "",
                    "content": item.get("content") or "",
                    "score": None,
                }
            )
        out.append(
            {
                "query": row["query"],
                "source": "low_conf",
                "low_conf_id": int(row["id"]),
                "retrieved": retrieved,
                "expected_faq_id": None,
                "candidates": cands,
            }
        )
    return out


def _load_candidates() -> list[dict]:
    """快照优先，快照没覆盖到的问题用库里 `retrieved` 补齐。

    按行合并而不是整份二选一：快照是某一天生成的，之后新攒的低置信度问题
    不在里面。整份二选一的话，要么标不到新问题，要么丢掉快照里更全的候选。
    """
    rows = _read_snapshot()
    seen = {finetune_repo.query_hash(row["query"]) for row in rows}
    for row in _candidates_from_retrieved():
        key = finetune_repo.query_hash(row["query"])
        if key not in seen:
            rows.append(row)
            seen.add(key)
    if not rows:
        raise HTTPException(
            status_code=409,
            detail="候选缓存不存在，先执行：uv run python -m scripts.finetune_dataset",
        )
    return rows


def _labeled_hashes() -> set[str]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT query_hash FROM finetune_labels")
        return {row["query_hash"] for row in cur.fetchall()}


@router.get("/api/admin/finetune/labels/pending")
async def pending_labels(limit: int = 20, offset: int = 0) -> dict:
    """待标清单：候选缓存里还没有人工结论的那些。

    pending 由「缓存全集 − 已标注 hash」算出来，不用额外的状态列——标注
    只有「标过」和「没标过」两态，多存一个 status 只会和真值漂移。
    """
    rows = _load_candidates()
    labeled = _labeled_hashes()
    pending = [
        row for row in rows if finetune_repo.query_hash(row["query"]) not in labeled
    ]
    window = pending[offset : offset + limit]
    return {
        "total": len(pending),
        "all": len(rows),
        "labeled": len(rows) - len(pending),
        "offset": offset,
        "items": window,
    }


class LabelRequest(BaseModel):
    query: str
    label: str
    faq_id: str | None = None
    note: str | None = None
    low_conf_id: int | None = None


@router.post("/api/admin/finetune/labels")
async def submit_label(req: LabelRequest) -> dict:
    if req.label not in finetune_repo.LABELS:
        raise HTTPException(status_code=400, detail=f"未知标注类型：{req.label}")
    if req.label == "doc":
        if not req.faq_id:
            raise HTTPException(status_code=400, detail="标为 doc 时必须给 faq_id")
        if store.faq_by_id(req.faq_id) is None:
            raise HTTPException(
                status_code=400, detail=f"条款不存在：{req.faq_id}"
            )
    try:
        label_id = finetune_repo.upsert_label(
            req.query,
            req.label,
            faq_id=req.faq_id,
            low_conf_id=req.low_conf_id,
            note=req.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": label_id, "stats": finetune_repo.label_stats()}


@router.get("/api/admin/finetune/labels/stats")
async def label_stats() -> dict:
    stats = finetune_repo.label_stats()
    try:
        total = len(_load_candidates())
    except HTTPException:
        total = 0
    return {**stats, "pool": total}


@router.get("/api/admin/finetune/labels")
async def list_labels(label: str | None = None, limit: int = 100) -> dict:
    rows = finetune_repo.list_labels(label, limit=limit)
    return {"items": rows, "stats": finetune_repo.label_stats()}


class BuildRequest(BaseModel):
    seed: int = 20261104
    name: str | None = None


@router.post("/api/admin/finetune/datasets/build")
async def build_dataset_endpoint(req: BuildRequest) -> dict:
    """触发远端容器构建 train/dev/test，并在本地登记一条数据集记录。

    构建本身在容器里跑：标注、FAQ、轨迹都在远端 MySQL，本地构建要先拉
    再传回去，多一次往返还多一个「忘了同步」的坑。这里只负责编排与登记，
    不做第二份构建逻辑——两份实现迟早会分叉。
    """
    from app.finetune import trainer

    try:
        remote = await asyncio.to_thread(trainer.build_dataset_remote, req.seed)
    except trainer.TrainerUnavailable as exc:
        raise HTTPException(
            status_code=503, detail=f"训练服务不可达：{exc}"
        ) from exc
    except trainer.TrainerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    stats = remote["stats"]
    name = req.name or f"r21-{stats['labeled']}-{req.seed}"
    dataset_id = finetune_repo.create_dataset(
        name,
        remote.get("data_dir") or "",
        split={"seed": req.seed, "sizes": stats["splits"]},
        stats=stats,
    )
    return {"id": dataset_id, "name": name, "stats": stats}


@router.get("/api/admin/finetune/datasets")
async def list_datasets(limit: int = 10) -> dict:
    return {"items": finetune_repo.list_datasets(limit=limit)}


class JobRequest(BaseModel):
    version: str | None = None
    epochs: int | None = None
    lr: float | None = None
    batch_size: int | None = None
    max_length: int | None = None
    seed: int | None = None


def _trainer_errors(exc: Exception) -> HTTPException:
    """训练服务不可达与请求被拒分开报。

    混成 500 会让人去翻应用日志，而真正的原因在远端容器里。
    """
    from app.finetune import trainer

    if isinstance(exc, trainer.TrainerUnavailable):
        return HTTPException(status_code=503, detail=f"训练服务不可达：{exc}")
    return HTTPException(status_code=409, detail=str(exc))


@router.get("/api/admin/finetune/trainer")
async def trainer_status() -> dict:
    """训练服务健康与算力。远端有没有卡只有它自己知道，这里如实透传。"""
    from app.finetune import trainer

    try:
        return await asyncio.to_thread(trainer.health)
    except Exception as exc:  # noqa: BLE001 - 探测失败是常态，不是异常路径
        raise _trainer_errors(exc) from exc


@router.post("/api/admin/finetune/jobs")
async def start_job(req: JobRequest) -> dict:
    """提交训练任务。

    数据集必须先在容器内构建过：本地只登记了统计信息，真正喂给模型的
    jsonl 在远端卷里，构建与否由训练服务自己判断。
    """
    from app.finetune import trainer

    datasets = finetune_repo.list_datasets(limit=1)
    if not datasets:
        raise HTTPException(
            status_code=409, detail="还没有数据集记录，先生成 train/dev/test"
        )
    dataset_id = datasets[0]["id"]

    params = req.model_dump(exclude_none=True)
    try:
        remote = await asyncio.to_thread(trainer.submit, params)
    except Exception as exc:  # noqa: BLE001
        raise _trainer_errors(exc) from exc

    job_id = remote["id"]
    finetune_repo.create_job(job_id, dataset_id, params=params, status="queued")
    return {"id": job_id, "status": remote.get("status", "queued")}


@router.get("/api/admin/finetune/jobs/{job_id}")
async def job_status(job_id: str, tail: int = 40) -> dict:
    from app.finetune import trainer

    local = finetune_repo.get_job(job_id)
    if local is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{job_id}")

    try:
        remote = await asyncio.to_thread(trainer.sync_job, job_id, tail)
    except Exception as exc:  # noqa: BLE001 - 同步失败不该让页面白屏
        logger.warning("同步训练任务失败：%s", exc)
        return {**local, "log_tail": []}

    return {**finetune_repo.get_job(job_id), "log_tail": remote.get("log_tail") or []}


@router.get("/api/admin/finetune/jobs")
async def list_jobs(limit: int = 10) -> dict:
    return {"items": finetune_repo.list_jobs(limit=limit)}


@router.get("/api/admin/finetune/models")
async def list_models(limit: int = 10) -> dict:
    from app.finetune import trainer

    try:
        remote = await asyncio.to_thread(trainer.list_remote_models)
    except Exception as exc:  # noqa: BLE001
        raise _trainer_errors(exc) from exc
    return {
        "active": remote.get("active"),
        "models": remote.get("models") or [],
        "local": finetune_repo.list_models(limit=limit),
    }


class EvalRequest(BaseModel):
    e2e_doc_hit_rate: float | None = None
    e2e_baseline: float | None = None


@router.post("/api/admin/finetune/models/{version}/evaluate")
async def evaluate_model(version: str, req: EvalRequest) -> dict:
    """跑三把尺里的离线两把，并把端到端的两个数字一起记进门禁。

    端到端回归要整张对话图，只能在本地跑（`uv run python -m scripts.eval_pipeline`
    之类），跑完把 `doc_hit_rate` 与基线填进来。不填不算「没劣化」，门禁会
    如实拦下——缺数字与数字难看，在发布这件事上等价。
    """
    from app.finetune import trainer

    e2e = None
    if req.e2e_doc_hit_rate is not None and req.e2e_baseline is not None:
        e2e = {"doc_hit_rate": req.e2e_doc_hit_rate, "baseline": req.e2e_baseline}

    try:
        return await asyncio.to_thread(trainer.evaluate_remote, version, e2e)
    except Exception as exc:  # noqa: BLE001
        raise _trainer_errors(exc) from exc


@router.post("/api/admin/finetune/models/{version}/activate")
async def activate_model(version: str) -> dict:
    """切活跃版本。远端先过门禁再写 active 文件，本地只跟着记一笔。"""
    from app.finetune import trainer

    try:
        remote = await asyncio.to_thread(trainer.activate_remote, version)
    except Exception as exc:  # noqa: BLE001
        raise _trainer_errors(exc) from exc

    finetune_repo.activate_model(version)
    return {"active": remote.get("active", version), "gate": remote.get("gate")}