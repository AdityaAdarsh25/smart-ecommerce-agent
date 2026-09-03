"""The persisted quote: a server-chosen price, frozen, with an expiry.

A quote is the only place a price is fixed, so these tests are mostly
about what the caller CANNOT influence.
"""

from datetime import datetime
from decimal import Decimal

import pytest

from backend.commerce.product_routes import QUOTE_TTL
from backend.databases.quote_db import quote_db
from backend.enums.PolicyDecision import PolicyDecision
from backend.enums.QuoteStatus import QuoteStatus
from backend.enums.RejectionReason import RejectionReason
from backend.enums.TransactionStatus import TransactionStatus

WIDGET_COST = Decimal("100.00")


# --- 1. the quote persists the current server-side price -------------------


def test_quote_persists_the_current_price(quote_for, db_session):
    body = quote_for(quantity=2)

    row = db_session.get(quote_db, body["quote_id"])
    assert row is not None
    assert row.quoted_unit_price == WIDGET_COST
    assert row.quoted_total == WIDGET_COST * 2
    assert row.status is QuoteStatus.ACTIVE
    # Decimal on the way out, not float.
    assert isinstance(row.quoted_total, Decimal)


def test_quote_response_reports_the_persisted_price(quote_for, db_session):
    body = quote_for(quantity=3)
    row = db_session.get(quote_db, body["quote_id"])

    assert Decimal(body["quoted_unit_price"]) == row.quoted_unit_price
    assert Decimal(body["quoted_total"]) == row.quoted_total
    assert body["product"]["product_name"] == "Test Widget"


# --- 2. total is unit price x quantity -------------------------------------


@pytest.mark.parametrize("quantity", [1, 2, 3, 7])
def test_quoted_total_is_unit_price_times_quantity(quote_for, quantity):
    body = quote_for(quantity=quantity)

    unit = Decimal(body["quoted_unit_price"])
    assert Decimal(body["quoted_total"]) == unit * quantity
    assert body["quantity"] == quantity


# --- 3. the quote expires --------------------------------------------------


def test_quote_has_a_deterministic_expiry(quote_for, db_session):
    body = quote_for()

    created = datetime.fromisoformat(body["created_at"])
    expires = datetime.fromisoformat(body["expires_at"])
    assert expires > created
    assert expires - created == QUOTE_TTL

    row = db_session.get(quote_db, body["quote_id"])
    assert not row.is_expired()


# --- 4. invalid quantity is rejected ---------------------------------------


@pytest.mark.parametrize("quantity", [0, -1, -50])
def test_non_positive_quantity_is_rejected_and_persists_nothing(
    client, seed, db_session, quantity
):
    response = client.post(
        "/app/v1/quote",
        json={
            "buyer_id": seed["buyer_id"],
            "product_id": seed["widget_id"],
            "quantity": quantity,
        },
    )
    assert response.status_code == 422
    assert db_session.query(quote_db).count() == 0


# --- 5. insufficient stock is refused --------------------------------------


def test_insufficient_stock_is_refused_and_persists_nothing(
    client, seed, db_session, quote_for
):
    body = quote_for(quantity=999, expect=409)

    assert body["detail"]["reason"] == RejectionReason.INSUFFICIENT_STOCK.value
    assert db_session.query(quote_db).count() == 0


# --- 6. the caller cannot choose the price ---------------------------------


def test_caller_supplied_price_fields_are_ignored(client, seed, db_session):
    """Prices smuggled into the request body must have no effect. The only
    price that exists is the one the server read from its own catalogue."""
    response = client.post(
        "/app/v1/quote",
        json={
            "buyer_id": seed["buyer_id"],
            "product_id": seed["widget_id"],
            "quantity": 2,
            # All of these are attempts to set the price. None is an input.
            "cost": 1,
            "amount": 1,
            "price": 1,
            "quoted_unit_price": 1,
            "quoted_total": 1,
        },
    )
    assert response.status_code == 200
    body = response.json()

    assert Decimal(body["quoted_unit_price"]) == WIDGET_COST
    assert Decimal(body["quoted_total"]) == WIDGET_COST * 2

    row = db_session.get(quote_db, body["quote_id"])
    assert row.quoted_total == WIDGET_COST * 2


# --- the quote is a policy decision, never a payment state -----------------


def test_quote_reports_a_policy_decision_and_no_transaction_status(quote_for):
    body = quote_for(quantity=2)

    assert body["policy"]["decision"] == PolicyDecision.ALLOW.value
    assert body["quote_status"] == QuoteStatus.ACTIVE.value
    # A quote status is not a transaction status, and must never be one.
    assert body["quote_status"] not in {s.value for s in TransactionStatus}


def test_quote_creates_no_transaction(quote_for, db_session):
    from backend.databases.transaction_db import transaction_db

    quote_for(quantity=2)
    assert db_session.query(transaction_db).count() == 0
