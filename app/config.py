from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    openai_api_base: str = "https://api.openai.com/v1"
    openai_api_key: str = "changeme"
    model_name: str = "gpt-4o-mini"
    max_context_tokens: int = 4096
    max_history_turns: int = 10
    database_url: str = "sqlite:///./mewhelp.db"


@lru_cache
def get_settings() -> Settings:
    return Settings()
