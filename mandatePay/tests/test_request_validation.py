"""Quantity must be rejected at the schema boundary."""

import pytest
from pydantic import ValidationError

from backend.models.order_request import OrderRequest


# --- 9 & 10. quantity 0 / negative -> schema validation failure ------------


@pytest.mark.parametrize("quantity", [0, -1, -5])
def test_non_positive_quantity_rejected_by_schema(quantity):
    with pytest.raises(ValidationError):
        OrderRequest(buyer_id="b", product_id="p", quantity=quantity)


def test_positive_quantity_accepted():
    assert OrderRequest(buyer_id="b", product_id="p", quantity=1).quantity == 1


@pytest.mark.parametrize("quantity", [0, -5])
@pytest.mark.parametrize("endpoint", ["/app/v1/quote", "/app/v1/create-order"])
def test_non_positive_quantity_rejected_by_api(client, seed, quantity, endpoint):
    """422 before any handler code runs -- so a negative amount is never
    computed, never evaluated, and can never reach Razorpay."""
    response = client.post(
        endpoint,
        json={
            "buyer_id": seed["buyer_id"],
            "product_id": seed["widget_id"],
            "quantity": quantity,
        },
    )
    assert response.status_code == 422
