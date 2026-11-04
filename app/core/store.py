"""持久化访问的统一入口。

实现按领域拆在 `app.repo` 下：

- `chats`      会话与消息
- `commerce`   商品、订单、工单、工单草稿
- `knowledge`  FAQ 政策知识

这里只做转发，让调用方不必感知内部拆分。
"""

from __future__ import annotations

from app.core.db import conn_params as _conn_params
from app.core.db import init_db, now
from app.repo import (
    audit,
    chats,
    commerce,
    evals,
    feedback,
    finetune,
    insights,
    knowledge,
    pending,
)

__all__ = [
    "init_db",
    "now",
    # 会话
    "create_conversation",
    "conversation_owned_by",
    "load_history",
    "load_history_after",
    "list_conversations",
    "append_message",
    "get_summary",
    "upsert_summary",
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
    # 轨迹
    "record_trace",
    "list_traces",
    "trace_stats",
    # 工具审计
    "record_tool_audit",
    "list_tool_audits",
    "mask_audit_args",
    # 待确认动作
    "create_pending_action",
    "get_pending_action",
    "resolve_pending_action",
    "list_pending_actions",
    # 用户反馈
    "feedback_message_of",
    "upsert_feedback",
    "remove_feedback",
    "list_feedback",
    "feedback_by_id",
    "feedback_stats",
    "list_negative_feedback",
    "feedback_reasons",
    "feedback_query_for",
    "low_confidence_by_id",
    # 评估样本
    "create_eval_case",
    "list_eval_cases",
    "get_eval_case",
    "set_eval_case_status",
    # 精排微调
    "normalize_query",
    "query_hash",
    "upsert_finetune_label",
    "get_finetune_label",
    "list_finetune_labels",
    "finetune_label_stats",
    "create_finetune_dataset",
    "get_finetune_dataset",
    "list_finetune_datasets",
    "create_finetune_job",
    "update_finetune_job",
    "get_finetune_job",
    "list_finetune_jobs",
    "running_finetune_job",
    "add_finetune_model",
    "list_finetune_models",
    "activate_finetune_model",
    "active_finetune_model",
]


# ---------- 会话 ----------

create_conversation = chats.create_conversation
conversation_owned_by = chats.conversation_owned_by
load_history = chats.load_history
load_history_after = chats.load_history_after
list_conversations = chats.list_conversations
append_message = chats.append_message
get_summary = chats.get_summary
upsert_summary = chats.upsert_summary

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
create_faq_documents = knowledge.create_documents
update_faq_document = knowledge.update_document
set_faq_enabled = knowledge.set_enabled
delete_faq_document = knowledge.delete_document
all_faq_for_index = knowledge.all_for_index
faq_by_id = knowledge.by_id
list_faq = knowledge.list_by_category
faq_categories = knowledge.categories
delete_stale_faq = knowledge.delete_missing

# ---------- 低置信度 ----------

record_low_confidence = insights.record
list_low_confidence = insights.list_recent
low_confidence_stats = insights.stats

# ---------- agent 轨迹 ----------

record_trace = insights.record_trace
list_traces = insights.list_traces
trace_stats = insights.trace_stats

# ---------- 工具审计 ----------

record_tool_audit = audit.record
list_tool_audits = audit.list_logs
mask_audit_args = audit.mask_args

# ---------- 待确认动作 ----------

create_pending_action = pending.create
get_pending_action = pending.get
resolve_pending_action = pending.resolve
list_pending_actions = pending.list_by_visitor

# ---------- 用户反馈 ----------

feedback_message_of = feedback.message_of
upsert_feedback = feedback.upsert
remove_feedback = feedback.remove
list_feedback = feedback.by_conversation
feedback_by_id = feedback.by_id
feedback_stats = feedback.stats
list_negative_feedback = feedback.list_negative
feedback_reasons = feedback.REASONS
feedback_query_for = feedback.query_for

low_confidence_by_id = insights.low_confidence_by_id

# ---------- 评估样本 ----------

create_eval_case = evals.create_case
list_eval_cases = evals.list_cases
get_eval_case = evals.get_case
set_eval_case_status = evals.set_case_status

# ---------- 精排微调 ----------

normalize_query = finetune.normalize_query
query_hash = finetune.query_hash
upsert_finetune_label = finetune.upsert_label
get_finetune_label = finetune.get_label
list_finetune_labels = finetune.list_labels
finetune_label_stats = finetune.label_stats
create_finetune_dataset = finetune.create_dataset
get_finetune_dataset = finetune.get_dataset
list_finetune_datasets = finetune.list_datasets
create_finetune_job = finetune.create_job
update_finetune_job = finetune.update_job
get_finetune_job = finetune.get_job
list_finetune_jobs = finetune.list_jobs
running_finetune_job = finetune.running_job
add_finetune_model = finetune.add_model
list_finetune_models = finetune.list_models
activate_finetune_model = finetune.activate_model
active_finetune_model = finetune.active_model