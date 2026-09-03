"""Duplicate protection, exercised through the API against a real DB."""

from backend.enums.PolicyDecision import PolicyDecision
from backend.enums.TransactionStatus import TransactionStatus

WIDGET_COST = 100.0


def quote(client, seed, quantity=2, product="widget_id"):
    response = client.post(
        "/app/v1/quote",
        json={
            "buyer_id": seed["buyer_id"],
            "product_id": seed[product],
            "quantity": quantity,
        },
    )
    assert response.status_code == 200
    return response.json()["policy"]


def is_duplicate_block(policy):
    return policy["decision"] == PolicyDecision.BLOCK.value and any(
        "identical purchase" in v for v in policy["hard_violations"]
    )


# --- 11. recent AWAITING_APPROVAL duplicate -> BLOCK -----------------------


def test_recent_awaiting_approval_is_duplicate(client, seed, add_transaction):
    """This is what stops an unbounded pile of identical approval requests."""
    add_transaction(
        buyer_id=seed["buyer_id"],
        product_id=seed["widget_id"],
        quantity=2,
        amount=2 * WIDGET_COST,
        status=TransactionStatus.AWAITING_APPROVAL,
        minutes_ago=1,
    )
    assert is_duplicate_block(quote(client, seed, quantity=2))


# --- 12. recent PAID duplicate -> BLOCK ------------------------------------


def test_recent_paid_is_duplicate(client, seed, add_transaction):
    add_transaction(
        buyer_id=seed["buyer_id"],
        product_id=seed["widget_id"],
        quantity=2,
        amount=2 * WIDGET_COST,
        status=TransactionStatus.PAID,
        minutes_ago=1,
    )
    assert is_duplicate_block(quote(client, seed, quantity=2))


def test_recent_authorized_and_order_created_are_duplicates(
    client, seed, add_transaction
):
    for status in (TransactionStatus.AUTHORIZED, TransactionStatus.ORDER_CREATED):
        add_transaction(
            buyer_id=seed["buyer_id"],
            product_id=seed["widget_id"],
            quantity=3,
            amount=3 * WIDGET_COST,
            status=status,
            minutes_ago=1,
        )
        assert is_duplicate_block(quote(client, seed, quantity=3)), status


# --- 13. dead attempts must not poison a legitimate retry ------------------


def test_blocked_failed_cancelled_do_not_prevent_retry(client, seed, add_transaction):
    """The original bug: a BLOCKED row matched its own duplicate window, so
    the blocked attempt blocked its own retry for five minutes."""
    for status in (
        TransactionStatus.BLOCKED,
        TransactionStatus.FAILED,
        TransactionStatus.CANCELLED,
    ):
        add_transaction(
            buyer_id=seed["buyer_id"],
            product_id=seed["widget_id"],
            quantity=2,
            amount=2 * WIDGET_COST,
            status=status,
            minutes_ago=1,
        )

    policy = quote(client, seed, quantity=2)
    assert policy["decision"] == PolicyDecision.ALLOW.value
    assert policy["hard_violations"] == []


# --- Window and key boundaries --------------------------------------------


def test_duplicate_outside_the_window_does_not_block(client, seed, add_transaction):
    add_transaction(
        buyer_id=seed["buyer_id"],
        product_id=seed["widget_id"],
        quantity=2,
        amount=2 * WIDGET_COST,
        status=TransactionStatus.AWAITING_APPROVAL,
        minutes_ago=30,
    )
    assert quote(client, seed, quantity=2)["decision"] == PolicyDecision.ALLOW.value


def test_different_quantity_is_not_a_duplicate(client, seed, add_transaction):
    add_transaction(
        buyer_id=seed["buyer_id"],
        product_id=seed["widget_id"],
        quantity=2,
        amount=2 * WIDGET_COST,
        status=TransactionStatus.AWAITING_APPROVAL,
        minutes_ago=1,
    )
    assert quote(client, seed, quantity=5)["decision"] == PolicyDecision.ALLOW.value


def test_different_product_is_not_a_duplicate(client, seed, add_transaction):
    add_transaction(
        buyer_id=seed["buyer_id"],
        product_id=seed["widget_id"],
        quantity=2,
        amount=2 * WIDGET_COST,
        status=TransactionStatus.AWAITING_APPROVAL,
        minutes_ago=1,
    )
    policy = quote(client, seed, quantity=2, product="gadget_id")
    assert policy["decision"] == PolicyDecision.ALLOW.value


# --- Monthly spend counts verified payments only --------------------------


def test_only_paid_transactions_consume_the_monthly_cap(
    client, seed, add_transaction
):
    """An authorized-but-unpaid attempt has not spent anything yet."""
    # 4900 of the 5000 cap, but only ORDER_CREATED -- not money yet.
    add_transaction(
        buyer_id=seed["buyer_id"],
        product_id=seed["machine_id"],
        quantity=1,
        amount=4900.0,
        status=TransactionStatus.ORDER_CREATED,
        minutes_ago=10,
    )
    assert quote(client, seed, quantity=2)["decision"] == PolicyDecision.ALLOW.value

    # The same amount, verified as PAID, does consume the cap.
    add_transaction(
        buyer_id=seed["buyer_id"],
        product_id=seed["machine_id"],
        quantity=1,
        amount=4900.0,
        status=TransactionStatus.PAID,
        minutes_ago=10,
    )
    policy = quote(client, seed, quantity=2)
    assert policy["decision"] == PolicyDecision.BLOCK.value
    assert any("monthly spend" in v for v in policy["hard_violations"])
