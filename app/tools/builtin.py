"""四个内置业务工具，全部经注册中心登记。

实现函数不再自己做参数校验——执行引擎已用 `args_schema` 统一校验，
这里只负责业务逻辑与「查不到/数据库不可用」的语义化返回。
"""

from __future__ import annotations

import logging

from app import store
from app.schemas import NoArgs, ProductQuery, WorkOrderCreate, WorkOrderQuery
from app.tools.registry import registered

logger = logging.getLogger(__name__)


@registered(
    name="lookup_product",
    label="商品",
    args_schema=ProductQuery,
    scope="none",
    risk="read",
    allowed_callers=("agent", "admin"),
)
def lookup_product(runtime, *, keyword=None, product_id=None) -> dict:
    """查询店铺在售商品的价格、颜色、库存与简介。顾客询问是否有某件商品、
    多少钱、什么颜色、有没有货时使用。"""
    try:
        items = store.search_products(keyword=keyword, product_id=product_id)
        if items:
            return {"products": items, "count": len(items)}

        # 关键词没命中时给出全部在售商品，由模型判断哪些相关。
        # 商品规模小，全量返回比猜词更可靠
        everything = store.search_products()
    except Exception:  # noqa: BLE001 - 数据库异常作为数据回给模型
        return {"error": "商品查询暂时不可用，请稍后重试"}

    return {
        "products": everything,
        "count": len(everything),
        "note": f"没有匹配「{keyword}」的商品，以上是全部在售商品。"
        "若顾客问的是使用规范、保养、存放等政策类问题而非买货，如实告知"
        "政策问题请咨询知识类问题入口。",
    }


@registered(
    name="list_my_orders",
    label="订单",
    args_schema=NoArgs,
    scope="visitor",
    risk="read",
    allowed_callers=("agent",),
)
def list_my_orders(runtime) -> dict:
    """列出当前顾客名下的全部订单（订单号、商品、下单时间、已有工单）。
    顾客不确定订单号、或在问「我的订单」「我买的东西」时使用，避免让顾客
    凭记忆报订单号。无参数。"""
    try:
        orders = store.list_orders(runtime.context.visitor_id)
    except Exception:  # noqa: BLE001
        return {"error": "订单查询暂时不可用，请稍后重试"}

    if not orders:
        return {"orders": [], "note": "该顾客名下没有订单记录"}
    return {"orders": orders, "count": len(orders)}


@registered(
    name="lookup_work_order",
    label="工单",
    args_schema=WorkOrderQuery,
    scope="visitor",
    risk="read",
    allowed_callers=("agent",),
)
def lookup_work_order(runtime, *, order_no: str) -> dict:
    """按订单号查询顾客自己的工单处理进度。顾客询问自己的订单/工单状态、
    发货进度、售后处理到哪一步时使用。只能查询当前顾客本人的工单。"""
    visitor_id = runtime.context.visitor_id
    try:
        order = store.find_work_order(visitor_id, order_no)
    except Exception:  # noqa: BLE001
        return {"error": "工单查询暂时不可用，请稍后重试"}

    if order is None:
        # 不区分「订单不存在」与「不属于当前顾客」，避免泄露订单号是否存在
        return {
            "found": False,
            "note": "未找到该订单的工单。请确认订单号是否正确，或告知顾客需要人工核实。",
        }
    return {"found": True, "work_order": order}


def _work_order_preview(runtime, *, order_no=None, issue_type, description) -> dict:
    """挂起前的只读检查：订单归属与重复建单。任一不通过就不生成确认卡片。"""
    visitor_id = runtime.context.visitor_id
    try:
        if order_no is not None and not store.visitor_has_order(visitor_id, order_no):
            return {
                "ok": False,
                "created": False,
                "reason": "order_not_found",
                "note": "系统中查不到该订单号属于当前顾客，无法建单。"
                "请顾客核对订单号，或说明需要人工核实。不要改为直接建单。",
            }

        existing = store.find_open_work_order(visitor_id, order_no, issue_type)
        if existing is not None:
            return {
                "ok": False,
                "created": False,
                "reason": "duplicate",
                "existing_work_order_no": existing["id"],
                "existing_status": existing["status"],
                "note": "该顾客已有同类未完结工单，请告知正在处理中，不要重复建单。",
            }
    except Exception:  # noqa: BLE001 - 数据库异常作为数据回给模型
        return {"ok": False, "error": "工单检查暂时不可用，请稍后重试或转人工处理"}

    return {"ok": True}


def _work_order_summary(args: dict) -> dict:
    """确认卡片的全部内容。前端只认 title/fields/note 三样。"""
    return {
        "title": "待确认工单",
        "fields": [
            {"k": "订单号", "v": args.get("order_no") or "不关联订单"},
            {"k": "问题类型", "v": args["issue_type"]},
            {"k": "问题描述", "v": args["description"]},
        ],
        "note": "确认后才会真正提交，可随时取消",
    }


@registered(
    name="create_work_order",
    label="建工单",
    args_schema=WorkOrderCreate,
    scope="visitor",
    risk="write",
    requires_confirmation=True,
    allowed_callers=("agent",),
    preview=_work_order_preview,
    summarize=_work_order_summary,
)
def create_work_order(runtime, *, order_no=None, issue_type, description) -> dict:
    """为顾客提交一张售后工单。本工具不会立刻建单：它会先生成确认卡片，
    顾客点击确认后工单才真正生成。

    因此你不需要征求顾客口头同意，随时可以准备建单；但必须先把问题类型与
    问题描述问清楚，缺信息时先追问。你无法直接建立工单，也不要宣称「已提交」。

    订单号规则：运输破损 / 发货延迟 / 质量问题 / 退换货 必须有订单号，
    可先用 list_my_orders 找到；咨询 / 投诉建议 / 其他 无需订单号。

    同一顾客同一订单同一类型已有未完结工单时不会生成卡片，会返回既有工单。"""
    visitor_id = runtime.context.visitor_id

    try:
        work_order_no = store.create_work_order(
            visitor_id, order_no, issue_type, description, source="ai"
        )
    except Exception:  # noqa: BLE001 - 数据库异常作为数据回给模型
        return {"error": "工单创建失败，请稍后重试或转人工处理"}

    return {
        "created": True,
        "work_order_no": work_order_no,
        "note": f"工单 {work_order_no} 已提交成功，可以告知顾客单号。",
    }