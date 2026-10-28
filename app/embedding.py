"""文本嵌入：BGE-M3，走硅基流动的 OpenAI 兼容接口。

模型在远端，本地只需 HTTP 调用——避免在无 GPU 的机器上下载 2.3GB 权重
并用 CPU 慢速推理。
"""

from __future__ import annotations

import os
from functools import lru_cache

from openai import OpenAI

EMBED_DIM = 1024
BATCH_SIZE = 32


class EmbeddingError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def _client() -> OpenAI:
    api_key = os.getenv("SILICONFLOW_API_KEY", "").strip()
    base_url = os.getenv("EMBED_BASE_URL", "https://api.siliconflow.cn/v1").strip()
    if not api_key:
        raise EmbeddingError("缺少 SILICONFLOW_API_KEY，无法生成向量")
    return OpenAI(api_key=api_key, base_url=base_url)


def _model() -> str:
    return os.getenv("EMBED_MODEL", "BAAI/bge-m3").strip()


def embed_texts(texts: list[str]) -> list[list[float]]:
    """批量嵌入。返回顺序与入参一致。"""
    if not texts:
        return []

    vectors: list[list[float]] = []
    client = _client()
    model = _model()

    for start in range(0, len(texts), BATCH_SIZE):
        batch = texts[start : start + BATCH_SIZE]
        try:
            resp = client.embeddings.create(model=model, input=batch)
        except Exception as exc:  # noqa: BLE001 - 网络与鉴权错误都要上抛给调用方决定
            raise EmbeddingError(f"嵌入服务调用失败：{exc}") from exc

        ordered = sorted(resp.data, key=lambda d: d.index)
        if len(ordered) != len(batch):
            raise EmbeddingError(
                f"嵌入返回数量不符：请求 {len(batch)} 条，返回 {len(ordered)} 条"
            )
        for item in ordered:
            if len(item.embedding) != EMBED_DIM:
                raise EmbeddingError(
                    f"向量维度异常：期望 {EMBED_DIM}，实际 {len(item.embedding)}"
                )
            vectors.append(item.embedding)

    return vectors


def embed_text(text: str) -> list[float]:
    return embed_texts([text])[0]
