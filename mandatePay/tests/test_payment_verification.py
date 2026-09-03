"""Payment verification: the only path to PAID.

Every test here is really one question -- can anything other than a
server-verified signature move money, stock, or the transaction into PAID?
The answer must be no, including when the same callback arrives twice.
"""

from decimal import Decimal

import pytest
import requests

from backend.databases.buyer_db import buyer_db
from backend.databases.product_db import product_db
from backend.databases.quote_db import quote_db
from backend.databases.transaction_db import transaction_db
from backend.enums.QuoteStatus import QuoteStatus
from backend.enums.RejectionReason import RejectionReason
from backend.enums.TransactionStatus import TransactionStatus
from tests.conftest import BALANCE, sign

WIDGET_COST = Decimal("100.00")
START_STOCK = 100


@pytest.fixture()
def ordered(quote_for, order_from_quote, fake_razorpay):
    """A widget x2 purchase sitting at ORDER_CREATED, ready to be paid."""

    def _ordered(quantity=2, product="widget_id"):
        quote = quote_for(quantity=quantity, product=product)
        order = order_from_quote(quote["quote_id"])
        assert order["transaction"]["status"] == TransactionStatus.ORDER_CREATED.value
        return quote, order

    return _ordered


def verify(
    client,
    order,
    *,
    payment_id="pay_FAKE123",
    order_id=None,
    signature=None,
    expect=200,
):
    order_id = order_id or order["transaction"]["razorpay_order_id"]
    response = client.post(
        "/app/v1/payment/verify",
        json={
            "transaction_id": order["transaction"]["id"],
            "razorpay_order_id": order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": signature or sign(order_id, payment_id),
        },
    )
    assert response.status_code == expect, response.text
    return response.json()


def state(db_session, seed, order):
    db_session.expire_all()
    txn = db_session.get(transaction_db, order["transaction"]["id"])
    return (
        txn,
        db_session.get(buyer_db, seed["buyer_id"]).balance,
        db_session.get(product_db, seed["widget_id"]).quantity,
        db_session.get(quote_db, str(txn.quote_id)).status,
    )


# --- 16. a valid signature settles the payment -----------------------------


def test_valid_signature_marks_paid(client, seed, db_session, ordered):
    _quote, order = ordered()
    body = verify(client, order)

    assert body["verified"] is True
    assert body["already_verified"] is False
    assert body["transaction"]["status"] == TransactionStatus.PAID.value
    assert body["transaction"]["razorpay_payment_id"] == "pay_FAKE123"

    txn, _balance, _stock, _quote_status = state(db_session, seed, order)
    assert txn.status is TransactionStatus.PAID


def test_verification_uses_the_sdk_signature_utility(
    client, seed, ordered, fake_razorpay
):
    """The check is the SDK's own utility, given exactly the three fields
    Razorpay Checkout returns -- and nothing about the amount."""
    _quote, order = ordered()
    verify(client, order)

    assert len(fake_razorpay.signatures_checked) == 1
    assert set(fake_razorpay.signatures_checked[0]) == {
        "razorpay_order_id",
        "razorpay_payment_id",
        "razorpay_signature",
    }


def test_signature_is_never_persisted(client, seed, db_session, ordered):
    _quote, order = ordered()
    signature = sign(order["transaction"]["razorpay_order_id"], "pay_FAKE123")
    verify(client, order)

    db_session.expire_all()
    txn = db_session.get(transaction_db, order["transaction"]["id"])
    stored = " ".join(str(v) for v in vars(txn).values() if v is not None)
    assert signature not in stored


# --- 17 / 24 / 25. an invalid signature settles nothing --------------------


def test_invalid_signature_never_marks_paid(client, seed, db_session, ordered):
    _quote, order = ordered()
    before = state(db_session, seed, order)

    body = verify(client, order, signature="not-a-real-signature", expect=400)
    assert body["detail"]["reason"] == RejectionReason.SIGNATURE_VERIFICATION_FAILED.value

    txn, balance, stock, quote_status = state(db_session, seed, order)
    assert txn.status is TransactionStatus.FAILED
    assert txn.status is not TransactionStatus.PAID
    assert txn.razorpay_payment_id is None
    # 24 & 25: balance and stock are exactly as they were.
    assert balance == before[1] == BALANCE
    assert stock == before[2] == START_STOCK
    # The quote is not consumed by a failed attempt.
    assert quote_status is QuoteStatus.ACTIVE


# --- 18. a mismatched Razorpay order id is refused -------------------------


def test_wrong_razorpay_order_id_is_rejected(client, seed, db_session, ordered):
    _quote, order = ordered()

    body = verify(client, order, order_id="order_SOMEONE_ELSE", expect=409)
    assert body["detail"]["reason"] == RejectionReason.ORDER_ID_MISMATCH.value

    txn, balance, stock, quote_status = state(db_session, seed, order)
    # Refused, not failed: our transaction is still perfectly payable.
    assert txn.status is TransactionStatus.ORDER_CREATED
    assert balance == BALANCE
    assert stock == START_STOCK
    assert quote_status is QuoteStatus.ACTIVE


def test_payment_for_an_unknown_transaction_is_rejected(client, seed, ordered):
    _quote, order = ordered()
    order_id = order["transaction"]["razorpay_order_id"]
    response = client.post(
        "/app/v1/payment/verify",
        json={
            "transaction_id": "00000000-0000-0000-0000-0000000000ff",
            "razorpay_order_id": order_id,
            "razorpay_payment_id": "pay_FAKE123",
            "razorpay_signature": sign(order_id, "pay_FAKE123"),
        },
    )
    assert response.status_code == 404
    assert (
        response.json()["detail"]["reason"]
        == RejectionReason.TRANSACTION_NOT_FOUND.value
    )


def test_a_transaction_that_never_reached_order_created_cannot_be_paid(
    client, seed, db_session, quote_for, order_from_quote, fake_razorpay
):
    """An AWAITING_APPROVAL transaction has no Razorpay order at all. A
    callback aimed at it must not be able to settle it."""
    quote = quote_for(quantity=5, product="gadget_id")
    order = order_from_quote(quote["quote_id"])
    assert order["transaction"]["status"] == TransactionStatus.AWAITING_APPROVAL.value

    body = verify(client, order, order_id="order_FAKE123", expect=409)
    assert (
        body["detail"]["reason"]
        == RejectionReason.TRANSACTION_NOT_AWAITING_PAYMENT.value
    )

    db_session.expire_all()
    txn = db_session.get(transaction_db, order["transaction"]["id"])
    assert txn.status is TransactionStatus.AWAITING_APPROVAL


# --- 19 / 20 / 21. settlement moves balance, stock and the quote ----------


def test_successful_verification_decrements_balance_exactly_once(
    client, seed, db_session, ordered
):
    _quote, order = ordered(quantity=2)
    body = verify(client, order)

    _txn, balance, _stock, _status = state(db_session, seed, order)
    assert Decimal(str(balance)) == Decimal(str(BALANCE)) - WIDGET_COST * 2
    assert Decimal(body["buyer_balance"]) == Decimal(str(balance))


def test_successful_verification_decrements_stock_exactly_once(
    client, seed, db_session, ordered
):
    _quote, order = ordered(quantity=2)
    body = verify(client, order)

    _txn, _balance, stock, _status = state(db_session, seed, order)
    assert stock == START_STOCK - 2
    assert body["product_stock_remaining"] == stock


def test_successful_verification_consumes_the_quote(
    client, seed, db_session, ordered
):
    quote, order = ordered()
    body = verify(client, order)

    assert body["quote_status"] == QuoteStatus.CONSUMED.value
    db_session.expire_all()
    assert db_session.get(quote_db, quote["quote_id"]).status is QuoteStatus.CONSUMED


# --- 22. the same callback twice changes nothing the second time ----------


def test_duplicate_verification_callback_is_idempotent(
    client, seed, db_session, ordered
):
    _quote, order = ordered(quantity=2)

    first = verify(client, order)
    after_first = state(db_session, seed, order)

    second = verify(client, order)
    after_second = state(db_session, seed, order)

    assert first["already_verified"] is False
    assert second["already_verified"] is True
    assert second["verified"] is True
    assert second["transaction"]["status"] == TransactionStatus.PAID.value

    # Exactly once: balance, stock and quote are untouched by the replay.
    assert after_second[1] == after_first[1] == BALANCE - 200
    assert after_second[2] == after_first[2] == START_STOCK - 2
    assert after_second[3] is after_first[3] is QuoteStatus.CONSUMED
    assert after_second[0].status is TransactionStatus.PAID


def test_replayed_callback_does_not_re_verify_the_signature(
    client, seed, ordered, fake_razorpay
):
    """A replay is answered from our own record. It must not need the
    provider, and must not be able to mutate anything on the way."""
    _quote, order = ordered()
    verify(client, order)
    checks_after_first = len(fake_razorpay.signatures_checked)

    verify(client, order)
    assert len(fake_razorpay.signatures_checked) == checks_after_first


# --- 23. a different payment cannot double-charge a settled transaction ----


def test_second_payment_id_cannot_double_charge(client, seed, db_session, ordered):
    _quote, order = ordered(quantity=2)
    verify(client, order, payment_id="pay_FIRST")
    settled = state(db_session, seed, order)

    body = verify(client, order, payment_id="pay_SECOND", expect=409)
    assert body["detail"]["reason"] == RejectionReason.PAYMENT_ALREADY_SETTLED.value

    after = state(db_session, seed, order)
    assert after[1] == settled[1] == BALANCE - 200
    assert after[2] == settled[2] == START_STOCK - 2
    assert after[0].status is TransactionStatus.PAID
    # The original payment id stands. It is not overwritten.
    assert after[0].razorpay_payment_id == "pay_FIRST"


def test_a_different_order_id_against_a_settled_transaction_is_refused(
    client, seed, db_session, ordered
):
    _quote, order = ordered(quantity=2)
    verify(client, order, payment_id="pay_FIRST")

    body = verify(
        client,
        order,
        payment_id="pay_FIRST",
        order_id="order_OTHER",
        expect=409,
    )
    assert body["detail"]["reason"] == RejectionReason.PAYMENT_ALREADY_SETTLED.value

    _txn, balance, stock, _status = state(db_session, seed, order)
    assert balance == BALANCE - 200
    assert stock == START_STOCK - 2


# --- 26. only PAID transactions count toward monthly spend -----------------


def test_only_paid_transactions_count_toward_monthly_spend(
    client, seed, quote_for, order_from_quote, fake_razorpay, ordered
):
    """Monthly spend now works end-to-end, because PAID can genuinely
    happen. An unpaid order must not eat into the buyer's cap."""
    # An ORDER_CREATED transaction worth 200 that was never paid.
    unpaid = order_from_quote(quote_for(quantity=2)["quote_id"])
    assert unpaid["transaction"]["status"] == TransactionStatus.ORDER_CREATED.value

    # Cap 5000. Machine x 3 = 6000 alone breaks it; the projection is the
    # engine reporting exactly what it counted -- the unpaid 200 is absent.
    machine = quote_for(quantity=3, product="machine_id")
    violations = " ".join(machine["policy"]["hard_violations"])
    assert "monthly spend to 6000.00" in violations

    # Now actually settle a 250 payment (a different product, so this is not
    # a duplicate of the unpaid one).
    _quote, order = ordered(quantity=1, product="gadget_id")
    verify(client, order, payment_id="pay_COUNTED")

    machine_after = quote_for(quantity=3, product="machine_id")
    violations_after = " ".join(machine_after["policy"]["hard_violations"])
    assert "monthly spend to 6250.00" in violations_after


# --- 27. no test may reach the real provider -------------------------------


def test_the_suite_cannot_make_real_network_calls():
    """The `no_network` fixture is autouse, so this holds for every test in
    the suite, not just this one."""
    with pytest.raises(AssertionError):
        requests.get("https://api.razorpay.com/v1/orders")


def test_razorpay_module_in_routes_is_the_fake(fake_razorpay):
    from backend.commerce import product_routes

    assert product_routes.razorpay is fake_razorpay
