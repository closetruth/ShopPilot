"""精排推理服务：onnxruntime 加载活跃版本，协议对齐硅基流动 /v1/rerank。

只依赖 onnxruntime，不加载 transformers 的模型类：推理路径越短，冷启动
越快，也不会因为 transformers 版本变动让线上行为和训练时不一致。

tokenizer 用 `tokenizers` 直接读 `tokenizer.json`，同样绕开 transformers。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import numpy as np
import onnxruntime as ort
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from tokenizers import Tokenizer

MODELS_DIR = Path("/app/models")
ACTIVE_FILE = MODELS_DIR / "active"

_router = APIRouter()
# 训练服务要挂同一组路由：推理与训练共用一个进程，前端不必记两个端口
router = _router
_lock = threading.Lock()
_cache: dict[str, dict] = {}


def active_version() -> str | None:
    """活跃版本写在文件里而不是数据库。

    推理容器不该连 MySQL：多一个依赖就多一个线上故障源，训练服务重启
    也不该被数据库可用性拖住。版本切换由训练服务写这个文件。
    """
    if not ACTIVE_FILE.exists():
        return None
    version = ACTIVE_FILE.read_text(encoding="utf-8").strip()
    return version or None


def set_active(version: str) -> bool:
    model_dir = MODELS_DIR / version
    if not (model_dir / "model.onnx").exists():
        return False
    ACTIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
    ACTIVE_FILE.write_text(version, encoding="utf-8")
    return True


def load_version(version: str) -> dict:
    with _lock:
        if version in _cache:
            return _cache[version]

        model_dir = MODELS_DIR / version
        onnx_path = model_dir / "model.onnx"
        tokenizer_path = model_dir / "tokenizer.json"
        if not onnx_path.exists() or not tokenizer_path.exists():
            raise FileNotFoundError(f"模型版本 {version} 缺 model.onnx 或 tokenizer.json")

        meta = {}
        meta_path = model_dir / "meta.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))

        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        session = ort.InferenceSession(
            str(onnx_path), sess_options=options, providers=["CPUExecutionProvider"]
        )
        tokenizer = Tokenizer.from_file(str(tokenizer_path))
        tokenizer.enable_truncation(max_length=int(meta.get("max_length", 256)))
        tokenizer.enable_padding()

        entry = {"session": session, "tokenizer": tokenizer, "meta": meta}
        _cache[version] = entry
        return entry


def clear_cache(version: str | None = None) -> None:
    with _lock:
        if version is None:
            _cache.clear()
        else:
            _cache.pop(version, None)


def score(version: str, query: str, documents: list[str]) -> list[float]:
    entry = load_version(version)
    encoded = entry["tokenizer"].encode_batch(
        [(query, document) for document in documents]
    )
    input_ids = np.array([item.ids for item in encoded], dtype=np.int64)
    attention_mask = np.array([item.attention_mask for item in encoded], dtype=np.int64)

    session = entry["session"]
    logits = session.run(
        ["logits"], {"input_ids": input_ids, "attention_mask": attention_mask}
    )[0]
    flat = np.asarray(logits).reshape(len(documents), -1)[:, 0]
    return (1.0 / (1.0 + np.exp(-flat)).astype(np.float64)).tolist()


class RerankDocument(BaseModel):
    text: str


class RerankRequest(BaseModel):
    model: str
    query: str
    documents: list[RerankDocument]
    top_n: int | None = None


@_router.post("/v1/rerank")
async def rerank(req: RerankRequest) -> dict:
    """协议与硅基流动一致，`model` 传版本号即可。

    `model` 不存在返回 404 而不是空结果：客户端把空结果当成「没有相关
    条款」会静默降级，问题被掩盖。
    """
    if not req.documents:
        return {"results": [], "model": req.model}

    if not (MODELS_DIR / req.model / "model.onnx").exists():
        raise HTTPException(status_code=404, detail=f"模型版本不存在：{req.model}")

    scores = score(req.model, req.query, [doc.text for doc in req.documents])
    results = [
        {"index": index, "relevance_score": round(float(value), 6)}
        for index, value in enumerate(scores)
    ]
    results.sort(key=lambda item: item["relevance_score"], reverse=True)
    if req.top_n:
        results = results[: req.top_n]
    return {"results": results, "model": req.model, "usage": {"total_tokens": 0}}


@_router.get("/v1/models")
async def list_served_models() -> dict:
    versions = sorted(
        path.name for path in MODELS_DIR.iterdir()
        if path.is_dir() and (path / "model.onnx").exists()
    ) if MODELS_DIR.exists() else []
    return {"active": active_version(), "models": versions}


def create_app():
    from fastapi import FastAPI

    app = FastAPI(title="ShopPilot Reranker")
    app.include_router(_router)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "active": active_version()}

    return app


app = create_app()