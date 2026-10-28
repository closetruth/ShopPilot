"""持久化访问的统一入口。

实现按领域拆在 `app.repo` 下：

- `chats`      会话与消息
- `commerce`   商品、订单、工单、工单草稿
- `knowledge`  FAQ 政策知识

这里只做转发，让调用方不必感知内部拆分。
"""

from __future__ import annotations

from app.db import conn_params as _conn_params
from app.db import init_db, now
from app.repo import chats, commerce, knowledge

__all__ = [
    "init_db",
    "now",
    # 会话
    "create_conversation",
    "conversation_owned_by",
    "load_history",
    "list_conversations",
    "append_message",
    # 商品
    "search_products",
    # 订单
    "visitor_has_order",
    "list_orders",
    "get_order",
    "product_name_for_order",
    # 工单
    "find_open_work_order",
    "create_work_order",
    "list_work_orders",
    "get_work_order",
    "find_work_order",
    # 草稿
    "create_work_order_draft",
    "get_work_order_draft",
    "delete_work_order_draft",
    "purge_expired_drafts",
    # FAQ
    "upsert_faq_document",
    "all_faq_for_index",
    "faq_by_id",
    "list_faq",
]


# ---------- 会话 ----------

create_conversation = chats.create_conversation
conversation_owned_by = chats.conversation_owned_by
load_history = chats.load_history
list_conversations = chats.list_conversations
append_message = chats.append_message

# ---------- 商品 ----------

search_products = commerce.search_products

# ---------- 订单 ----------

visitor_has_order = commerce.visitor_has_order
list_orders = commerce.list_orders
get_order = commerce.get_order
product_name_for_order = commerce.product_name_for_order

# ---------- 工单 ----------

find_open_work_order = commerce.find_open_work_order
create_work_order = commerce.create_work_order
list_work_orders = commerce.list_work_orders
get_work_order = commerce.get_work_order
find_work_order = commerce.find_work_order

# ---------- 工单草稿 ----------

create_work_order_draft = commerce.create_draft
get_work_order_draft = commerce.get_draft
delete_work_order_draft = commerce.delete_draft
purge_expired_drafts = commerce.purge_expired_drafts

# ---------- FAQ ----------

upsert_faq_document = knowledge.upsert_document
create_faq_document = knowledge.create_document
update_faq_document = knowledge.update_document
set_faq_enabled = knowledge.set_enabled
delete_faq_document = knowledge.delete_document
all_faq_for_index = knowledge.all_for_index
faq_by_id = knowledge.by_id
list_faq = knowledge.list_by_category
faq_categories = knowledge.categories
delete_stale_faq = knowledge.delete_missing
