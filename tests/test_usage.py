"""r20 Task 4：用量计量与归因。

关键不确定性是「回调能不能传进节点内的 LLM 调用」，所以用真
BaseChatModel 替身（GenericFakeChatModel）跑真图，验证 langgraph_node 归因。
"""

import uuid

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver

from app.chat.graph import build_graph
from app.core import store
from app.observability import usage as obs
from app.repo import usage as usage_repo


def _fake_llm():
    """真 Runnable，才有 on_llm_end 回调；带 usage_metadata。"""
    msgs = [
        AIMessage(
            content="退回运费由买家承担 [P-RET-001-3]",
            usage_metadata={
                "input_tokens": 100,
                "output_tokens": 20,
                "total_tokens": 120,
            },
        )
        for _ in range(20)
    ]
    return GenericFakeChatModel(messages=iter(msgs))


def test_cost_micros_formula(monkeypatch):
    """价目是元/百万 token，tokens × 价目 直接等于微元。"""
    monkeypatch.setenv("LLM_PRICE_INPUT_PER_MTOK", "2")
    monkeypatch.setenv("LLM_PRICE_OUTPUT_PER_MTOK", "4")
    assert obs.cost_micros(1_000_000, 0) == 2_000_000
    assert obs.cost_micros(0, 1_000_000) == 4_000_000
    assert obs.cost_micros(100, 20) == 280


@pytest.mark.anyio
async def test_usage_attributed_to_graph_node(monkeypatch):
    from app.retrieval import vector_store

    monkeypatch.setattr(
        vector_store,
        "search",
        lambda query, visitor_id, top_k=3: [
            {"source_id": "P-RET-001-3", "score": 0.9, "text": "退回运费由买家承担"}
        ],
    )
    visitor = "usage-" + uuid.uuid4().hex[:8]
    conv = store.create_conversation(visitor)
    handler = obs.build_usage_handler(conversation_id=conv, visitor_id=visitor)

    graph = build_graph(
        checkpointer=MemorySaver(), llm=_fake_llm(), llm_quiet=_fake_llm()
    )
    await graph.ainvoke(
        {"message": "退货运费谁出", "history": [], "visitor_id": visitor},
        config={"configurable": {"thread_id": conv}, "callbacks": [handler]},
    )

    rows = usage_repo.by_conversation(conv)
    assert rows, "必须落至少一行用量"
    assert any(r["node"] == "knowledge" for r in rows)
    assert all(r["source"] == "chat" for r in rows)
    assert rows[0]["input_tokens"] == 100


@pytest.mark.anyio
async def test_missing_usage_does_not_write(monkeypatch):
    """模型不报用量时跳过落库，不能写 0 行污染统计。"""
    visitor = "usage-" + uuid.uuid4().hex[:8]
    conv = store.create_conversation(visitor)
    handler = obs.build_usage_handler(conversation_id=conv, visitor_id=visitor)
    graph = build_graph(checkpointer=MemorySaver(), llm=None, llm_quiet=None)
    await graph.ainvoke(
        {"message": "你好", "history": [], "visitor_id": visitor},
        config={"configurable": {"thread_id": conv}, "callbacks": [handler]},
    )
    assert usage_repo.by_conversation(conv) == []