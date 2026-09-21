import json

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base, get_db
import app.db.models  # noqa: F401
from app.main import app
from app.services import chat_service


class FakeChunk:
    def __init__(self, content: str) -> None:
        self.content = content


class FakeModel:
    async def astream(self, messages):
        for token in ["你", "好"]:
            yield FakeChunk(token)


def test_chat_mode_streams_session_tokens_and_done(monkeypatch) -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)

    def override_get_db():
        database = TestSession()
        try:
            yield database
        finally:
            database.close()

    monkeypatch.setattr(
        chat_service, "get_chat_model", lambda: FakeModel()
    )
    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/chat", json={"message": "你好"}
            )
    finally:
        app.dependency_overrides.clear()
        engine.dispose()

    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    events = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert events[0]["type"] == "session"
    assert [event["content"] for event in events if event["type"] == "token"] == [
        "你",
        "好",
    ]
    assert events[-1] == {"type": "done"}
