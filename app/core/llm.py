"""模型接入：OpenAI 兼容接口，换供应商只改环境变量。"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

load_dotenv()


class ConfigError(RuntimeError):
    pass


def get_llm() -> ChatOpenAI:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    base_url = os.getenv("OPENAI_BASE_URL", "").strip()
    model = os.getenv("CHAT_MODEL", "").strip()

    missing = [
        name
        for name, value in (
            ("OPENAI_API_KEY", api_key),
            ("OPENAI_BASE_URL", base_url),
            ("CHAT_MODEL", model),
        )
        if not value
    ]
    if missing:
        raise ConfigError(
            "缺少环境变量：" + ", ".join(missing) + "。请参考 .env.example 配置 .env"
        )

    return _build(streaming=True)


def get_llm_quiet() -> ChatOpenAI:
    """非流式 LLM：内部调用（消解/意图分类/知识生成）专用。

    实例级 streaming=True 的模型连 ainvoke 都走流式聚合，中间内容会在
    astream_events 里以 on_chat_model_stream 冒泡给前端——消解改写文本、
    意图分类 JSON 都会泄漏并污染落库消息。这些调用的结果不直接给顾客，
    必须用非流式实例。
    """
    return _build(streaming=False)


def _build(*, streaming: bool) -> ChatOpenAI:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    base_url = os.getenv("OPENAI_BASE_URL", "").strip()
    model = os.getenv("CHAT_MODEL", "").strip()

    return ChatOpenAI(
        api_key=api_key,
        base_url=base_url,
        model=model,
        streaming=streaming,
        temperature=0.3,
        # 自定义 base_url 时 langchain 默认关 stream_usage，流式响应里
        # 没有 usage_metadata，agent 的 token 上限会形同虚设。显式开启；
        # 供应商不支持时该参数被忽略，上限退化为步数兜底
        stream_usage=True,
    )
