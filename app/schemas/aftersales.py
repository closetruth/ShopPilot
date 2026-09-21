from typing import Literal

from pydantic import BaseModel


INTENT_TYPES = (
    "refund",
    "return",
    "exchange",
    "repair",
    "cancel",
    "logistics",
    "invoice",
    "other",
)

IntentType = Literal[
    "refund",
    "return",
    "exchange",
    "repair",
    "cancel",
    "logistics",
    "invoice",
    "other",
]


class AfterSalesExtract(BaseModel):
    order_id: str | None
    intent_type: IntentType
    expected_resolution: str
    raw_summary: str
