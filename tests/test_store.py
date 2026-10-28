import pytest

from app import store


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    store.init_db()


def test_create_and_exists():
    conv_id = store.create_conversation()
    assert conv_id
    assert store.conversation_exists(conv_id)
    assert not store.conversation_exists("nonexistent")


def test_append_and_load():
    conv_id = store.create_conversation()
    store.append_message(conv_id, "user", "你好")
    store.append_message(conv_id, "assistant", "您好")

    history = store.load_history(conv_id)
    assert history == [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "您好"},
    ]


def test_load_history_limit_keeps_latest_in_order():
    conv_id = store.create_conversation()
    for i in range(10):
        store.append_message(conv_id, "user", f"m{i}")

    history = store.load_history(conv_id, limit=3)
    assert [m["content"] for m in history] == ["m7", "m8", "m9"]


def test_history_isolated_between_conversations():
    a = store.create_conversation()
    b = store.create_conversation()
    store.append_message(a, "user", "A")
    store.append_message(b, "user", "B")

    assert [m["content"] for m in store.load_history(a)] == ["A"]
    assert [m["content"] for m in store.load_history(b)] == ["B"]


def test_empty_history():
    conv_id = store.create_conversation()
    assert store.load_history(conv_id) == []
