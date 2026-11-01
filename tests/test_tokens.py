"""r18 T1：本地 token 计数的验证测试。

tiktoken cl100k_base 对 DeepSeek tokenizer 是近似——中文略多计，
预算偏保守（宁可提前压缩，不可撑爆真实窗口），方向安全。
"""

import pytest

from app.tokens import count_messages_tokens, count_tokens


def test_chinese_counts_reasonably():
    """中文约 0.5-1 token/字（cl100k 对中文略多计）。"""
    n = count_tokens("你好，我买的保温杯坏了想退货")
    assert 5 <= n <= 40, f"中文计数异常：{n}"


def test_english_counts_reasonably():
    assert count_tokens("hello world") <= 4


def test_empty_string():
    assert count_tokens("") == 0


def test_messages_add_per_message_overhead():
    """每条消息 +4 封装开销（role/content 包裹），条数越多越明显。"""
    one = [{"role": "user", "content": "你好"}]
    ten = one * 10
    n1 = count_messages_tokens(one)
    n10 = count_messages_tokens(ten)
    assert n10 == n1 * 10


def test_fallback_when_tiktoken_unavailable(monkeypatch):
    """词表加载失败降级 chars/3，对话不能因为计数器挂掉。"""
    import app.tokens as t

    monkeypatch.setattr(t, "_get_encoding", lambda: None)
    assert t.count_tokens("一二三四五六") == 2  # 6 字 / 3
