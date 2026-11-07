"""训练服务：提交训练任务、查进度、切活跃模型，并挂载精排推理路由。

远端算力未知（docs/dev-notes/r21.md），所以这里启动时探测 CUDA 并如实
报出来，不自报 GPU。训练在后台线程跑，日志落文件——一次训练几分钟到
几十分钟，HTTP 请求等不起。

鉴权用共享令牌 `TRAINER_TOKEN`，留空即不校验。这是内网临时方案，
不是长期设计。
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

import rerank_service

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
MODELS_DIR = Path(os.getenv("MODELS_DIR", "/app/models"))
LOGS_DIR = Path(os.getenv("LOGS_DIR", "/app/logs"))
CANDIDATES_DIR = Path(os.getenv("CANDIDATES_DIR", "/app/candidates"))
TOKEN = os.getenv("TRAINER_TOKEN", "").strip()

router = APIRouter()
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def require_token(x_trainer_token: str | None = Header(default=None)) -> None:
    if TOKEN and x_trainer_token != TOKEN:
        raise HTTPException(status_code=401, detail="训练令牌无效")


class TrainRequest(BaseModel):
    version: str | None = None
    epochs: int | None = None
    lr: float | None = None
    batch_size: int | None = None
    max_length: int | None = None
    seed: int | None = None


class BuildRequest(BaseModel):
    seed: int = 20261104


class EvalRequest(BaseModel):
    """端到端那把尺由本地 app 侧跑完带进来。

    容器里没有对话图，也没有可用的 Milvus（Lite 数据文件被运行中的服务
    独占），所以第三把尺只能传数进来，不在这里算。
    """

    e2e: dict | None = None


def _count_pairs(path: Path) -> tuple[int, int]:
    """数一份 jsonl 里的正负例条数。文件不存在按 0 条算。"""
    positives = negatives = 0
    if not path.exists():
        return positives, negatives
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("label") == 1:
                positives += 1
            elif row.get("label") == 0:
                negatives += 1
    return positives, negatives


def _dataset_gate() -> dict:
    """开训前的硬性检查。

    这三条都是「不满足就必然白训」的硬伤，而不是可以事后调参的软指标：

    - 没有 train.jsonl：压根没构建；
    - 训练集没有负例：BCE 的最优解是给所有输入打高分，loss 照样下降，
      但模型没有任何区分能力，训完还会显示「训练成功」；
    - 缺评估池：dev 只能在自己几条正例里排序，MRR 与随机排序无法区分，
      早停会按噪声挑轮次。

    放在入口拦下来，比训完让人看指标发现不对便宜得多。
    """
    train_path = DATA_DIR / "train.jsonl"
    pool_path = DATA_DIR / "faq_pool.jsonl"
    positives, negatives = _count_pairs(train_path)
    pool_size = 0
    if pool_path.exists():
        with pool_path.open(encoding="utf-8") as handle:
            pool_size = sum(1 for line in handle if line.strip())

    problems: list[str] = []
    if not train_path.exists():
        problems.append("还没有构建训练集，先点「构建数据集」")
    elif negatives == 0:
        problems.append(
            f"训练集 {positives} 条全是正例，没有负例可学；"
            "多半是知识库里没有其他条款可抽"
        )
    if pool_size < 2:
        problems.append("评估池为空或只有一条，无法计算有意义的排序指标")

    return {
        "ok": not problems,
        "problems": problems,
        "train_positives": positives,
        "train_negatives": negatives,
        "pool": pool_size,
    }


def _candidates_path() -> Path | None:
    """检索快照是可选的。

    没有它只是少了 hard negative（同批候选里分最高但不对的条款），训练
    仍能跑；有它模型才学得到「为什么这条不是答案」。所以缺失不报错，
    只记一条日志。
    """
    path = CANDIDATES_DIR / "candidates.jsonl"
    return path if path.exists() else None


@router.post("/datasets/build", dependencies=[Depends(require_token)])
async def build_dataset_endpoint(req: BuildRequest) -> dict:
    """在容器内从远端 MySQL 现取标注并重建 train/dev/test。

    放在这里而不是本地，是因为标注、FAQ、轨迹本来就在远端 MySQL 里。
    本地构建再传过来，等于多一次网络往返，还多一个「忘了同步」的失败
    模式——训练跑在旧标签上，指标好看但模型是错的。
    """
    import asyncio

    from scripts.finetune_dataset import build_dataset

    candidates = _candidates_path()
    result = await asyncio.to_thread(
        build_dataset,
        seed=req.seed,
        candidates_path=candidates,
        out_dir=DATA_DIR,
    )
    if not result["ok"]:
        raise HTTPException(status_code=409, detail=result["message"])

    stats = result["stats"]
    stats["candidates"] = candidates is not None
    return {"stats": stats, "data_dir": str(DATA_DIR)}


@router.get("/datasets")
async def dataset_info() -> dict:
    stats_path = DATA_DIR / "dataset_stats.json"
    gate = _dataset_gate()
    if not stats_path.exists():
        return {"built": False, "gate": gate}
    return {
        "built": True,
        "stats": json.loads(stats_path.read_text(encoding="utf-8")),
        "candidates": _candidates_path() is not None,
        "gate": gate,
    }


def _device_info() -> dict:
    try:
        import torch

        available = torch.cuda.is_available()
        return {
            "device": "cuda" if available else "cpu",
            "cuda": available,
            "gpu": torch.cuda.get_device_name(0) if available else None,
        }
    except Exception as exc:  # noqa: BLE001 - 缺 torch 时如实降级，不冒充
        return {"device": "unknown", "cuda": False, "gpu": None, "error": str(exc)}


def _log_path(job_id: str) -> Path:
    return LOGS_DIR / f"{job_id}.log"


def _append_log(job_id: str, line: str) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with _log_path(job_id).open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _run_job(job_id: str, req: TrainRequest) -> None:
    from train import TrainConfig, train

    with _jobs_lock:
        _jobs[job_id]["status"] = "running"
        _jobs[job_id]["started_at"] = datetime.now(timezone.utc).isoformat()

    def log(line: str) -> None:
        _append_log(job_id, line)

    try:
        config = TrainConfig(
            data_dir=DATA_DIR,
            out_dir=MODELS_DIR,
            version=req.version or "",
            epochs=req.epochs or TrainConfig.epochs,
            lr=req.lr or TrainConfig.lr,
            batch_size=req.batch_size or 0,
            max_length=req.max_length or TrainConfig.max_length,
            seed=req.seed or TrainConfig.seed,
        )
        result = train(config, log=log)
        with _jobs_lock:
            _jobs[job_id].update(
                status="succeeded",
                result=result,
                finished_at=datetime.now(timezone.utc).isoformat(),
            )
    except Exception as exc:  # noqa: BLE001 - 失败要留全量日志给远端排查
        log(f"训练失败：{type(exc).__name__}: {exc}")
        with _jobs_lock:
            _jobs[job_id].update(
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
                finished_at=datetime.now(timezone.utc).isoformat(),
            )


@router.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "device": _device_info(),
        "active": rerank_service.active_version(),
        "dataset_built": (DATA_DIR / "train.jsonl").exists(),
        "gate": _dataset_gate(),
        "candidates": _candidates_path() is not None,
        "jobs": len(_jobs),
    }


@router.post("/train", dependencies=[Depends(require_token)])
async def start_train(req: TrainRequest) -> dict:
    """起一个后台训练任务。同一时间只允许一个：两个训练抢 CPU 会都变慢，
    且显存不够时第二个会 OOM，失败原因还很难看懂。"""
    gate = _dataset_gate()
    if not gate["ok"]:
        raise HTTPException(status_code=409, detail="；".join(gate["problems"]))

    with _jobs_lock:
        running = [jid for jid, job in _jobs.items() if job["status"] == "running"]
    if running:
        raise HTTPException(
            status_code=409, detail=f"已有训练在跑：{running[0]}"
        )

    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "status": "queued",
            "request": req.model_dump(),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    _append_log(job_id, f"任务 {job_id} 已创建")

    thread = threading.Thread(target=_run_job, args=(job_id, req), daemon=True)
    thread.start()
    return {"id": job_id, "status": "queued"}


@router.get("/train/{job_id}")
async def get_train(job_id: str, tail: int = 40) -> dict:
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{job_id}")

    log_path = _log_path(job_id)
    lines: list[str] = []
    if log_path.exists():
        lines = log_path.read_text(encoding="utf-8").splitlines()[-max(1, tail) :]
    return {**job, "log_tail": lines}


def _eval_path(version: str) -> Path:
    return MODELS_DIR / version / "eval.json"


def _read_eval(version: str) -> dict | None:
    path = _eval_path(version)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


@router.get("/models")
async def list_models() -> dict:
    from app.finetune.gate import evaluate_gate

    versions = []
    if MODELS_DIR.exists():
        for path in sorted(MODELS_DIR.iterdir()):
            if not path.is_dir() or not (path / "model.onnx").exists():
                continue
            meta_path = path / "meta.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
            evaluation = _read_eval(path.name)
            versions.append(
                {
                    "version": path.name,
                    "best": meta.get("best"),
                    "test": meta.get("test"),
                    "created_at": meta.get("created_at"),
                    "eval": evaluation,
                    # 门禁结果一并返回：前端要靠它把激活按钮置灰，
                    # 点下去再被 409 拦住是更差的体验
                    "gate": evaluate_gate(evaluation) if evaluation else None,
                }
            )
    return {"active": rerank_service.active_version(), "models": versions}


@router.post("/models/{version}/evaluate", dependencies=[Depends(require_token)])
async def evaluate_model(version: str, req: EvalRequest) -> dict:
    """跑离线两把尺 + 阈值标定，把结果写进版本目录的 eval.json。

    同步跑而不是排队：一次评估在 CPU 上约一两分钟，比训练短一个量级，
    为它单开一套任务队列只会多一处状态要对齐。
    """
    from app.finetune.gate import evaluate_gate
    from scripts.finetune_eval import run as run_eval

    if not (MODELS_DIR / version / "model.onnx").exists():
        raise HTTPException(status_code=404, detail=f"模型版本不存在：{version}")

    try:
        result = await asyncio.to_thread(
            run_eval,
            version=version,
            data_dir=DATA_DIR,
            models_dir=MODELS_DIR,
            e2e=req.e2e,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"eval": result, "gate": evaluate_gate(result)}


@router.post("/models/{version}/activate", dependencies=[Depends(require_token)])
async def activate(version: str) -> dict:
    """切活跃版本。三把尺没过就不切。

    门禁放在这里而不是只放页面上：页面可以被绕过（curl、脚本、别人写的
    小工具），而发布是一次性动作，错的模型上线后要靠回滚补救。
    """
    from app.finetune.gate import evaluate_gate

    if not (MODELS_DIR / version / "model.onnx").exists():
        raise HTTPException(status_code=404, detail=f"模型版本不存在：{version}")

    # 先判门禁再写 active 文件：写文件是唯一的副作用，顺序反了会出现
    # 「请求被 409 拒绝但模型已经切过去了」——页面显示旧版本，线上跑新版本
    evaluation = _read_eval(version)
    if evaluation is None:
        raise HTTPException(
            status_code=409,
            detail=f"{version} 还没评估，先跑一次评估再激活",
        )
    gate = evaluate_gate(evaluation)
    if not gate["ok"]:
        raise HTTPException(
            status_code=409, detail="门禁未通过：" + "；".join(gate["failed"])
        )

    rerank_service.set_active(version)
    rerank_service.clear_cache()
    return {"active": version, "gate": gate}


app = FastAPI(title="ShopPilot Trainer")
app.include_router(router)
app.include_router(rerank_service.router)