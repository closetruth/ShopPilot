"""工具入参的校验模型。"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ISSUE_TYPES = (
    "运输破损",
    "发货延迟",
    "质量问题",
    "退换货",
    "咨询",
    "投诉建议",
    "其他",
)
IssueType = Literal[
    "运输破损",
    "发货延迟",
    "质量问题",
    "退换货",
    "咨询",
    "投诉建议",
    "其他",
]

# 不依赖订单的问题类型：投诉、咨询、建议本身就不对应某一笔订单
ORDERLESS_ISSUE_TYPES = ("咨询", "投诉建议", "其他")


class ProductQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keyword: Annotated[
        str | None,
        Field(description="商品名称、类别或颜色的关键词", max_length=32),
    ] = None
    product_id: Annotated[
        str | None,
        Field(description="商品 ID", max_length=32),
    ] = None

    @model_validator(mode="after")
    def at_least_one(self) -> "ProductQuery":
        if not (self.keyword or self.product_id):
            raise ValueError("keyword 与 product_id 至少提供一个")
        return self


ORDER_NO_RE = re.compile(r"^[A-Z0-9]{8,32}$")


class WorkOrderQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_no: Annotated[
        str,
        Field(description="订单号，8-32 位大写字母或数字"),
    ]

    @model_validator(mode="after")
    def valid_order_no(self) -> "WorkOrderQuery":
        if not ORDER_NO_RE.match(self.order_no):
            raise ValueError(
                "order_no 需为 8-32 位大写字母或数字，不能包含空格或小写字母"
            )
        return self


class WorkOrderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_no: Annotated[
        str | None,
        Field(
            description="订单号，8-32 位大写字母或数字。"
            "售后类问题（运输破损/发货延迟/质量问题/退换货）必填；"
            "咨询、投诉建议、其他类型可不填",
        ),
    ] = None
    issue_type: Annotated[
        IssueType,
        Field(description="问题类型，必须是枚举值之一"),
    ]
    description: Annotated[
        str,
        Field(
            description="顾客描述的具体问题，需包含可核实的细节，10-500 字",
        ),
    ]

    @field_validator("order_no")
    @classmethod
    def valid_order_no(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        if not ORDER_NO_RE.match(value):
            raise ValueError(
                "order_no 需为 8-32 位大写字母或数字，不能包含空格或小写字母"
            )
        return value

    @field_validator("description")
    @classmethod
    def valid_description(cls, value: str) -> str:
        text = value.strip()
        if len(text) < 10:
            raise ValueError("问题描述太短，请补充具体现象、时间或部位等细节（至少 10 字）")
        if len(text) > 500:
            raise ValueError("问题描述过长，请精简到 500 字以内")
        return text

    @model_validator(mode="after")
    def order_required_for_after_sale(self) -> "WorkOrderCreate":
        if self.order_no is None and self.issue_type not in ORDERLESS_ISSUE_TYPES:
            raise ValueError(
                f"「{self.issue_type}」类工单必须提供订单号；"
                "如无订单请改用「咨询」「投诉建议」或「其他」"
            )
        return self
