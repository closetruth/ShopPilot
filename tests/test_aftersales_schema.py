import pytest
from pydantic import ValidationError

from app.schemas.aftersales import AfterSalesExtract


def test_valid_extract():
    model = AfterSalesExtract(
        order_id="ORD-1",
        intent_type="logistics",
        expected_resolution="补发",
        raw_summary="包裹丢失",
    )

    assert model.intent_type == "logistics"


def test_invalid_intent():
    with pytest.raises(ValidationError):
        AfterSalesExtract(
            order_id=None,
            intent_type="hack",
            expected_resolution="x",
            raw_summary="y",
        )
