import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
import app.db.models  # noqa: F401
from app.db.repository import create_session, list_messages
from app.services import chat_service


class FakeChunk:
    def __init__(self, content):
        self.content = content


class FakeModel:
    async def astream(self, messages):
        for token in ["你", "好"]:
            yield FakeChunk(token)


class FailingModel:
    async def astream(self, messages):
        yield FakeChunk("半")
        raise RuntimeError("upstream failed")


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(chat_service, "get_chat_model", lambda: FakeModel())
    database = Session()
    yield database
    database.close()


@pytest.mark.asyncio
async def test_stream_chat_persists_roundtrip(db):
    events = []

    async for event in chat_service.stream_chat(db, None, "你好"):
        events.append(event)

    assert events[0]["type"] == "session"
    assert [event["content"] for event in events if event["type"] == "token"] == [
        "你",
        "好",
    ]
    assert events[-1] == {"type": "done"}
    messages = list_messages(db, events[0]["session_id"])
    assert [(message.role, message.content) for message in messages] == [
        ("user", "你好"),
        ("assistant", "你好"),
    ]


@pytest.mark.asyncio
async def test_stream_chat_rejects_missing_session(db):
    with pytest.raises(chat_service.SessionNotFound):
        async for _ in chat_service.stream_chat(db, "missing", "你好"):
            pass


@pytest.mark.asyncio
async def test_stream_chat_does_not_persist_partial_assistant_on_error(
    db, monkeypatch
):
    session = create_session(db)
    monkeypatch.setattr(chat_service, "get_chat_model", lambda: FailingModel())

    events = [
        event
        async for event in chat_service.stream_chat(db, session.id, "帮帮我")
    ]

    assert events == [
        {"type": "session", "session_id": session.id},
        {"type": "token", "content": "半"},
        {"type": "error", "message": "upstream failed"},
    ]
    messages = list_messages(db, session.id)
    assert [(message.role, message.content) for message in messages] == [
        ("user", "帮帮我")
    ]
