"""The monthly cap at settlement, and the settlement timestamp itself.

Two defects are pinned here.

The first: the monthly cap counted only PAID spend, but only order
creation checked it. Two orders of 4,000 against a 5,000 cap could both
reach ORDER_CREATED -- correctly, since neither had settled -- and then
both settle, so the buyer actually paid 8,000 against a 5,000 mandate.
The cap is now re-checked at the moment money moves.

The second: the month a payment counted against was taken from
`transactions.timestamp`, which is when the transaction was CREATED. An
order created on 31 August and paid on 1 September was charged to
August's allowance -- a month it did not spend. `paid_at` now records
when the money actually moved, and the cap is counted from that.
"""

from datetime import datetime

import pytest

from backend.commerce import product_routes
from backend.databases import quote_db as quote_db_module
from backend.databases.buyer_db import buyer_db
from backend.databases.product_db import product_db
from backend.databases.quote_db import quote_db
from backend.databases.transaction_db import transaction_db
from backend.enums.PolicyDecision import PolicyDecision
from backend.enums.QuoteStatus import QuoteStatus
from backend.enums.RejectionReason import RejectionReason
from backend.enums.TransactionStatus import TransactionStatus
from tests.conftest import BALANCE, MONTHLY_CAP, sign

START_STOCK = 100


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def verify(client, order, *, payment_id="pay_FAKE123", expect=200):
    order_id = order["transaction"]["razorpay_order_id"]
    response = client.post(
        "/app/v1/payment/verify",
        json={
            "transaction_id": order["transaction"]["id"],
            "razorpay_order_id": order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": sign(order_id, payment_id),
        },
    )
    assert response.status_code == expect, response.text
    return response.json()


@pytest.fixture()
def ordered(quote_for, order_from_quote, fake_razorpay):
    """A widget purchase sitting at ORDER_CREATED, ready to be paid."""

    def _ordered(quantity, product="widget_id"):
        quote = quote_for(quantity=quantity, product=product)
        order = order_from_quote(quote["quote_id"])
        assert order["transaction"]["status"] == TransactionStatus.ORDER_CREATED.value
        return quote, order

    return _ordered


def row(db_session, order):
    db_session.expire_all()
    return db_session.get(transaction_db, order["transaction"]["id"])


# ---------------------------------------------------------------------------
# A. The pipelined settlement that used to bypass the cap
# ---------------------------------------------------------------------------


def test_two_orders_created_before_either_pays_cannot_both_settle(
    client, db_session, seed, set_mandate, ordered
):
    """The exact bypass, end to end.

    Both orders are legitimately creatable: when each is created, nothing
    has been PAID, so neither breaches the cap. Only the second SETTLEMENT
    breaches it, and that is the moment it is now caught.
    """
    # Cap 5,000; both amounts autonomous so no approval is involved.
    set_mandate(autonomous_limit=5000.0, monthly_cap=MONTHLY_CAP)

    _quote_a, order_a = ordered(30)  # 3,000.00
    _quote_b, order_b = ordered(25)  # 2,500.00 -- 5,500.00 together

    # Both really did reach ORDER_CREATED before either was paid. That is
    # the state the old code settled twice.
    assert row(db_session, order_a).status is TransactionStatus.ORDER_CREATED
    assert row(db_session, order_b).status is TransactionStatus.ORDER_CREATED

    first = verify(client, order_a, payment_id="pay_ONE")
    assert first["transaction"]["status"] == TransactionStatus.PAID.value

    second = verify(client, order_b, payment_id="pay_TWO", expect=409)
    assert second["detail"]["reason"] == RejectionReason.MONTHLY_CAP_EXCEEDED.value

    db_session.expire_all()

    # Exactly one payment, and it is the first one.
    paid = (
        db_session.query(transaction_db)
        .filter(transaction_db.status == TransactionStatus.PAID)
        .all()
    )
    assert len(paid) == 1
    assert str(paid[0].id) == order_a["transaction"]["id"]

    # The refused one moved nothing at all.
    txn_b = row(db_session, order_b)
    assert txn_b.status is not TransactionStatus.PAID
    assert txn_b.paid_at is None
    assert txn_b.razorpay_payment_id is None

    # Balance and stock moved once, for the first purchase only.
    assert db_session.get(buyer_db, seed["buyer_id"]).balance == BALANCE - 3000.0
    assert (
        db_session.get(product_db, seed["widget_id"]).quantity == START_STOCK - 30
    )

    # The unsettled quote is not consumed -- nothing was collected against it.
    assert (
        db_session.get(quote_db, str(txn_b.quote_id)).status is not QuoteStatus.CONSUMED
    )


def test_settlement_at_exactly_the_cap_is_allowed(
    client, db_session, seed, set_mandate, ordered
):
    """The cap is a ceiling, not a fence. Landing exactly on it settles."""
    set_mandate(autonomous_limit=5000.0, monthly_cap=5000.0)

    _quote_a, order_a = ordered(30)  # 3,000.00
    _quote_b, order_b = ordered(20)  # 2,000.00 -- exactly 5,000.00 together

    verify(client, order_a, payment_id="pay_ONE")
    second = verify(client, order_b, payment_id="pay_TWO")

    assert second["transaction"]["status"] == TransactionStatus.PAID.value
    assert db_session.get(buyer_db, seed["buyer_id"]).balance == BALANCE - 5000.0


def test_cap_refusal_records_that_nothing_was_settled(
    client, db_session, seed, set_mandate, ordered, audit_events
):
    set_mandate(autonomous_limit=5000.0, monthly_cap=MONTHLY_CAP)
    _quote_a, order_a = ordered(30)
    _quote_b, order_b = ordered(25)

    verify(client, order_a, payment_id="pay_ONE")
    verify(client, order_b, payment_id="pay_TWO", expect=409)

    events = audit_events(transaction_id=order_b["transaction"]["id"])
    failures = [
        event
        for event in events
        if event.event_type == "PAYMENT_VERIFICATION_FAILED"
    ]
    assert failures, "the refused settlement must be recorded"
    detail = failures[-1].details
    assert detail["reason"] == RejectionReason.MONTHLY_CAP_EXCEEDED.value
    assert detail["paid"] is False
    assert detail["balance_or_stock_mutated"] is False


# ---------------------------------------------------------------------------
# B. The month boundary
# ---------------------------------------------------------------------------


class FakeClock:
    """A clock the test moves by hand. Never touches the system clock."""

    def __init__(self, now):
        self.now = now

    def set(self, now):
        self.now = now

    def __call__(self):
        return self.now


AUGUST = datetime(2025, 8, 31, 23, 50, 0)
SEPTEMBER = datetime(2025, 9, 1, 0, 10, 0)


@pytest.fixture()
def clock(monkeypatch):
    """Freeze the clock the commerce path reads.

    Both modules that ask the time on this path are moved together, so a
    quote issued at the fake time is not immediately expired by the real
    one.
    """
    fake = FakeClock(AUGUST)
    monkeypatch.setattr(product_routes, "utcnow_naive", fake)
    monkeypatch.setattr(quote_db_module, "utcnow_naive", fake)
    return fake


def test_payment_counts_against_the_month_it_settled_in(
    client, db_session, seed, set_mandate, ordered, clock
):
    """Created 31 August, paid 1 September: September's allowance pays."""
    set_mandate(autonomous_limit=5000.0, monthly_cap=MONTHLY_CAP)

    clock.set(AUGUST)
    _quote, order = ordered(30)  # 3,000.00

    # The transaction really was created in August, and that is left alone
    # -- `timestamp` keeps meaning "when this transaction was created".
    txn = row(db_session, order)
    txn.timestamp = AUGUST
    db_session.commit()
    assert txn.paid_at is None

    clock.set(SEPTEMBER)
    verify(client, order, payment_id="pay_SEPT")

    db_session.expire_all()
    txn = row(db_session, order)
    assert txn.status is TransactionStatus.PAID
    assert txn.paid_at == SEPTEMBER
    # Creation time is untouched: the two questions stay separate.
    assert txn.timestamp == AUGUST

    # It spends September's allowance, not August's.
    assert product_routes._monthly_spend(db_session, seed["buyer_id"], at=SEPTEMBER) == 3000.0
    assert product_routes._monthly_spend(db_session, seed["buyer_id"], at=AUGUST) == 0.0


def test_september_policy_sees_the_september_payment(
    client, db_session, seed, set_mandate, ordered, clock, quote_for
):
    """A later September evaluation must see the money as already spent."""
    set_mandate(autonomous_limit=5000.0, monthly_cap=3500.0)

    clock.set(AUGUST)
    _quote, order = ordered(30)  # 3,000.00
    txn = row(db_session, order)
    txn.timestamp = AUGUST
    db_session.commit()

    clock.set(SEPTEMBER)
    verify(client, order, payment_id="pay_SEPT")

    # 3,000 already paid this month, so a further 1,000 is over the 3,500
    # cap. Under the old timestamp-based reading the payment sat in
    # August and this would have been allowed.
    later = quote_for(quantity=10, product="widget_id")
    assert later["policy"]["decision"] == PolicyDecision.BLOCK.value
    assert "monthly_cap" in later["policy"]["violation_codes"]


def test_a_settlement_crossing_into_a_new_month_gets_that_months_cap(
    client, db_session, seed, set_mandate, ordered, clock
):
    """The cap re-check is against the SETTLEMENT month, not the creation
    month: August spend must not stop a September payment."""
    set_mandate(autonomous_limit=5000.0, monthly_cap=MONTHLY_CAP)

    # Two orders created in August, when nothing had yet been paid.
    clock.set(AUGUST)
    _quote_a, order_a = ordered(40)  # 4,000.00
    _quote_b, order_b = ordered(30)  # 3,000.00
    for order in (order_a, order_b):
        row(db_session, order).timestamp = AUGUST
    db_session.commit()

    # The first settles in August and spends 4,000 of August's 5,000.
    verify(client, order_a, payment_id="pay_AUG")
    assert row(db_session, order_a).paid_at == AUGUST

    # The second settles in September. Together they are 7,000, over the
    # 5,000 cap -- but they fall in different months, so September's cap
    # is untouched by August's spend. Counted against the creation month,
    # as it used to be, this settlement would have been refused.

    clock.set(SEPTEMBER)
    body = verify(client, order_b, payment_id="pay_SEP")

    assert body["transaction"]["status"] == TransactionStatus.PAID.value
    assert row(db_session, order_b).paid_at == SEPTEMBER
    assert product_routes._monthly_spend(db_session, seed["buyer_id"], at=AUGUST) == 4000.0
    assert product_routes._monthly_spend(db_session, seed["buyer_id"], at=SEPTEMBER) == 3000.0


# ---------------------------------------------------------------------------
# C. The paid_at lifecycle
# ---------------------------------------------------------------------------


def test_paid_at_is_null_before_verification(db_session, ordered):
    _quote, order = ordered(2)
    assert order["transaction"]["paid_at"] is None
    assert row(db_session, order).paid_at is None


def test_paid_at_is_written_only_by_verified_payment(client, db_session, ordered):
    _quote, order = ordered(2)

    before = product_routes.utcnow_naive()
    body = verify(client, order)
    after = product_routes.utcnow_naive()

    txn = row(db_session, order)
    assert txn.paid_at is not None
    assert before <= txn.paid_at <= after
    assert body["transaction"]["paid_at"] is not None


def test_failed_verification_leaves_paid_at_null(client, db_session, ordered):
    _quote, order = ordered(2)
    order_id = order["transaction"]["razorpay_order_id"]

    response = client.post(
        "/app/v1/payment/verify",
        json={
            "transaction_id": order["transaction"]["id"],
            "razorpay_order_id": order_id,
            "razorpay_payment_id": "pay_FORGED",
            "razorpay_signature": "not-a-real-signature",
        },
    )
    assert response.status_code == 400
    assert (
        response.json()["detail"]["reason"]
        == RejectionReason.SIGNATURE_VERIFICATION_FAILED.value
    )

    txn = row(db_session, order)
    assert txn.status is TransactionStatus.FAILED
    assert txn.paid_at is None


def test_replayed_verification_does_not_move_paid_at(client, db_session, ordered):
    """A replayed callback is answered "already done" and changes nothing,
    the settlement time included."""
    _quote, order = ordered(2)

    verify(client, order)
    first = row(db_session, order).paid_at

    replay = verify(client, order)
    assert replay["already_verified"] is True

    assert row(db_session, order).paid_at == first


def test_a_different_payment_after_settlement_cannot_rewrite_paid_at(
    client, db_session, ordered
):
    _quote, order = ordered(2)
    verify(client, order, payment_id="pay_REAL")
    settled_at = row(db_session, order).paid_at

    body = verify(client, order, payment_id="pay_OTHER", expect=409)
    assert body["detail"]["reason"] == RejectionReason.PAYMENT_ALREADY_SETTLED.value

    txn = row(db_session, order)
    assert txn.paid_at == settled_at
    assert txn.razorpay_payment_id == "pay_REAL"


def test_legacy_paid_rows_without_paid_at_still_count_against_the_cap(
    db_session, seed, add_transaction
):
    """A PAID row written before `paid_at` existed must not vanish from the
    cap. It falls back to its creation timestamp, which can only ever
    leave a buyer with less allowance, never more."""
    add_transaction(
        buyer_id=seed["buyer_id"],
        product_id=seed["widget_id"],
        quantity=1,
        amount=1200.0,
        status=TransactionStatus.PAID,
    )
    assert product_routes._monthly_spend(db_session, seed["buyer_id"]) == 1200.0
