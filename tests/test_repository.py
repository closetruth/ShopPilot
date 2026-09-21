from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.db.base import Base
from app.db import repository as repo
import app.db.models  # noqa: F401


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def test_create_session_and_messages():
    db = _session()
    s = repo.create_session(db)
    assert s.id
    repo.add_message(db, s.id, "user", "hi")
    repo.add_message(db, s.id, "assistant", "hello")
    msgs = repo.list_messages(db, s.id)
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert msgs[0].seq == 1 and msgs[1].seq == 2


def test_get_session_missing():
    db = _session()
    assert repo.get_session(db, "no-such") is None
