"""工具入参的校验模型。"""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
