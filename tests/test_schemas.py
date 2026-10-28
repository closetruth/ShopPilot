import pytest
from pydantic import ValidationError

from app.schemas import ProductQuery, WorkOrderCreate, WorkOrderQuery


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


def test_create_accepts_valid_input():
    q = WorkOrderCreate(
        order_no="ORDER20260001",
        issue_type="运输破损",
        description="收到时杯口有裂纹，外包装完好无损",
    )
    assert q.issue_type == "运输破损"


def test_create_rejects_unknown_issue_type():
    with pytest.raises(ValidationError):
        WorkOrderCreate(
            order_no="ORDER20260001",
            issue_type="缺货",
            description="收到时杯口有裂纹，外包装完好无损",
        )


def test_create_rejects_short_description():
    with pytest.raises(ValidationError):
        WorkOrderCreate(
            order_no="ORDER20260001",
            issue_type="运输破损",
            description="坏了",
        )


def test_create_rejects_long_description():
    with pytest.raises(ValidationError):
        WorkOrderCreate(
            order_no="ORDER20260001",
            issue_type="运输破损",
            description="裂" * 501,
        )


def test_create_rejects_bad_order_no():
    with pytest.raises(ValidationError):
        WorkOrderCreate(
            order_no="order-2026",
            issue_type="运输破损",
            description="收到时杯口有裂纹，外包装完好无损",
        )


def test_create_rejects_extra_field():
    with pytest.raises(ValidationError):
        WorkOrderCreate(
            order_no="ORDER20260001",
            issue_type="运输破损",
            description="收到时杯口有裂纹，外包装完好无损",
            visitor_id="someone-else",
        )


def test_create_strips_description_whitespace():
    q = WorkOrderCreate(
        order_no="ORDER20260001",
        issue_type="运输破损",
        description="   收到时杯口有裂纹，外包装完好无损   ",
    )
    assert q.description == "收到时杯口有裂纹，外包装完好无损"
