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

    return ChatOpenAI(
        api_key=api_key,
        base_url=base_url,
        model=model,
        streaming=True,
        temperature=0.3,
    )
