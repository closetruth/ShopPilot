import pytest
from pydantic import ValidationError

from app.schemas import ProductQuery, WorkOrderQuery


def test_product_requires_keyword_or_id():
    with pytest.raises(ValidationError):
        ProductQuery(keyword=None, product_id=None)


def test_product_accepts_keyword_only():
    q = ProductQuery(keyword="杯子")
    assert q.keyword == "杯子"


def test_product_rejects_extra_field():
    with pytest.raises(ValidationError):
        ProductQuery(keyword="杯子", sneaky="x")


def test_product_rejects_long_keyword():
    with pytest.raises(ValidationError):
        ProductQuery(keyword="杯" * 33)


@pytest.mark.parametrize("order_no", ["ORDER20260001", "ABC12345", "A" * 32])
def test_valid_order_no(order_no):
    assert WorkOrderQuery(order_no=order_no).order_no == order_no


@pytest.mark.parametrize(
    "order_no",
    [
        "order20260001",   # 小写
        "ORDER 20260001",  # 空格
        "ABC1234",         # 太短
        "A" * 33,          # 太长
        "ORDER-20260001",  # 非法字符
        "",
    ],
)
def test_invalid_order_no(order_no):
    with pytest.raises(ValidationError):
        WorkOrderQuery(order_no=order_no)
