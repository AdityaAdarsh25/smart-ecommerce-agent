"""Payment-state truth: ALLOW is not PAID, and neither is a Razorpay order.

Razorpay is replaced with the recording fake from conftest. No network
access occurs -- the `no_network` fixture makes that structural.
"""

from backend.commerce import product_routes
from backend.databases.buyer_db import buyer_db
from backend.databases.product_db import product_db
from backend.databases.transaction_db import transaction_db
from backend.enums.PolicyDecision import PolicyDecision
from backend.enums.TransactionStatus import TransactionStatus


def create_order(client, seed, quantity, product="widget_id", expect=200):
    """The real caller flow: quote first, then order from that quote.

    Order creation takes a quote id and nothing else, so the price it uses
    is the one the server froze -- never one the caller chose.
    """
    quote = client.post(
        "/app/v1/quote",
        json={
            "buyer_id": seed["buyer_id"],
            "product_id": seed[product],
            "quantity": quantity,
        },
    )
    assert quote.status_code == 200, quote.text

    response = client.post(
        "/app/v1/create-order", json={"quote_id": quote.json()["quote_id"]}
    )
    assert response.status_code == expect, response.text
    return response.json()


# Fixture mandate: autonomous limit 1000, monthly cap 5000, balance 50000.
# Widget 100, Gadget 250, Machine 2000.
ALLOW_CASE = (2, "widget_id")  # 200   -> ALLOW
APPROVAL_CASE = (5, "gadget_id")  # 1250  -> REQUIRE_APPROVAL
BLOCK_CASE = (3, "machine_id")  # 6000  -> BLOCK (over monthly cap)


def test_allow_creates_order_but_never_marks_paid(client, seed, fake_razorpay):
    """ALLOW -> AUTHORIZED -> ORDER_CREATED. A Razorpay order is an intent
    to collect, not a collection."""
    body = create_order(client, seed, *ALLOW_CASE)

    assert body["policy"]["decision"] == PolicyDecision.ALLOW.value
    assert body["transaction"]["status"] == TransactionStatus.ORDER_CREATED.value
    assert body["transaction"]["status"] != TransactionStatus.PAID.value
    assert body["transaction"]["razorpay_order_id"] == "order_FAKE123"
    assert len(fake_razorpay.orders_created) == 1


def test_block_never_contacts_the_payment_provider(client, seed, fake_razorpay):
    body = create_order(client, seed, *BLOCK_CASE)

    assert body["policy"]["decision"] == PolicyDecision.BLOCK.value
    assert body["transaction"]["status"] == TransactionStatus.BLOCKED.value
    assert body["transaction"]["razorpay_order_id"] is None
    assert fake_razorpay.orders_created == []


def test_require_approval_never_contacts_the_payment_provider(
    client, seed, fake_razorpay
):
    body = create_order(client, seed, *APPROVAL_CASE)

    assert body["policy"]["decision"] == PolicyDecision.REQUIRE_APPROVAL.value
    assert body["transaction"]["status"] == TransactionStatus.AWAITING_APPROVAL.value
    assert body["transaction"]["razorpay_order_id"] is None
    assert fake_razorpay.orders_created == []


def test_provider_failure_marks_failed_not_paid(client, seed, db_session, monkeypatch):
    class ExplodingRazorpay:
        def Client(self, auth=None):  # noqa: N802
            raise RuntimeError("provider unreachable")

    monkeypatch.setattr(product_routes, "razorpay", ExplodingRazorpay())

    before_balance = db_session.get(buyer_db, seed["buyer_id"]).balance
    before_stock = db_session.get(product_db, seed["widget_id"]).quantity

    create_order(client, seed, *ALLOW_CASE, expect=502)

    db_session.expire_all()
    txn = db_session.query(transaction_db).one()
    assert txn.status is TransactionStatus.FAILED
    assert txn.razorpay_order_id is None
    # 15. A provider failure must not move money or stock.
    assert db_session.get(buyer_db, seed["buyer_id"]).balance == before_balance
    assert db_session.get(product_db, seed["widget_id"]).quantity == before_stock


def test_paid_is_never_written_by_create_order(client, seed, fake_razorpay, db_session):
    """Sweep every decision path. PAID must be unreachable in Package 1 --
    only payment verification may set it, and that arrives in Package 2."""
    for quantity, product in (ALLOW_CASE, APPROVAL_CASE, BLOCK_CASE):
        create_order(client, seed, quantity, product)

    db_session.expire_all()
    statuses = {t.status for t in db_session.query(transaction_db).all()}

    assert TransactionStatus.PAID not in statuses
    assert statuses == {
        TransactionStatus.ORDER_CREATED,
        TransactionStatus.AWAITING_APPROVAL,
        TransactionStatus.BLOCKED,
    }


def test_balance_and_stock_are_not_consumed_before_payment(
    client, seed, fake_razorpay, db_session
):
    """Nothing may be decremented until a payment is verified in Package 2."""
    before_balance = db_session.get(buyer_db, seed["buyer_id"]).balance
    before_stock = db_session.get(product_db, seed["widget_id"]).quantity

    create_order(client, seed, *ALLOW_CASE)

    db_session.expire_all()
    assert db_session.get(buyer_db, seed["buyer_id"]).balance == before_balance
    assert db_session.get(product_db, seed["widget_id"]).quantity == before_stock


def test_quote_reports_policy_and_never_a_transaction_status(client, seed):
    """Asking whether you may buy something is not a financial event."""
    response = client.post(
        "/app/v1/quote",
        json={
            "buyer_id": seed["buyer_id"],
            "product_id": seed["widget_id"],
            "quantity": 2,
        },
    )
    assert response.status_code == 200
    body = response.json()

    assert body["policy"]["decision"] == PolicyDecision.ALLOW.value
    assert "status" not in body
    assert body["policy"]["decision"] not in {s.value for s in TransactionStatus}
    # Nothing was persisted.
    assert body.get("transaction") is None


def test_insufficient_stock_is_a_conflict_not_a_policy_decision(client, seed):
    response = client.post(
        "/app/v1/quote",
        json={
            "buyer_id": seed["buyer_id"],
            "product_id": seed["widget_id"],
            "quantity": 999,
        },
    )
    assert response.status_code == 409
