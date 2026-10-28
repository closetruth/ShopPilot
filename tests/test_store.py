import pytest

from app import store

VISITOR_A = "visitor-a"
VISITOR_B = "visitor-b"


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    store.init_db()


def test_create_and_ownership():
    conv_id = store.create_conversation(VISITOR_A)
    assert conv_id
    assert store.conversation_owned_by(conv_id, VISITOR_A)
    assert not store.conversation_owned_by(conv_id, VISITOR_B)
    assert not store.conversation_owned_by("nonexistent", VISITOR_A)


def test_append_and_load():
    conv_id = store.create_conversation(VISITOR_A)
    store.append_message(conv_id, "user", "你好")
    store.append_message(conv_id, "assistant", "您好")

    assert store.load_history(conv_id) == [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "您好"},
    ]


def test_load_history_limit_keeps_latest_in_order():
    conv_id = store.create_conversation(VISITOR_A)
    for i in range(10):
        store.append_message(conv_id, "user", f"m{i}")

    history = store.load_history(conv_id, limit=3)
    assert [m["content"] for m in history] == ["m7", "m8", "m9"]


def test_history_isolated_between_conversations():
    a = store.create_conversation(VISITOR_A)
    b = store.create_conversation(VISITOR_A)
    store.append_message(a, "user", "A")
    store.append_message(b, "user", "B")

    assert [m["content"] for m in store.load_history(a)] == ["A"]
    assert [m["content"] for m in store.load_history(b)] == ["B"]


def test_empty_history():
    conv_id = store.create_conversation(VISITOR_A)
    assert store.load_history(conv_id) == []


def test_list_conversations_filtered_by_visitor():
    a = store.create_conversation(VISITOR_A)
    b = store.create_conversation(VISITOR_B)
    store.append_message(a, "user", "A 的问题")
    store.append_message(b, "user", "B 的问题")

    a_list = store.list_conversations(VISITOR_A)
    b_list = store.list_conversations(VISITOR_B)

    assert [c["id"] for c in a_list] == [a]
    assert [c["id"] for c in b_list] == [b]


def test_list_conversations_ordered_by_recent_activity():
    first = store.create_conversation(VISITOR_A)
    second = store.create_conversation(VISITOR_A)
    store.append_message(first, "user", "先说的")
    store.append_message(second, "user", "后说的")
    store.append_message(first, "assistant", "回第一条")

    ids = [c["id"] for c in store.list_conversations(VISITOR_A)]
    assert ids == [first, second]


def test_title_uses_first_user_message():
    conv_id = store.create_conversation(VISITOR_A)
    store.append_message(conv_id, "user", "我的包裹什么时候到")
    store.append_message(conv_id, "user", "另一个问题")

    assert store.list_conversations(VISITOR_A)[0]["title"] == "我的包裹什么时候到"


def test_title_truncated_at_twenty_chars():
    conv_id = store.create_conversation(VISITOR_A)
    store.append_message(conv_id, "user", "一二三四五六七八九十一二三四五六七八九十多出来的")

    title = store.list_conversations(VISITOR_A)[0]["title"]
    assert title == "一二三四五六七八九十一二三四五六七八九十…"


def test_empty_conversation_title():
    store.create_conversation(VISITOR_A)
    conv = store.list_conversations(VISITOR_A)[0]
    assert conv["title"] == "新对话"
    assert conv["has_messages"] is False
