"""本地 token 计数。

tiktoken cl100k_base 对 DeepSeek tokenizer 是近似——中文略多计，
预算因此偏保守（宁可提前压缩，不可撑爆真实窗口）。词表首次加载
要从 openaipublic CDN 拉取并缓存；拉取失败降级 chars/3 估算并记
warning——计数器故障不能打断对话路径。

不用 API 数 token：慢、贵、且在请求路径上。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_encoding = None
_load_attempted = False

_FALLBACK_CHARS_PER_TOKEN = 3  # 中文经验值：cl100k 下约 0.6-0.8 字/token，3 取保守


def _get_encoding():
    """惰性加载词表；失败只警告一次，后续调用直接走降级。"""
    global _encoding, _load_attempted
    if _encoding is None and not _load_attempted:
        _load_attempted = True
        try:
            import tiktoken

            _encoding = tiktoken.get_encoding("cl100k_base")
        except Exception as exc:  # noqa: BLE001 - CDN 不可达等环境故障
            logger.warning("tiktoken 词表加载失败，降级 chars/3 估算：%s", exc)
    return _encoding


def count_tokens(text: str) -> int:
    """文本 token 数。词表不可用时按 chars/3 估算。"""
    if not text:
        return 0
    enc = _get_encoding()
    if enc is None:
        return max(1, len(text) // _FALLBACK_CHARS_PER_TOKEN)
    return len(enc.encode(text))


def count_messages_tokens(messages: list[dict]) -> int:
    """消息列表的 token 数，每条 +4 封装开销（role/content 包裹）。"""
    total = 0
    for m in messages:
        total += count_tokens(m.get("content", "")) + 4
    return total
