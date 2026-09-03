"""Price drift and quote validity at order-creation time.

The quoted price is what the buyer agreed to. If the catalogue has moved,
the correct answer is "re-quote", never "charge the new number".
"""

from datetime import timedelta
from decimal import Decimal

from backend.database import utcnow_naive
from backend.databases.quote_db import quote_db
from backend.enums.QuoteStatus import QuoteStatus
from backend.enums.RejectionReason import RejectionReason
from backend.enums.TransactionStatus import TransactionStatus


def _set_price(db_session, product_id, cost):
    from backend.databases.product_db import product_db

    product = db_session.get(product_db, product_id)
    product.cost = cost
    db_session.commit()


# --- 7. an unchanged price lets the order through --------------------------


def test_unchanged_price_permits_order_creation(
    quote_for, order_from_quote, fake_razorpay
):
    quote = quote_for(quantity=2)
    body = order_from_quote(quote["quote_id"])

    assert body["transaction"]["status"] == TransactionStatus.ORDER_CREATED.value
    assert body["transaction"]["quote_id"] == quote["quote_id"]
    assert len(fake_razorpay.orders_created) == 1
    # The provider amount comes from the frozen quote, in integer paise.
    assert fake_razorpay.orders_created[0]["amount"] == 20000
    assert body["razorpay"]["amount_in_paise"] == 20000


def test_a_float_shaped_but_equal_price_is_not_drift(
    quote_for, order_from_quote, fake_razorpay, db_session, seed
):
    """100.0 and 100.00 are the same money. Comparison is in integer paise,
    so a harmless re-write of the same price must not invalidate a quote."""
    quote = quote_for(quantity=2)
    _set_price(db_session, seed["widget_id"], 100.00)

    body = order_from_quote(quote["quote_id"])
    assert body["transaction"]["status"] == TransactionStatus.ORDER_CREATED.value


# --- 8. a changed price stops the order dead -------------------------------


def test_price_increase_prevents_razorpay_order_creation(
    quote_for, order_from_quote, fake_razorpay, db_session, seed
):
    quote = quote_for(quantity=2)
    _set_price(db_session, seed["widget_id"], 150.0)

    body = order_from_quote(quote["quote_id"], expect=409)
    detail = body["detail"]

    assert detail["reason"] == RejectionReason.PRICE_DRIFT.value
    assert detail["requote_required"] is True
    assert Decimal(detail["quoted_unit_price"]) == Decimal("100.00")
    assert Decimal(detail["current_unit_price"]) == Decimal("150.00")

    # No provider contact, no transaction, and the quote is dead.
    assert fake_razorpay.orders_created == []
    from backend.databases.transaction_db import transaction_db

    assert db_session.query(transaction_db).count() == 0
    db_session.expire_all()
    assert db_session.get(quote_db, quote["quote_id"]).status is QuoteStatus.EXPIRED


def test_price_decrease_also_invalidates_the_quote(
    quote_for, order_from_quote, fake_razorpay, db_session, seed
):
    """For v1 ANY change invalidates. A cheaper price is still not the price
    the buyer was quoted, and silently substituting it is the behaviour this
    guard exists to prevent."""
    quote = quote_for(quantity=2)
    _set_price(db_session, seed["widget_id"], 90.0)

    body = order_from_quote(quote["quote_id"], expect=409)

    assert body["detail"]["reason"] == RejectionReason.PRICE_DRIFT.value
    assert fake_razorpay.orders_created == []


def test_drifted_quote_price_is_never_silently_replaced(
    quote_for, order_from_quote, db_session, seed, fake_razorpay
):
    quote = quote_for(quantity=2)
    _set_price(db_session, seed["widget_id"], 150.0)
    order_from_quote(quote["quote_id"], expect=409)

    db_session.expire_all()
    row = db_session.get(quote_db, quote["quote_id"])
    assert row.quoted_unit_price == Decimal("100.00")
    assert row.quoted_total == Decimal("200.00")


# --- 9. an expired quote cannot create an order ----------------------------


def test_expired_quote_prevents_order_creation(
    quote_for, order_from_quote, fake_razorpay, db_session
):
    quote = quote_for(quantity=2)

    row = db_session.get(quote_db, quote["quote_id"])
    row.expires_at = utcnow_naive() - timedelta(seconds=1)
    db_session.commit()

    body = order_from_quote(quote["quote_id"], expect=409)

    assert body["detail"]["reason"] == RejectionReason.QUOTE_EXPIRED.value
    assert fake_razorpay.orders_created == []
    db_session.expire_all()
    assert db_session.get(quote_db, quote["quote_id"]).status is QuoteStatus.EXPIRED


# --- 10. a consumed quote cannot be reused ---------------------------------


def test_consumed_quote_cannot_be_reused(
    paid_flow, order_from_quote, fake_razorpay, db_session
):
    quote, _order, verification = paid_flow()
    assert verification["quote_status"] == QuoteStatus.CONSUMED.value

    orders_before = len(fake_razorpay.orders_created)
    body = order_from_quote(quote["quote_id"], expect=409)

    assert body["detail"]["reason"] == RejectionReason.QUOTE_NOT_ACTIVE.value
    assert len(fake_razorpay.orders_created) == orders_before


def test_unknown_quote_is_refused(order_from_quote, fake_razorpay):
    body = order_from_quote("00000000-0000-0000-0000-0000000000ff", expect=404)

    assert body["detail"]["reason"] == RejectionReason.QUOTE_NOT_FOUND.value
    assert fake_razorpay.orders_created == []


def test_stock_falling_below_the_quote_prevents_order_creation(
    quote_for, order_from_quote, fake_razorpay, db_session, seed
):
    from backend.databases.product_db import product_db

    quote = quote_for(quantity=5)
    product = db_session.get(product_db, seed["widget_id"])
    product.quantity = 1
    db_session.commit()

    body = order_from_quote(quote["quote_id"], expect=409)

    assert body["detail"]["reason"] == RejectionReason.INSUFFICIENT_STOCK.value
    assert fake_razorpay.orders_created == []
