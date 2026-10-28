import uuid

import pytest

from app import store


@pytest.fixture
def visitor():
    """每个测试用独立 visitor_id，避免共享 MySQL 库时相互污染。"""
    return "test-" + uuid.uuid4().hex[:12]


def test_create_and_ownership(visitor):
    conv_id = store.create_conversation(visitor)
    assert conv_id
    assert store.conversation_owned_by(conv_id, visitor)
    assert not store.conversation_owned_by(conv_id, "someone-else")
    assert not store.conversation_owned_by("nonexistent", visitor)


def test_append_and_load(visitor):
    conv_id = store.create_conversation(visitor)
    store.append_message(conv_id, "user", "你好")
    store.append_message(conv_id, "assistant", "您好")

    assert store.load_history(conv_id) == [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "您好"},
    ]


def test_load_history_limit_keeps_latest_in_order(visitor):
    conv_id = store.create_conversation(visitor)
    for i in range(10):
        store.append_message(conv_id, "user", f"m{i}")

    history = store.load_history(conv_id, limit=3)
    assert [m["content"] for m in history] == ["m7", "m8", "m9"]


def test_history_isolated_between_conversations(visitor):
    a = store.create_conversation(visitor)
    b = store.create_conversation(visitor)
    store.append_message(a, "user", "A")
    store.append_message(b, "user", "B")

    assert [m["content"] for m in store.load_history(a)] == ["A"]
    assert [m["content"] for m in store.load_history(b)] == ["B"]


def test_empty_history(visitor):
    conv_id = store.create_conversation(visitor)
    assert store.load_history(conv_id) == []


def test_list_conversations_filtered_by_visitor(visitor):
    other = visitor + "-other"
    a = store.create_conversation(visitor)
    b = store.create_conversation(other)
    store.append_message(a, "user", "A 的问题")
    store.append_message(b, "user", "B 的问题")

    assert [c["id"] for c in store.list_conversations(visitor)] == [a]
    assert [c["id"] for c in store.list_conversations(other)] == [b]


def test_list_conversations_ordered_by_recent_activity(visitor):
    first = store.create_conversation(visitor)
    second = store.create_conversation(visitor)
    store.append_message(first, "user", "先说的")
    store.append_message(second, "user", "后说的")
    store.append_message(first, "assistant", "回第一条")

    ids = [c["id"] for c in store.list_conversations(visitor)]
    assert ids == [first, second]


def test_title_uses_first_user_message(visitor):
    conv_id = store.create_conversation(visitor)
    store.append_message(conv_id, "user", "我的包裹什么时候到")
    store.append_message(conv_id, "user", "另一个问题")

    assert store.list_conversations(visitor)[0]["title"] == "我的包裹什么时候到"


def test_title_truncated_at_twenty_chars(visitor):
    conv_id = store.create_conversation(visitor)
    store.append_message(conv_id, "user", "一二三四五六七八九十一二三四五六七八九十多出来的")

    title = store.list_conversations(visitor)[0]["title"]
    assert title == "一二三四五六七八九十一二三四五六七八九十…"


def test_empty_conversation_title(visitor):
    store.create_conversation(visitor)
    conv = store.list_conversations(visitor)[0]
    assert conv["title"] == "新对话"
    assert conv["has_messages"] is False


def test_search_products_by_color(visitor):
    results = store.search_products(keyword="蓝色")
    assert results
    assert all(r["color"] == "蓝色" for r in results)


def test_search_products_multi_term():
    results = store.search_products(keyword="蓝色 杯")
    assert results
    names = [r["name"] for r in results]
    assert any("蓝" in n or r["color"] == "蓝色" for n, r in zip(names, results))


def test_search_products_no_match():
    assert store.search_products(keyword="完全不存在的商品名称") == []


def test_search_products_by_id():
    results = store.search_products(product_id="p-mug-blue")
    assert len(results) == 1
    assert results[0]["name"] == "青花瓷马克杯"


def test_search_products_hides_internal_fields():
    results = store.search_products(product_id="p-mug-blue")
    assert set(results[0]) == {
        "id", "name", "category", "color", "price_yuan", "stock", "description",
    }


def test_find_work_order_owned(visitor):
    from scripts.seed import DEMO_VISITOR

    order = store.find_work_order(DEMO_VISITOR, "ORDER20260001")
    assert order is not None
    assert order["status"] == "运输破损"


def test_find_work_order_not_owned_returns_none(visitor):
    from scripts.seed import DEMO_VISITOR

    assert store.find_work_order("someone-else", "ORDER20260001") is None
    assert store.find_work_order(DEMO_VISITOR, "ORDER20260003") is None


def test_create_work_order_and_find_it(visitor):
    work_order_no = store.create_work_order(
        visitor, "ORDER20990001", "运输破损", "收到时杯身有裂纹，外包装压扁"
    )
    assert work_order_no.startswith("WO")

    found = store.find_work_order(visitor, "ORDER20990001")
    assert found is not None
    assert found["status"] == "待处理"
    assert found["issue"] == "收到时杯身有裂纹，外包装压扁"


def test_create_work_order_forces_server_side_fields(visitor):
    import pymysql

    work_order_no = store.create_work_order(
        visitor, "ORDER20990002", "质量问题", "内胆涂层脱落，使用一周后出现"
    )
    conn = pymysql.connect(
        host="127.0.0.1", port=3307, user="shoppilot",
        password="shoppilot", database="shoppilot",
        cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT visitor_id, status, source, priority"
                " FROM work_orders WHERE id = %s",
                (work_order_no,),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    assert row["visitor_id"] == visitor
    assert row["status"] == "待处理"
    assert row["source"] == "ai"
    assert row["priority"] == "普通"


def test_find_open_work_order_detects_duplicate(visitor):
    store.create_work_order(
        visitor, "ORDER20990003", "发货延迟", "下单五天仍未发货，物流无更新"
    )
    existing = store.find_open_work_order(visitor, "ORDER20990003", "发货延迟")
    assert existing is not None
    assert existing["status"] == "待处理"


def test_find_open_work_order_different_type_is_not_duplicate(visitor):
    store.create_work_order(
        visitor, "ORDER20990004", "退换货", "尺寸不合适申请退货处理"
    )
    assert store.find_open_work_order(visitor, "ORDER20990004", "发货延迟") is None


def test_visitor_has_order(visitor):
    store.create_work_order(
        visitor, "ORDER20990005", "其他", "顾客咨询售后流程需要记录跟进"
    )
    assert store.visitor_has_order(visitor, "ORDER20990005")
    assert not store.visitor_has_order(visitor, "ORDER20990006")
    assert not store.visitor_has_order("someone-else", "ORDER20990005")
