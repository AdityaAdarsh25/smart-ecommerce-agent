"""Human approval: what it authorizes, and everything it does not.

The property under test throughout is that approval is narrow. A human
answers the autonomous-threshold question and nothing else, so between
"approved" and "order created" the whole hard rule set is re-run against
the world as it is at that moment -- not as it was when the request was
raised.
"""

import pytest
import requests

from backend.databases.buyer_db import buyer_db
from backend.databases.product_db import product_db
from backend.databases.quote_db import quote_db
from backend.enums.ApprovalStatus import ApprovalStatus
from backend.enums.PolicyDecision import PolicyDecision
from backend.enums.QuoteStatus import QuoteStatus
from backend.enums.RejectionReason import RejectionReason
from backend.enums.TransactionStatus import TransactionStatus
from backend.policies.policy_engine import (
    RULE_AUTONOMOUS_LIMIT,
    RULE_BUYER_BALANCE,
    RULE_CATEGORY_ALLOWED,
    RULE_MERCHANT_ALLOWED,
    RULE_MONTHLY_CAP,
)
from tests.conftest import sign

# 40 x 100.00 = 4000.00: above the 3000 autonomous threshold, below the
# 6000 absolute ceiling. The approval band.
APPROVAL_QUANTITY = 40


@pytest.fixture()
def pending(set_mandate, quote_for, order_from_quote, fake_razorpay):
    """Drive a purchase into AWAITING_APPROVAL and hand back the pieces."""

    def _pending(quantity=APPROVAL_QUANTITY):
        set_mandate(
            autonomous_limit=3000.0,
            absolute_transaction_limit=6000.0,
            monthly_cap=50000.0,
        )
        quote = quote_for(quantity=quantity)
        order = order_from_quote(quote["quote_id"])
        assert (
            order["transaction"]["status"] == TransactionStatus.AWAITING_APPROVAL.value
        )
        assert fake_razorpay.orders_created == []
        return quote, order

    return _pending


# --- 10. REQUIRE_APPROVAL creates exactly one pending approval -------------


def test_require_approval_creates_one_pending_approval(
    pending, approvals_for, client
):
    _, order = pending()
    txn_id = order["transaction"]["id"]

    assert order["approval"] is not None
    assert order["approval"]["status"] == ApprovalStatus.PENDING.value
    assert order["approval"]["transaction_id"] == txn_id
    assert order["approval"]["resolved_at"] is None

    rows = approvals_for(txn_id)
    assert len(rows) == 1

    queue = client.get("/app/v1/approvals").json()
    assert queue["count"] == 1


# --- 11. a repeated identical request opens no second approval -------------


def test_repeated_identical_request_does_not_stack_approvals(
    pending, quote_for, order_from_quote, client, fake_razorpay
):
    pending()

    # Same buyer, same product, same quantity, same amount, inside the
    # duplicate window: this is the retry that used to pile up requests.
    second_quote = quote_for(quantity=APPROVAL_QUANTITY)
    second = order_from_quote(second_quote["quote_id"])

    assert second["transaction"]["status"] == TransactionStatus.BLOCKED.value
    assert second["approval"] is None

    queue = client.get("/app/v1/approvals").json()
    assert queue["count"] == 1, "a duplicate request must not queue a second human ask"
    assert fake_razorpay.orders_created == []


# --- 12 & 13. rejection moves nothing --------------------------------------


def test_reject_never_contacts_razorpay(pending, resolve_approval, fake_razorpay):
    _, order = pending()

    body = resolve_approval(order["transaction"]["id"], "reject")

    assert body["approval"]["status"] == ApprovalStatus.REJECTED.value
    assert body["approval"]["resolved_at"] is not None
    assert body["order_created"] is False
    assert body["transaction"]["status"] == TransactionStatus.CANCELLED.value
    assert body["transaction"]["razorpay_order_id"] is None
    assert fake_razorpay.orders_created == []


def test_reject_mutates_no_balance_stock_or_quote(
    pending, resolve_approval, db_session, seed
):
    quote, order = pending()
    buyer_before = db_session.get(buyer_db, seed["buyer_id"]).balance
    stock_before = db_session.get(product_db, seed["widget_id"]).quantity

    resolve_approval(order["transaction"]["id"], "reject")
    db_session.expire_all()

    assert db_session.get(buyer_db, seed["buyer_id"]).balance == buyer_before
    assert db_session.get(product_db, seed["widget_id"]).quantity == stock_before

    # The quote is closed, but never as if it had funded a purchase.
    stored = db_session.get(quote_db, quote["quote_id"])
    assert stored.status is QuoteStatus.CANCELLED
    assert stored.status is not QuoteStatus.CONSUMED


# --- 14, 15, 16. approval with an unchanged world --------------------------


def test_approve_with_unchanged_state_creates_the_order(
    pending, resolve_approval, fake_razorpay
):
    _, order = pending()

    body = resolve_approval(order["transaction"]["id"], "approve")

    assert body["approval"]["status"] == ApprovalStatus.APPROVED.value
    assert body["order_created"] is True
    assert body["transaction"]["status"] == TransactionStatus.ORDER_CREATED.value
    assert body["razorpay"]["razorpay_order_id"] == body["transaction"][
        "razorpay_order_id"
    ]
    # 4000.00, straight from the frozen quote.
    assert body["razorpay"]["amount_in_paise"] == 400000
    assert len(fake_razorpay.orders_created) == 1


def test_approve_does_not_set_paid(
    pending, resolve_approval, db_session, seed
):
    _, order = pending()

    body = resolve_approval(order["transaction"]["id"], "approve")

    assert body["transaction"]["status"] != TransactionStatus.PAID.value
    assert body["transaction"]["razorpay_payment_id"] is None
    db_session.expire_all()
    # An order is an intent to collect, not a collection.
    assert db_session.get(buyer_db, seed["buyer_id"]).balance == 50000.0
    assert db_session.get(product_db, seed["widget_id"]).quantity == 100
    assert db_session.get(quote_db, order["transaction"]["quote_id"]).status is (
        QuoteStatus.ACTIVE
    )


def test_approved_transaction_is_not_trapped_in_require_approval(
    pending, resolve_approval, client
):
    """The loop this guards against: re-evaluating an approved transaction
    finds it over the autonomous threshold and asks for approval again."""
    _, order = pending()

    body = resolve_approval(order["transaction"]["id"], "approve")

    assert body["policy"]["decision"] == PolicyDecision.ALLOW.value
    assert body["policy"]["approval_reasons"] == []
    assert body["policy"]["approval_codes"] == []
    assert (
        f"{RULE_AUTONOMOUS_LIMIT}: satisfied_by_human_approval"
        in body["policy"]["evaluated_rules"]
    )
    assert body["transaction"]["status"] != TransactionStatus.AWAITING_APPROVAL.value
    assert client.get("/app/v1/approvals").json()["count"] == 0


def test_approve_does_not_block_itself_as_its_own_duplicate(
    pending, resolve_approval
):
    """The AWAITING_APPROVAL row is itself a live commitment for this exact
    purchase, so re-evaluation must exclude it."""
    _, order = pending()

    body = resolve_approval(order["transaction"]["id"], "approve")

    assert body["order_created"] is True
    assert body["policy"]["violation_codes"] == []


# --- 17-21. the world moved while the request waited -----------------------


def test_approval_does_not_permit_a_stale_price(
    pending, resolve_approval, db_session, seed, fake_razorpay
):
    quote, order = pending()
    product = db_session.get(product_db, seed["widget_id"])
    product.cost = 150.0
    db_session.commit()

    body = resolve_approval(order["transaction"]["id"], "approve")

    # The human approved; the purchase still does not happen.
    assert body["approval"]["status"] == ApprovalStatus.APPROVED.value
    assert body["order_created"] is False
    assert body["revalidation"]["reason"] == RejectionReason.PRICE_DRIFT.value
    assert body["transaction"]["status"] == TransactionStatus.BLOCKED.value
    assert fake_razorpay.orders_created == []
    db_session.expire_all()
    assert db_session.get(quote_db, quote["quote_id"]).status is QuoteStatus.EXPIRED


def test_approval_does_not_override_the_monthly_cap(
    pending, resolve_approval, add_transaction, seed, fake_razorpay
):
    _, order = pending()

    # Another purchase settles while the request waits, eating the cap.
    add_transaction(
        buyer_id=seed["buyer_id"],
        product_id=seed["gadget_id"],
        quantity=10,
        amount=48000.0,
        status=TransactionStatus.PAID,
    )

    body = resolve_approval(order["transaction"]["id"], "approve")

    assert body["order_created"] is False
    assert body["revalidation"]["reason"] == RejectionReason.POLICY_BLOCKED.value
    assert RULE_MONTHLY_CAP in body["policy"]["violation_codes"]
    assert body["transaction"]["status"] == TransactionStatus.BLOCKED.value
    assert fake_razorpay.orders_created == []


def test_approval_does_not_override_insufficient_balance(
    pending, resolve_approval, db_session, seed, fake_razorpay
):
    _, order = pending()
    buyer = db_session.get(buyer_db, seed["buyer_id"])
    buyer.balance = 100.0
    db_session.commit()

    body = resolve_approval(order["transaction"]["id"], "approve")

    assert body["order_created"] is False
    assert RULE_BUYER_BALANCE in body["policy"]["violation_codes"]
    assert fake_razorpay.orders_created == []


def test_approval_does_not_override_stock_that_ran_out(
    pending, resolve_approval, db_session, seed, fake_razorpay
):
    _, order = pending()
    product = db_session.get(product_db, seed["widget_id"])
    product.quantity = 1
    db_session.commit()

    body = resolve_approval(order["transaction"]["id"], "approve")

    assert body["order_created"] is False
    assert body["revalidation"]["reason"] == RejectionReason.INSUFFICIENT_STOCK.value
    assert body["transaction"]["status"] == TransactionStatus.BLOCKED.value
    assert fake_razorpay.orders_created == []


def test_approval_does_not_override_a_newly_forbidden_merchant(
    pending, resolve_approval, set_mandate, seed, fake_razorpay
):
    _, order = pending()
    set_mandate(allowed_merchants=[seed["other_merchant_id"]])

    body = resolve_approval(order["transaction"]["id"], "approve")

    assert body["order_created"] is False
    assert RULE_MERCHANT_ALLOWED in body["policy"]["violation_codes"]
    assert fake_razorpay.orders_created == []


def test_approval_does_not_override_a_newly_forbidden_category(
    pending, resolve_approval, set_mandate, fake_razorpay
):
    _, order = pending()
    set_mandate(allowed_categories=["groceries"])

    body = resolve_approval(order["transaction"]["id"], "approve")

    assert body["order_created"] is False
    assert RULE_CATEGORY_ALLOWED in body["policy"]["violation_codes"]
    assert fake_razorpay.orders_created == []


# --- 22. an answered approval stays answered -------------------------------


@pytest.mark.parametrize(
    "first,second",
    [
        ("approve", "approve"),
        ("approve", "reject"),
        ("reject", "approve"),
        ("reject", "reject"),
    ],
)
def test_a_resolved_approval_cannot_be_resolved_again(
    pending, resolve_approval, approvals_for, first, second, fake_razorpay
):
    _, order = pending()
    txn_id = order["transaction"]["id"]

    resolve_approval(txn_id, first)
    before = approvals_for(txn_id)[0].status

    body = resolve_approval(txn_id, second, expect=409)

    assert body["detail"]["reason"] == RejectionReason.APPROVAL_ALREADY_RESOLVED.value
    rows = approvals_for(txn_id)
    assert len(rows) == 1
    assert rows[0].status is before, "the recorded human decision must not change"
    assert len(fake_razorpay.orders_created) == (1 if first == "approve" else 0)


def test_resolving_a_transaction_with_no_approval_is_refused(
    set_mandate, quote_for, order_from_quote, resolve_approval, fake_razorpay
):
    """An ALLOW transaction was never anyone's to approve."""
    set_mandate(
        autonomous_limit=3000.0,
        absolute_transaction_limit=6000.0,
        monthly_cap=50000.0,
    )
    quote = quote_for(quantity=25)
    order = order_from_quote(quote["quote_id"])

    body = resolve_approval(order["transaction"]["id"], "approve", expect=404)
    assert body["detail"]["reason"] == RejectionReason.APPROVAL_NOT_FOUND.value


def test_resolving_an_unknown_transaction_is_refused(resolve_approval):
    body = resolve_approval(
        "00000000-0000-0000-0000-0000000000ff", "approve", expect=404
    )
    assert body["detail"]["reason"] == RejectionReason.TRANSACTION_NOT_FOUND.value


# --- 23. a hard BLOCK is never approvable ----------------------------------


def test_hard_blocked_transaction_offers_nothing_to_approve(
    set_mandate, quote_for, order_from_quote, resolve_approval, approvals_for,
    fake_razorpay,
):
    set_mandate(
        autonomous_limit=3000.0,
        absolute_transaction_limit=6000.0,
        monthly_cap=50000.0,
    )
    quote = quote_for(quantity=70)  # 7000.00, over the absolute ceiling
    order = order_from_quote(quote["quote_id"])
    txn_id = order["transaction"]["id"]

    assert order["transaction"]["status"] == TransactionStatus.BLOCKED.value
    assert approvals_for(txn_id) == []

    body = resolve_approval(txn_id, "approve", expect=404)
    assert body["detail"]["reason"] == RejectionReason.APPROVAL_NOT_FOUND.value
    assert fake_razorpay.orders_created == []


def test_a_limit_lowered_while_waiting_blocks_the_approved_purchase(
    pending, resolve_approval, set_mandate, fake_razorpay
):
    """The mandate itself is narrowed while the request waits. Human
    approval cannot reach past the new absolute ceiling."""
    _, order = pending()
    set_mandate(absolute_transaction_limit=1000.0)

    body = resolve_approval(order["transaction"]["id"], "approve")

    assert body["order_created"] is False
    assert body["policy"]["decision"] == PolicyDecision.BLOCK.value
    assert body["transaction"]["status"] == TransactionStatus.BLOCKED.value
    assert fake_razorpay.orders_created == []


# --- 24. PAID still has exactly one writer ---------------------------------


def test_only_payment_verification_sets_paid_after_approval(
    pending, resolve_approval, client, db_session, seed
):
    """The approved path funnels into the Package 2 verification path and
    does not shortcut it."""
    _, order = pending()

    approved = resolve_approval(order["transaction"]["id"], "approve")
    assert approved["transaction"]["status"] == TransactionStatus.ORDER_CREATED.value

    order_id = approved["transaction"]["razorpay_order_id"]
    payment_id = "pay_APPROVED1"
    response = client.post(
        "/app/v1/payment/verify",
        json={
            "transaction_id": approved["transaction"]["id"],
            "razorpay_order_id": order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": sign(order_id, payment_id),
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["transaction"]["status"] == TransactionStatus.PAID.value
    assert body["transaction"]["razorpay_payment_id"] == payment_id
    # Balance and stock move here and nowhere else.
    db_session.expire_all()
    assert db_session.get(buyer_db, seed["buyer_id"]).balance == 46000.0
    assert db_session.get(product_db, seed["widget_id"]).quantity == 60
    assert db_session.get(quote_db, order["transaction"]["quote_id"]).status is (
        QuoteStatus.CONSUMED
    )


def test_a_blocked_approval_can_never_be_settled(
    pending, resolve_approval, client, db_session, seed
):
    _, order = pending()
    db_session.get(product_db, seed["widget_id"]).cost = 150.0
    db_session.commit()

    body = resolve_approval(order["transaction"]["id"], "approve")
    assert body["order_created"] is False

    response = client.post(
        "/app/v1/payment/verify",
        json={
            "transaction_id": order["transaction"]["id"],
            "razorpay_order_id": "order_FAKE123",
            "razorpay_payment_id": "pay_SHOULD_NOT_SETTLE",
            "razorpay_signature": sign("order_FAKE123", "pay_SHOULD_NOT_SETTLE"),
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == (
        RejectionReason.TRANSACTION_NOT_AWAITING_PAYMENT.value
    )


# --- 25. no real provider traffic ------------------------------------------


def test_the_suite_cannot_reach_the_network():
    """`no_network` is autouse, so this holds for every test in the suite,
    including the approval paths above."""
    with pytest.raises(AssertionError):
        requests.get("https://api.razorpay.com/v1/orders")


def test_approval_paths_use_the_faked_provider_only(
    pending, resolve_approval, fake_razorpay
):
    _, order = pending()
    resolve_approval(order["transaction"]["id"], "approve")

    assert len(fake_razorpay.orders_created) == 1
    assert fake_razorpay.orders_created[0]["amount"] == 400000
    assert fake_razorpay.orders_created[0]["currency"] == "INR"
