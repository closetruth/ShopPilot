from langchain_openai import ChatOpenAI

from app.config import get_settings


def get_chat_model() -> ChatOpenAI:
    settings = get_settings()
    return ChatOpenAI(
        model=settings.model_name,
        api_key=settings.openai_api_key,
        base_url=settings.openai_api_base,
        temperature=0.3,
    )
