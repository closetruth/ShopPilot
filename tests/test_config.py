from app.config import get_settings

def test_settings_reads_env(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("OPENAI_API_BASE", "http://localhost:11434/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("MODEL_NAME", "llama3")
    monkeypatch.setenv("MAX_CONTEXT_TOKENS", "2048")
    monkeypatch.setenv("MAX_HISTORY_TURNS", "6")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./test.db")
    s = get_settings()
    assert s.openai_api_base == "http://localhost:11434/v1"
    assert s.openai_api_key == "sk-test"
    assert s.model_name == "llama3"
    assert s.max_context_tokens == 2048
    assert s.max_history_turns == 6
    assert s.database_url == "sqlite:///./test.db"
