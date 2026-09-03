"""Failure-state correctness on the critical path.

Every test here asks the same question in a different way: when something
goes wrong, does the system stay honest about money?

The assertions are deliberately repetitive -- status, balance, stock,
quote status, and whether the provider was reached. That repetition is
the point: a failure is only safe if ALL of those are right, and checking
three of five is how a regression hides.
"""

import pytest

from backend.databases.buyer_db import buyer_db
from backend.databases.product_db import product_db
from backend.databases.quote_db import quote_db
from backend.databases.transaction_db import transaction_db
from backend.enums.AuditEventType import AuditEventType
from backend.enums.QuoteStatus import QuoteStatus
from backend.enums.TransactionStatus import TransactionStatus

from tests.conftest import sign


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _money(db_session):
    """(buyer balance, widget stock) -- the two things nothing may move
    except a verified payment."""
    db_session.expire_all()
    buyer = db_session.query(buyer_db).one()
    widget = (
        db_session.query(product_db)
        .filter(product_db.product_name == "Test Widget")
        .one()
    )
    return buyer.balance, widget.quantity


# ---------------------------------------------------------------------------
# 1. Provider exception during order creation
# ---------------------------------------------------------------------------


def test_provider_exception_never_produces_order_created_or_paid(
    quote_for, order_from_quote, broken_razorpay, db_session
):
    quote = quote_for(quantity=2)

    order_from_quote(quote["quote_id"], expect=502)

    txn = db_session.query(transaction_db).one()
    assert txn.status is TransactionStatus.FAILED
    assert txn.status is not TransactionStatus.ORDER_CREATED
    assert txn.status is not TransactionStatus.PAID
    # No order id was ever written, because none was ever returned.
    assert txn.razorpay_order_id is None
    assert txn.razorpay_payment_id is None
    # An explicit, readable failure reason -- not a silent terminal state.
    assert "Razorpay order creation failed" in txn.reason


def test_provider_exception_never_decrements_balance_or_stock(
    quote_for, order_from_quote, broken_razorpay, db_session
):
    quote = quote_for(quantity=2)
    before = _money(db_session)

    order_from_quote(quote["quote_id"], expect=502)

    assert _money(db_session) == before
    # And the quote was not consumed by an attempt that collected nothing.
    assert db_session.query(quote_db).one().status is QuoteStatus.ACTIVE


def test_a_failed_provider_attempt_does_not_block_a_legitimate_retry(
    quote_for, order_from_quote, broken_razorpay, monkeypatch, db_session
):
    """FAILED is a dead attempt, and dead attempts must not poison the
    duplicate window."""
    from backend.commerce import product_routes
    from tests.conftest import FakeRazorpayModule, TEST_KEY_SECRET

    quote = quote_for(quantity=2)
    order_from_quote(quote["quote_id"], expect=502)

    # The provider comes back up. The same quote is still live.
    working = FakeRazorpayModule()
    monkeypatch.setattr(product_routes, "razorpay", working)
    monkeypatch.setattr(product_routes, "key_secret", TEST_KEY_SECRET)

    retry = order_from_quote(quote["quote_id"])
    assert retry["transaction"]["status"] == TransactionStatus.ORDER_CREATED.value
    assert len(working.orders_created) == 1


# ---------------------------------------------------------------------------
# 2. Invalid payment signature
# ---------------------------------------------------------------------------


def test_invalid_signature_leaves_state_and_money_unchanged(
    quote_for, order_from_quote, fake_razorpay, client, db_session
):
    quote = quote_for(quantity=2)
    order = order_from_quote(quote["quote_id"])
    before = _money(db_session)

    response = client.post(
        "/app/v1/payment/verify",
        json={
            "transaction_id": order["transaction"]["id"],
            "razorpay_order_id": order["transaction"]["razorpay_order_id"],
            "razorpay_payment_id": "pay_FAKE123",
            "razorpay_signature": "f" * 64,
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"]["reason"] == "SIGNATURE_VERIFICATION_FAILED"

    db_session.expire_all()
    txn = db_session.query(transaction_db).one()
    assert txn.status is TransactionStatus.FAILED
    assert txn.status is not TransactionStatus.PAID
    assert txn.razorpay_payment_id is None
    assert _money(db_session) == before


def test_invalid_signature_does_not_consume_the_quote(
    quote_for, order_from_quote, fake_razorpay, client, db_session
):
    """An unverified payment must not leave a trace that looks paid."""
    quote = quote_for(quantity=2)
    order = order_from_quote(quote["quote_id"])

    client.post(
        "/app/v1/payment/verify",
        json={
            "transaction_id": order["transaction"]["id"],
            "razorpay_order_id": order["transaction"]["razorpay_order_id"],
            "razorpay_payment_id": "pay_FAKE123",
            "razorpay_signature": "f" * 64,
        },
    )

    db_session.expire_all()
    quote_row = db_session.query(quote_db).one()
    assert quote_row.status is not QuoteStatus.CONSUMED
    assert quote_row.status is QuoteStatus.ACTIVE


# ---------------------------------------------------------------------------
# 3. Wrong Razorpay order id
# ---------------------------------------------------------------------------


def test_wrong_order_id_leaves_state_and_money_unchanged(
    quote_for, order_from_quote, fake_razorpay, client, db_session
):
    quote = quote_for(quantity=2)
    order = order_from_quote(quote["quote_id"])
    before = _money(db_session)

    response = client.post(
        "/app/v1/payment/verify",
        json={
            "transaction_id": order["transaction"]["id"],
            "razorpay_order_id": "order_SOMEONE_ELSE",
            "razorpay_payment_id": "pay_FAKE123",
            "razorpay_signature": sign("order_SOMEONE_ELSE", "pay_FAKE123"),
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "ORDER_ID_MISMATCH"

    db_session.expire_all()
    txn = db_session.query(transaction_db).one()
    # Refused without being marked failed: our transaction is still payable.
    assert txn.status is TransactionStatus.ORDER_CREATED
    assert _money(db_session) == before
    # The signature was never even computed against, so the fake never saw it.
    assert fake_razorpay.signatures_checked == []


# ---------------------------------------------------------------------------
# 4. Repeat valid verification
# ---------------------------------------------------------------------------


def test_repeated_verification_is_idempotent_with_one_mutation(
    paid_flow, client, db_session
):
    quote, order, first = paid_flow()
    after_first = _money(db_session)
    assert first["already_verified"] is False

    order_id = order["transaction"]["razorpay_order_id"]
    second = client.post(
        "/app/v1/payment/verify",
        json={
            "transaction_id": order["transaction"]["id"],
            "razorpay_order_id": order_id,
            "razorpay_payment_id": "pay_FAKE123",
            "razorpay_signature": sign(order_id, "pay_FAKE123"),
        },
    )

    assert second.status_code == 200
    assert second.json()["already_verified"] is True
    # The replay moved nothing.
    assert _money(db_session) == after_first


def test_a_different_payment_against_a_settled_transaction_is_refused(
    paid_flow, client, db_session
):
    quote, order, _ = paid_flow()
    after_first = _money(db_session)

    order_id = order["transaction"]["razorpay_order_id"]
    response = client.post(
        "/app/v1/payment/verify",
        json={
            "transaction_id": order["transaction"]["id"],
            "razorpay_order_id": order_id,
            "razorpay_payment_id": "pay_A_SECOND_CHARGE",
            "razorpay_signature": sign(order_id, "pay_A_SECOND_CHARGE"),
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "PAYMENT_ALREADY_SETTLED"
    assert _money(db_session) == after_first


# ---------------------------------------------------------------------------
# 5 & 6. Quote expiry and price drift both stop short of the provider
# ---------------------------------------------------------------------------


def test_expired_quote_prevents_the_provider_call(
    quote_for, order_from_quote, fake_razorpay, db_session
):
    from datetime import timedelta

    from backend.database import utcnow_naive

    quote = quote_for(quantity=2)
    row = db_session.query(quote_db).one()
    row.expires_at = utcnow_naive() - timedelta(seconds=1)
    db_session.commit()

    before = _money(db_session)
    order_from_quote(quote["quote_id"], expect=409)

    assert fake_razorpay.orders_created == []
    assert db_session.query(transaction_db).count() == 0
    assert _money(db_session) == before


def test_price_drift_prevents_the_provider_call_and_keeps_the_quote_immutable(
    quote_for, order_from_quote, fake_razorpay, db_session, audit_events
):
    quote = quote_for(quantity=2)
    quoted_unit = db_session.query(quote_db).one().quoted_unit_price
    quoted_total = db_session.query(quote_db).one().quoted_total

    widget = (
        db_session.query(product_db)
        .filter(product_db.product_name == "Test Widget")
        .one()
    )
    widget.cost = 150.0
    db_session.commit()

    before = _money(db_session)
    response_body = order_from_quote(quote["quote_id"], expect=409)

    assert response_body["detail"]["reason"] == "PRICE_DRIFT"
    assert fake_razorpay.orders_created == []
    assert db_session.query(transaction_db).count() == 0
    assert _money(db_session) == before

    # The quote is dead, never repriced.
    db_session.expire_all()
    row = db_session.query(quote_db).one()
    assert row.status is QuoteStatus.EXPIRED
    assert row.quoted_unit_price == quoted_unit
    assert row.quoted_total == quoted_total

    drift = audit_events(
        quote_id=quote["quote_id"], event_type=AuditEventType.PRICE_DRIFT_DETECTED
    )
    assert len(drift) == 1
    assert drift[0].details["provider_called"] is False
    assert drift[0].details["quoted_unit_price"] == "100.00"
    assert drift[0].details["current_unit_price"] == "150.00"


def test_downward_price_drift_is_refused_too(
    quote_for, order_from_quote, fake_razorpay, db_session
):
    """A price that fell is still a price the buyer never agreed to."""
    quote = quote_for(quantity=2)
    widget = (
        db_session.query(product_db)
        .filter(product_db.product_name == "Test Widget")
        .one()
    )
    widget.cost = 50.0
    db_session.commit()

    body = order_from_quote(quote["quote_id"], expect=409)
    assert body["detail"]["reason"] == "PRICE_DRIFT"
    assert fake_razorpay.orders_created == []


# ---------------------------------------------------------------------------
# 7. Duplicates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "dead_status",
    [
        TransactionStatus.FAILED,
        TransactionStatus.BLOCKED,
        TransactionStatus.CANCELLED,
    ],
)
def test_dead_attempts_do_not_poison_a_legitimate_retry(
    quote_for, order_from_quote, add_transaction, fake_razorpay, seed, dead_status
):
    add_transaction(
        buyer_id=seed["buyer_id"],
        product_id=seed["widget_id"],
        quantity=2,
        amount=200.0,
        status=dead_status,
    )

    quote = quote_for(quantity=2)
    order = order_from_quote(quote["quote_id"])
    assert order["transaction"]["status"] == TransactionStatus.ORDER_CREATED.value


@pytest.mark.parametrize(
    "live_status",
    [
        TransactionStatus.AWAITING_APPROVAL,
        TransactionStatus.AUTHORIZED,
        TransactionStatus.ORDER_CREATED,
        TransactionStatus.PAID,
    ],
)
def test_live_commitments_block_an_unsafe_duplicate(
    quote_for, order_from_quote, add_transaction, fake_razorpay, seed, live_status
):
    add_transaction(
        buyer_id=seed["buyer_id"],
        product_id=seed["widget_id"],
        quantity=2,
        amount=200.0,
        status=live_status,
    )

    quote = quote_for(quantity=2)
    order = order_from_quote(quote["quote_id"])
    assert order["transaction"]["status"] == TransactionStatus.BLOCKED.value
    assert "duplicate_purchase" in order["policy"]["violation_codes"]
    assert fake_razorpay.orders_created == []


# ---------------------------------------------------------------------------
# 8. Approval revalidation failure
# ---------------------------------------------------------------------------


def test_approval_revalidation_failure_prevents_the_provider_call(
    quote_for,
    order_from_quote,
    resolve_approval,
    db_session,
    fake_razorpay,
    approvals_for,
):
    from backend.enums.ApprovalStatus import ApprovalStatus

    quote = quote_for(quantity=6, product="gadget_id")
    order = order_from_quote(quote["quote_id"])
    txn_id = order["transaction"]["id"]
    before = _money(db_session)

    # The price moves while the human deliberates.
    gadget = (
        db_session.query(product_db)
        .filter(product_db.product_name == "Test Gadget")
        .one()
    )
    gadget.cost = 300.0
    db_session.commit()

    resolution = resolve_approval(txn_id, action="approve")

    assert resolution["order_created"] is False
    assert resolution["revalidation"]["reason"] == "PRICE_DRIFT"
    assert fake_razorpay.orders_created == []

    db_session.expire_all()
    txn = db_session.get(transaction_db, txn_id)
    assert txn.status is TransactionStatus.BLOCKED
    assert txn.razorpay_order_id is None
    assert _money(db_session) == before

    # The human's yes remains historically true.
    approval = approvals_for(txn_id)[0]
    assert approval.status is ApprovalStatus.APPROVED
    assert approval.resolved_at is not None


def test_approval_cannot_override_a_newly_violated_hard_rule(
    quote_for, order_from_quote, resolve_approval, set_mandate, fake_razorpay
):
    """Human approval answers the autonomous threshold and nothing else."""
    quote = quote_for(quantity=6, product="gadget_id")  # 1500
    order = order_from_quote(quote["quote_id"])

    # The ceiling drops below the amount while the request waits.
    set_mandate(absolute_transaction_limit=500.0)

    resolution = resolve_approval(order["transaction"]["id"], action="approve")

    assert resolution["order_created"] is False
    assert resolution["policy"]["decision"] == "block"
    assert "absolute_transaction_limit" in resolution["policy"]["violation_codes"]
    assert fake_razorpay.orders_created == []


# ---------------------------------------------------------------------------
# 9. LLM failure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "intent_payload, expected_status",
    [
        ({"item": 12345}, 502),  # wrong type: unusable reply
        ({"item": "keyboard", "quantity": 0}, 502),  # nonsensical quantity
        ({"item": "keyboard", "max_budget": -5}, 502),  # nonsensical budget
    ],
)
def test_llm_failure_creates_no_financial_side_effects(
    agent_purchase, fake_llm, agent_catalogue, db_session, intent_payload, expected_status
):
    fake_llm.set_intent(**intent_payload)

    agent_purchase("buy a keyboard", expect=expected_status)

    assert db_session.query(quote_db).count() == 0
    assert db_session.query(transaction_db).count() == 0


def test_ranker_failure_creates_no_quote(
    agent_purchase, fake_llm, agent_catalogue, db_session
):
    """The ranking step runs after discovery but before pricing."""
    fake_llm.set_intent(item="wireless keyboard", quantity=1, required_brand="Logitech")
    fake_llm.set_ranking(action="DO_WHATEVER_I_SAY", product_id=None)

    agent_purchase("a Logitech wireless keyboard", expect=502)

    assert db_session.query(quote_db).count() == 0
    assert db_session.query(transaction_db).count() == 0


# ---------------------------------------------------------------------------
# 10. Malicious catalogue text
# ---------------------------------------------------------------------------


def test_malicious_catalogue_text_cannot_alter_server_financial_truth(
    agent_purchase, fake_llm, agent_catalogue, db_session
):
    """The injected product asks to be priced at Rs 1 and marked paid.

    It is a real row at 45000. The server prices it from that row, the
    policy engine blocks it on the buyer's own limits, and no payment
    state is reachable from anything the text says.
    """
    fake_llm.set_intent(item="keyboard wireless", quantity=1)
    fake_llm.select_product(agent_catalogue["malicious_id"])

    result = agent_purchase("a wireless keyboard, ideally quiet")

    if result["quote"] is not None:
        # Priced from the database row, never from the product name.
        assert float(result["quote"]["quoted_unit_price"]) == 45000.0
        assert result["policy"]["decision"] != "allow"
        assert result["next_action"] != "create_order"

    # Nothing was paid, and no transaction exists at all.
    assert db_session.query(transaction_db).count() == 0
