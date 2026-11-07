"""远端训练服务的 HTTP 客户端与状态同步。

远端不可达时抛 `TrainerUnavailable`，由接口层翻译成 503：训练服务没起来
和代码有 bug 是两件事，混成 500 会让人去翻错日志。
"""

from __future__ import annotations

import logging
import os

import httpx

from app.repo import finetune as finetune_repo

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://192.168.6.23:8010"
TIMEOUT = 15.0
# 评估要在一整池条款上逐条打分，CPU 上一两分钟很正常；沿用 15 秒会把
# 「跑得慢」误报成「服务不可达」
EVAL_TIMEOUT = 600.0

REMOTE_STATUS_MAP = {
    "queued": "queued",
    "running": "running",
    "succeeded": "done",
    "failed": "failed",
}


class TrainerError(Exception):
    """训练服务返回了非预期结果。"""


class TrainerUnavailable(TrainerError):
    """连不上训练服务：端口未开、容器没起、或需要 SSH 隧道。"""


def base_url() -> str:
    return (os.getenv("TRAINER_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def _headers() -> dict:
    token = (os.getenv("TRAINER_TOKEN") or "").strip()
    return {"X-Trainer-Token": token} if token else {}


def _detail(resp: httpx.Response) -> str:
    """优先取 FastAPI 的 `detail`：它带着可执行的补救说明。

    直接把整个 JSON body 塞进异常里，页面上的报错会变成
    `{"detail":"训练集全是正例..."}` 这种带引号包裹的字符串。
    """
    try:
        payload = resp.json()
    except ValueError:
        return resp.text[:200]
    if isinstance(payload, dict) and payload.get("detail"):
        return str(payload["detail"])
    return resp.text[:200]


def _request(method: str, path: str, *, timeout: float = TIMEOUT, **kwargs) -> dict:
    url = base_url() + path
    try:
        resp = httpx.request(
            method, url, headers=_headers(), timeout=timeout, **kwargs
        )
    except httpx.HTTPError as exc:
        raise TrainerUnavailable(f"{method} {url} 失败：{exc}") from exc

    if resp.status_code >= 500:
        raise TrainerUnavailable(f"{method} {url} 返回 {resp.status_code}")
    if resp.status_code >= 400:
        raise TrainerError(_detail(resp))
    return resp.json()


def health() -> dict:
    return _request("GET", "/health")


def build_dataset_remote(seed: int = 20261104) -> dict:
    """让容器从远端 MySQL 现取标注重建 train/dev/test。

    本地不重复实现一遍构建逻辑：标注、FAQ、轨迹都在远端库里，本地构建
    只能先拉数据再传回去，多一次往返，还多一个「忘了同步」导致训练跑在
    旧标签上的失败模式。
    """
    return _request("POST", "/datasets/build", json={"seed": seed})


def dataset_info() -> dict:
    return _request("GET", "/datasets")


def submit(params: dict) -> dict:
    """提交训练任务。远端同一时间只接受一个任务，冲突返回 409。"""
    return _request("POST", "/train", json=params)


def remote_job(job_id: str, tail: int = 40) -> dict:
    return _request("GET", f"/train/{job_id}", params={"tail": tail})


def list_remote_models() -> dict:
    return _request("GET", "/models")


def activate_remote(version: str) -> dict:
    return _request("POST", f"/models/{version}/activate")


def evaluate_remote(version: str, e2e: dict | None = None) -> dict:
    """让远端跑离线两把尺与阈值标定。

    端到端那把尺只能在本地算（要整张对话图与 Milvus），跑完把数字作为
    `e2e` 带进来；不带就是「没跑」，门禁会按未达标处理。
    """
    return _request(
        "POST", f"/models/{version}/evaluate", timeout=EVAL_TIMEOUT, json={"e2e": e2e}
    )


def sync_job(job_id: str, tail: int = 40) -> dict:
    """把远端任务状态同步回本地库。

    成功时顺带登记模型版本：远端已经产出 onnx，本地表里没有记录的话，
    发布页看不到这个版本，也就无从激活。
    """
    remote = remote_job(job_id, tail=tail)
    status = REMOTE_STATUS_MAP.get(remote.get("status", ""), "running")
    metrics = remote.get("result") or {}

    local = finetune_repo.get_job(job_id)
    if local is None:
        return remote

    if status in ("done", "failed"):
        if local["status"] in ("done", "failed"):
            return remote
        finetune_repo.update_job(
            job_id,
            status=status,
            metrics=metrics or None,
            error=(remote.get("error") or None),
        )
        if status == "done" and metrics.get("version"):
            _register_model(metrics)
        return remote

    if local["status"] != status:
        finetune_repo.update_job(job_id, status=status)
    return remote


def _register_model(result: dict) -> None:
    """登记模型版本。已存在就跳过，重复轮询不该产生重复行。"""
    version = result.get("version")
    if not version:
        return
    existing = {row["version"] for row in finetune_repo.list_models(limit=200)}
    if version in existing:
        return
    finetune_repo.add_model(
        version,
        result.get("base_model") or os.getenv("BASE_MODEL", "hfl/chinese-roberta-wwm-ext"),
        result.get("path") or "",
        result.get("onnx") or "",
        metrics={
            "best": result.get("best"),
            "test": result.get("test"),
            "rows": result.get("rows"),
        },
    )


def log_tail(job_id: str, tail: int = 40) -> list[str]:
    """取远端日志尾部。取不到就返回空列表——日志是排障辅助，不该阻断轮询。"""
    try:
        return remote_job(job_id, tail=tail).get("log_tail") or []
    except TrainerError as exc:
        logger.warning("取训练日志失败：%s", exc)
        return []