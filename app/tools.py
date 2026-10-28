"""LangChain 工具：查商品、查工单。

参数非法时不抛异常，而是返回结构化错误供模型修正后重试。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated

from langchain_core.tools import tool
from langgraph.prebuilt import ToolRuntime
from pydantic import ValidationError

from app import store
from app.schemas import ProductQuery, WorkOrderQuery


@dataclass
class AgentContext:
    visitor_id: str


def _validation_error(exc: ValidationError) -> dict:
    detail = "; ".join(
        f"{'.'.join(str(p) for p in err['loc']) or '参数'}: {err['msg']}"
        for err in exc.errors()
    )
    return {"error": f"参数不合法（{detail}）。请修正后重试。"}


@tool
def lookup_product(
    runtime: ToolRuntime[AgentContext],
    keyword: Annotated[str | None, "商品名称、类别或颜色的关键词，如「杯子」「蓝色」"] = None,
    product_id: Annotated[str | None, "已知的商品 ID，顾客未提供时留空"] = None,
) -> dict:
    """查询店铺在售商品的价格、颜色、库存与简介。顾客询问是否有某件商品、
    多少钱、什么颜色、有没有货时使用。"""
    try:
        query = ProductQuery(keyword=keyword, product_id=product_id)
    except ValidationError as exc:
        return _validation_error(exc)

    try:
        items = store.search_products(
            keyword=query.keyword, product_id=query.product_id
        )
    except Exception:  # noqa: BLE001 - 数据库异常作为数据回给模型
        return {"error": "商品查询暂时不可用，请稍后重试"}

    if not items:
        return {"products": [], "note": "没有找到匹配的商品"}
    return {"products": items, "count": len(items)}


@tool
def lookup_work_order(
    runtime: ToolRuntime[AgentContext],
    order_no: Annotated[str, "订单号，通常以 ORDER 开头，8-32 位大写字母或数字"],
) -> dict:
    """按订单号查询顾客自己的工单处理进度。顾客询问自己的订单/工单状态、
    发货进度、售后处理到哪一步时使用。只能查询当前顾客本人的工单。"""
    try:
        query = WorkOrderQuery(order_no=order_no)
    except ValidationError as exc:
        return _validation_error(exc)

    visitor_id = runtime.context.visitor_id
    try:
        order = store.find_work_order(visitor_id, query.order_no)
    except Exception:  # noqa: BLE001
        return {"error": "工单查询暂时不可用，请稍后重试"}

    if order is None:
        # 不区分「订单不存在」与「不属于当前顾客」，避免泄露订单号是否存在
        return {
            "found": False,
            "note": "未找到该订单的工单。请确认订单号是否正确，或告知顾客需要人工核实。",
        }
    return {"found": True, "work_order": order}


TOOLS = [lookup_product, lookup_work_order]
