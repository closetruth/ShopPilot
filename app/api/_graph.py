"""对话图的进程级单例。

图的 compile 开销虽小但每请求重复编译毫无收益；llm/checkpointer 都是
单例，图结构固定，进程内建一次复用。拆成独立模块避免 api/customer 与
main 之间的循环导入。
"""

from __future__ import annotations

from app.graph import build_graph

_compiled_graph = None


async def get_graph(llm_quiet_factory):
    """返回编译好的图；首次调用时建连并 compile。"""
    global _compiled_graph
    if _compiled_graph is None:
        from app.checkpointer import get_checkpointer
        from app.llm import get_llm

        _compiled_graph = build_graph(
            checkpointer=await get_checkpointer(),
            llm=get_llm(),
            llm_quiet=llm_quiet_factory(),
        )
    return _compiled_graph


def set_graph_for_testing(graph) -> None:
    """测试注入 mock 图用。"""
    global _compiled_graph
    _compiled_graph = graph
