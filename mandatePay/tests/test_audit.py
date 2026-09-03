"""The audit trail: what gets recorded, in what order, and what never does.

Two claims are under test throughout this file:

  1. The trail tells the whole story -- quote, verdict, human, provider,
     payment -- in an order a reader can trust.
  2. The trail is an observer. It holds no signature and no secret, it
     cannot be edited after the fact, and switching it off would change no
     financial outcome.
"""

import pytest
from sqlalchemy import text

from backend.audit import SENSITIVE_KEY_FRAGMENTS, record_event, scrub
from backend.databases.audit_db import AuditImmutableError, audit_event_db
from backend.databases.quote_db import quote_db
from backend.enums.AuditEventType import AuditEventType
from backend.enums.QuoteStatus import QuoteStatus
from backend.enums.TransactionStatus import TransactionStatus

from tests.conftest import (
    ABSOLUTE_LIMIT,
    AUTONOMOUS_LIMIT,
    BALANCE,
    TEST_KEY_SECRET,
    sign,
)


# ---------------------------------------------------------------------------
# Quote and policy
# ---------------------------------------------------------------------------


def test_quote_creation_emits_quote_created(quote_for, event_types, audit_events):
    quote = quote_for(quantity=2)

    types = event_types(quote_id=quote["quote_id"])
    assert AuditEventType.QUOTE_CREATED in types

    created = audit_events(
        quote_id=quote["quote_id"], event_type=AuditEventType.QUOTE_CREATED
    )[0]
    # The quoted numbers are in the record, exactly as quoted.
    assert created.details["quoted_total"] == str(quote["quoted_total"])
    assert created.details["quantity"] == 2
    assert created.details["product_id"] == quote["product"]["product_id"]


def test_policy_allow_is_audited(quote_for, audit_events):
    # 2 x 100 = 200, comfortably under the 1000 autonomous limit.
    quote = quote_for(quantity=2)

    allowed = audit_events(
        quote_id=quote["quote_id"], event_type=AuditEventType.POLICY_ALLOWED
    )
    assert len(allowed) == 1
    assert allowed[0].details["decision"] == "allow"
    assert allowed[0].details["violation_codes"] == []


def test_policy_require_approval_is_audited(quote_for, audit_events):
    # 6 x 250 = 1500: above the autonomous limit, under the absolute one.
    quote = quote_for(quantity=6, product="gadget_id")

    events = audit_events(
        quote_id=quote["quote_id"],
        event_type=AuditEventType.POLICY_APPROVAL_REQUIRED,
    )
    assert len(events) == 1
    assert events[0].details["approval_codes"] == ["autonomous_limit"]


def test_policy_block_is_audited(quote_for, set_mandate, audit_events):
    set_mandate(absolute_transaction_limit=300.0)
    quote = quote_for(quantity=6, product="gadget_id")

    blocked = audit_events(
        quote_id=quote["quote_id"], event_type=AuditEventType.POLICY_BLOCKED
    )
    assert len(blocked) == 1
    assert "absolute_transaction_limit" in blocked[0].details["violation_codes"]


def test_blocked_order_records_transaction_blocked_and_no_provider_call(
    quote_for, order_from_quote, set_mandate, fake_razorpay, audit_events
):
    set_mandate(absolute_transaction_limit=300.0)
    quote = quote_for(quantity=6, product="gadget_id")
    order = order_from_quote(quote["quote_id"])

    assert order["transaction"]["status"] == TransactionStatus.BLOCKED.value
    assert fake_razorpay.orders_created == []

    blocked = audit_events(
        transaction_id=order["transaction"]["id"],
        event_type=AuditEventType.TRANSACTION_BLOCKED,
    )
    assert len(blocked) == 1
    assert blocked[0].details["provider_called"] is False


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------


def test_approval_created_is_audited(
    quote_for, order_from_quote, fake_razorpay, audit_events
):
    quote = quote_for(quantity=6, product="gadget_id")
    order = order_from_quote(quote["quote_id"])

    created = audit_events(
        transaction_id=order["transaction"]["id"],
        event_type=AuditEventType.APPROVAL_CREATED,
    )
    assert len(created) == 1
    assert str(created[0].approval_id) == order["approval"]["id"]


@pytest.mark.parametrize(
    "action, expected",
    [
        ("approve", AuditEventType.APPROVAL_APPROVED),
        ("reject", AuditEventType.APPROVAL_REJECTED),
    ],
)
def test_approval_resolution_is_audited(
    quote_for,
    order_from_quote,
    resolve_approval,
    fake_razorpay,
    audit_events,
    action,
    expected,
):
    quote = quote_for(quantity=6, product="gadget_id")
    order = order_from_quote(quote["quote_id"])
    resolve_approval(order["transaction"]["id"], action=action)

    events = audit_events(
        transaction_id=order["transaction"]["id"], event_type=expected
    )
    assert len(events) == 1
    assert events[0].details["reviewer"] == "demo-approver"


def test_rejection_records_transaction_cancelled(
    quote_for, order_from_quote, resolve_approval, fake_razorpay, audit_events
):
    quote = quote_for(quantity=6, product="gadget_id")
    order = order_from_quote(quote["quote_id"])
    resolve_approval(order["transaction"]["id"], action="reject")

    cancelled = audit_events(
        transaction_id=order["transaction"]["id"],
        event_type=AuditEventType.TRANSACTION_CANCELLED,
    )
    assert len(cancelled) == 1
    assert cancelled[0].details["quote_status"] == QuoteStatus.CANCELLED.value
    assert cancelled[0].details["provider_called"] is False


def test_approval_revalidation_failure_is_audited_without_retracting_approval(
    quote_for,
    order_from_quote,
    resolve_approval,
    set_mandate,
    db_session,
    fake_razorpay,
    audit_events,
    event_types,
    seed,
):
    quote = quote_for(quantity=6, product="gadget_id")
    order = order_from_quote(quote["quote_id"])

    # The world moves while the request waits: this merchant is no longer
    # permitted at all.
    set_mandate(allowed_merchants=[seed["other_merchant_id"]])

    resolution = resolve_approval(order["transaction"]["id"], action="approve")
    assert resolution["order_created"] is False

    types = event_types(transaction_id=order["transaction"]["id"])
    # Both facts survive: the human said yes, AND a hard rule then said no.
    assert AuditEventType.APPROVAL_APPROVED in types
    assert AuditEventType.APPROVAL_REVALIDATION_FAILED in types
    # And the approval event still precedes the failure, because that is
    # the order it happened in.
    assert types.index(AuditEventType.APPROVAL_APPROVED) < types.index(
        AuditEventType.APPROVAL_REVALIDATION_FAILED
    )

    failure = audit_events(
        transaction_id=order["transaction"]["id"],
        event_type=AuditEventType.APPROVAL_REVALIDATION_FAILED,
    )[0]
    assert failure.details["provider_called"] is False
    assert failure.details["order_created"] is False
    assert "merchant_allowed" in failure.details["violation_codes"]
    assert fake_razorpay.orders_created == []


# ---------------------------------------------------------------------------
# Order creation and payment
# ---------------------------------------------------------------------------


def test_order_created_event(quote_for, order_from_quote, fake_razorpay, audit_events):
    quote = quote_for(quantity=2)
    order = order_from_quote(quote["quote_id"])

    created = audit_events(
        transaction_id=order["transaction"]["id"],
        event_type=AuditEventType.ORDER_CREATED,
    )
    assert len(created) == 1
    assert created[0].details["razorpay_order_id"] == "order_FAKE123"
    # An order is not a payment, and the record says so in as many words.
    assert created[0].details["paid"] is False


def test_order_creation_failure_event(
    quote_for, order_from_quote, broken_razorpay, audit_events, event_types, db_session
):
    from backend.databases.transaction_db import transaction_db

    quote = quote_for(quantity=2)
    order_from_quote(quote["quote_id"], expect=502)

    txn = db_session.query(transaction_db).one()
    types = event_types(transaction_id=txn.id)

    assert AuditEventType.ORDER_CREATION_ATTEMPTED in types
    assert AuditEventType.ORDER_CREATION_FAILED in types
    assert AuditEventType.ORDER_CREATED not in types

    failure = audit_events(
        transaction_id=txn.id, event_type=AuditEventType.ORDER_CREATION_FAILED
    )[0]
    assert failure.details["razorpay_order_id"] is None
    assert failure.details["balance_or_stock_mutated"] is False
    assert failure.details["status_after"] == TransactionStatus.FAILED.value


def test_payment_verified_event(paid_flow, audit_events):
    _, order, _ = paid_flow()
    txn_id = order["transaction"]["id"]

    verified = audit_events(
        transaction_id=txn_id, event_type=AuditEventType.PAYMENT_VERIFIED
    )
    assert len(verified) == 1
    assert verified[0].details["verified_server_side"] is True
    assert verified[0].details["replay"] is False

    paid = audit_events(
        transaction_id=txn_id, event_type=AuditEventType.TRANSACTION_PAID
    )
    assert len(paid) == 1
    assert paid[0].details["status_from"] == TransactionStatus.ORDER_CREATED.value
    assert paid[0].details["status_to"] == TransactionStatus.PAID.value


def test_payment_verification_failure_event(
    quote_for, order_from_quote, fake_razorpay, client, audit_events, event_types
):
    quote = quote_for(quantity=2)
    order = order_from_quote(quote["quote_id"])
    txn_id = order["transaction"]["id"]

    client.post(
        "/app/v1/payment/verify",
        json={
            "transaction_id": txn_id,
            "razorpay_order_id": order["transaction"]["razorpay_order_id"],
            "razorpay_payment_id": "pay_FAKE123",
            "razorpay_signature": "not-a-real-signature",
        },
    )

    types = event_types(transaction_id=txn_id)
    assert AuditEventType.PAYMENT_VERIFICATION_ATTEMPTED in types
    assert AuditEventType.PAYMENT_VERIFICATION_FAILED in types
    assert AuditEventType.TRANSACTION_PAID not in types

    failure = audit_events(
        transaction_id=txn_id, event_type=AuditEventType.PAYMENT_VERIFICATION_FAILED
    )[0]
    assert failure.details["reason"] == "SIGNATURE_VERIFICATION_FAILED"
    assert failure.details["paid"] is False
    assert failure.details["balance_or_stock_mutated"] is False


# ---------------------------------------------------------------------------
# Ordering, endpoint, and the demo stories
# ---------------------------------------------------------------------------


def test_events_are_chronologically_ordered(paid_flow, client):
    _, order, _ = paid_flow()
    body = client.get(f"/app/v1/transactions/{order['transaction']['id']}/audit").json()

    sequences = [event["sequence"] for event in body["events"]]
    timestamps = [event["timestamp"] for event in body["events"]]

    assert sequences == sorted(sequences)
    assert timestamps == sorted(timestamps)


def test_audit_endpoint_tells_the_successful_lifecycle(paid_flow, client):
    quote, order, _ = paid_flow()
    body = client.get(f"/app/v1/transactions/{order['transaction']['id']}/audit").json()

    assert body["transaction_id"] == order["transaction"]["id"]
    assert body["status"] == TransactionStatus.PAID.value
    assert body["quote_id"] == quote["quote_id"]
    assert body["event_count"] == len(body["events"])

    story = [event["event_type"] for event in body["events"]]
    # The quote is the first chapter even though it predates the
    # transaction row.
    assert story[0] == AuditEventType.QUOTE_CREATED.value
    for expected in (
        AuditEventType.POLICY_ALLOWED,
        AuditEventType.ORDER_CREATION_ATTEMPTED,
        AuditEventType.ORDER_CREATED,
        AuditEventType.PAYMENT_VERIFICATION_ATTEMPTED,
        AuditEventType.PAYMENT_VERIFIED,
        AuditEventType.TRANSACTION_PAID,
    ):
        assert expected.value in story
    assert story[-1] == AuditEventType.TRANSACTION_PAID.value


def test_audit_endpoint_tells_the_blocked_lifecycle(
    quote_for, order_from_quote, set_mandate, fake_razorpay, client
):
    set_mandate(absolute_transaction_limit=300.0)
    quote = quote_for(quantity=6, product="gadget_id")
    order = order_from_quote(quote["quote_id"])

    body = client.get(f"/app/v1/transactions/{order['transaction']['id']}/audit").json()
    story = [event["event_type"] for event in body["events"]]

    assert story[0] == AuditEventType.QUOTE_CREATED.value
    assert AuditEventType.POLICY_BLOCKED.value in story
    assert story[-1] == AuditEventType.TRANSACTION_BLOCKED.value
    # No Razorpay chapter at all.
    assert AuditEventType.ORDER_CREATION_ATTEMPTED.value not in story
    assert AuditEventType.ORDER_CREATED.value not in story
    assert fake_razorpay.orders_created == []


def test_audit_endpoint_tells_the_provider_failure_lifecycle(
    quote_for, order_from_quote, broken_razorpay, client, db_session
):
    from backend.databases.transaction_db import transaction_db

    quote = quote_for(quantity=2)
    order_from_quote(quote["quote_id"], expect=502)
    txn = db_session.query(transaction_db).one()

    body = client.get(f"/app/v1/transactions/{txn.id}/audit").json()
    story = [event["event_type"] for event in body["events"]]

    assert AuditEventType.ORDER_CREATION_ATTEMPTED.value in story
    assert AuditEventType.ORDER_CREATION_FAILED.value in story
    assert AuditEventType.ORDER_CREATED.value not in story
    assert AuditEventType.TRANSACTION_PAID.value not in story
    assert body["status"] == TransactionStatus.FAILED.value


def test_audit_endpoint_is_typed(paid_flow, client):
    _, order, _ = paid_flow()
    body = client.get(f"/app/v1/transactions/{order['transaction']['id']}/audit").json()

    event = body["events"][0]
    assert set(event) == {
        "sequence",
        "event_type",
        "timestamp",
        "buyer_id",
        "quote_id",
        "transaction_id",
        "approval_id",
        "details",
    }
    assert isinstance(event["details"], dict)


def test_audit_endpoint_404_for_unknown_transaction(client, seed):
    response = client.get(
        "/app/v1/transactions/00000000-0000-0000-0000-000000000000/audit"
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# What is never stored
# ---------------------------------------------------------------------------


def test_no_signature_or_secret_is_ever_stored(paid_flow, db_session, client):
    """The one non-negotiable content rule.

    Checked against the raw table text rather than the parsed details, so
    a value smuggled in under an unexpected key would still be caught.
    """
    order_id = "order_FAKE123"
    payment_id = "pay_FAKE123"
    signature = sign(order_id, payment_id)

    paid_flow(payment_id=payment_id)

    raw = " ".join(
        str(row[0]) for row in db_session.execute(text("SELECT details FROM audit_events"))
    )
    assert signature not in raw
    assert TEST_KEY_SECRET not in raw
    # The payment and order ids ARE legitimately stored -- they are
    # references, not credentials.
    assert payment_id in raw


def test_failed_verification_stores_no_signature(
    quote_for, order_from_quote, fake_razorpay, client, db_session
):
    quote = quote_for(quantity=2)
    order = order_from_quote(quote["quote_id"])
    forged = "0" * 64

    client.post(
        "/app/v1/payment/verify",
        json={
            "transaction_id": order["transaction"]["id"],
            "razorpay_order_id": order["transaction"]["razorpay_order_id"],
            "razorpay_payment_id": "pay_FAKE123",
            "razorpay_signature": forged,
        },
    )

    raw = " ".join(
        str(row[0]) for row in db_session.execute(text("SELECT details FROM audit_events"))
    )
    assert forged not in raw


def test_scrub_drops_sensitive_keys_at_every_depth():
    cleaned = scrub(
        {
            "amount": 100,
            "razorpay_signature": "deadbeef",
            "nested": {"api_key": "sk-live-xyz", "product_id": "abc"},
        }
    )

    assert cleaned["amount"] == 100
    assert "razorpay_signature" not in cleaned
    assert cleaned["_redacted"] == ["razorpay_signature"]
    assert "api_key" not in cleaned["nested"]
    assert cleaned["nested"]["product_id"] == "abc"


def test_every_sensitive_fragment_is_actually_dropped():
    payload = {f"some_{fragment}_field": "x" for fragment in SENSITIVE_KEY_FRAGMENTS}
    cleaned = scrub(payload)
    assert set(cleaned) == {"_redacted"}


# ---------------------------------------------------------------------------
# The trail as an observer
# ---------------------------------------------------------------------------


def test_audit_events_cannot_be_modified(db_session, seed):
    record_event(db_session, AuditEventType.QUOTE_CREATED, buyer_id=seed["buyer_id"])
    event = db_session.query(audit_event_db).one()

    event.event_type = AuditEventType.TRANSACTION_PAID
    with pytest.raises(AuditImmutableError):
        db_session.commit()
    db_session.rollback()


def test_audit_events_cannot_be_deleted(db_session, seed):
    record_event(db_session, AuditEventType.QUOTE_CREATED, buyer_id=seed["buyer_id"])
    event = db_session.query(audit_event_db).one()

    db_session.delete(event)
    with pytest.raises(AuditImmutableError):
        db_session.commit()
    db_session.rollback()


def test_a_broken_audit_writer_does_not_change_the_financial_outcome(
    monkeypatch, quote_for, order_from_quote, fake_razorpay, client, db_session
):
    """The load-bearing claim: audit observes, it does not authorize.

    Every `record_event` call is made to throw, and the purchase must
    still price, authorize, order and settle exactly as before -- with no
    audit rows to show for it.
    """
    import backend.audit as audit_module

    def _explode(*args, **kwargs):
        raise RuntimeError("audit table is on fire")

    monkeypatch.setattr(audit_module, "audit_event_db", _explode)

    quote = quote_for(quantity=2)
    order = order_from_quote(quote["quote_id"])
    order_id = order["transaction"]["razorpay_order_id"]

    response = client.post(
        "/app/v1/payment/verify",
        json={
            "transaction_id": order["transaction"]["id"],
            "razorpay_order_id": order_id,
            "razorpay_payment_id": "pay_FAKE123",
            "razorpay_signature": sign(order_id, "pay_FAKE123"),
        },
    )

    assert response.status_code == 200
    assert response.json()["transaction"]["status"] == TransactionStatus.PAID.value
    # ...and nothing was recorded, which is exactly the point.
    assert db_session.query(audit_event_db).count() == 0


def test_audit_read_endpoint_mutates_nothing(paid_flow, client, db_session):
    from backend.databases.buyer_db import buyer_db

    _, order, _ = paid_flow()
    txn_id = order["transaction"]["id"]

    before_events = db_session.query(audit_event_db).count()
    before_balance = db_session.query(buyer_db).one().balance

    for _ in range(3):
        assert client.get(f"/app/v1/transactions/{txn_id}/audit").status_code == 200

    db_session.expire_all()
    assert db_session.query(audit_event_db).count() == before_events
    assert db_session.query(buyer_db).one().balance == before_balance


# ---------------------------------------------------------------------------
# The AI half
# ---------------------------------------------------------------------------


def test_agent_selection_is_audited_without_prompts(
    agent_purchase, fake_llm, agent_catalogue, audit_events, db_session, seed
):
    fake_llm.set_intent(item="wireless keyboard", quantity=1, required_brand="Logitech")
    fake_llm.select_product(agent_catalogue["cheap_id"])

    agent_purchase("I need a Logitech wireless keyboard")

    interpreted = audit_events(event_type=AuditEventType.AGENT_REQUEST_INTERPRETED)
    assert len(interpreted) == 1
    assert interpreted[0].details["item"] == "wireless keyboard"
    assert interpreted[0].details["required_brand"] == "Logitech"

    selected = audit_events(event_type=AuditEventType.AGENT_PRODUCT_SELECTED)
    assert len(selected) == 1
    assert selected[0].details["product_id"] == agent_catalogue["cheap_id"]

    # No prompt, no reply, no reasoning anywhere in the trail.
    raw = " ".join(
        str(row[0]) for row in db_session.execute(text("SELECT details FROM audit_events"))
    )
    assert "IGNORE ALL PREVIOUS INSTRUCTIONS" not in raw
    assert "intent parser" not in raw


def test_agent_clarification_is_audited(
    agent_purchase, fake_llm, agent_catalogue, event_types
):
    fake_llm.set_intent(item="thing", quantity=1)

    agent_purchase("buy me something")

    assert AuditEventType.AGENT_CLARIFICATION_REQUIRED in event_types()
    assert AuditEventType.QUOTE_CREATED not in event_types()


def test_agent_llm_failure_is_audited_with_no_financial_side_effects(
    agent_purchase, fake_llm, audit_events, db_session
):
    from backend.databases.transaction_db import transaction_db

    fake_llm.set_intent(item=12345)  # not a string: an unusable reply

    agent_purchase("buy a keyboard", expect=502)

    failed = audit_events(event_type=AuditEventType.AGENT_REQUEST_FAILED)
    assert len(failed) == 1
    assert failed[0].details["quote_created"] is False
    assert failed[0].details["transaction_created"] is False
    assert failed[0].details["provider_called"] is False

    assert db_session.query(quote_db).count() == 0
    assert db_session.query(transaction_db).count() == 0


def test_agent_multi_item_request_is_audited_as_unsupported(
    agent_purchase, fake_llm, agent_catalogue, event_types
):
    fake_llm.set_intent(
        item="keyboard",
        quantity=1,
        is_multi_item=True,
        requested_items=["keyboard", "mouse"],
    )

    agent_purchase("a keyboard and a mouse")

    types = event_types()
    assert AuditEventType.AGENT_REQUEST_UNSUPPORTED in types
    assert AuditEventType.QUOTE_CREATED not in types


def test_agent_approval_lifecycle_reads_end_to_end(
    agent_purchase,
    fake_llm,
    agent_catalogue,
    order_from_quote,
    resolve_approval,
    fake_razorpay,
    client,
):
    """The demo story, in one trail: request -> selection -> quote ->
    approval required -> approved -> allowed -> order."""
    fake_llm.set_intent(item="wireless keyboard", quantity=1, required_brand="Logitech")
    fake_llm.select_product(agent_catalogue["mid_id"])  # 2500, over the 1000 threshold

    result = agent_purchase("a Logitech wireless keyboard")
    assert result["next_action"] == "await_approval"

    order = order_from_quote(result["quote"]["quote_id"])
    resolution = resolve_approval(order["transaction"]["id"], action="approve")
    assert resolution["order_created"] is True

    body = client.get(f"/app/v1/transactions/{order['transaction']['id']}/audit").json()
    story = [event["event_type"] for event in body["events"]]

    for expected in (
        AuditEventType.AGENT_PRODUCT_SELECTED,
        AuditEventType.QUOTE_CREATED,
        AuditEventType.POLICY_APPROVAL_REQUIRED,
        AuditEventType.APPROVAL_CREATED,
        AuditEventType.APPROVAL_APPROVED,
        AuditEventType.POLICY_ALLOWED,
        AuditEventType.ORDER_CREATED,
    ):
        assert expected.value in story, f"{expected.value} missing from {story}"

    assert story.index(AuditEventType.APPROVAL_APPROVED.value) < story.index(
        AuditEventType.ORDER_CREATED.value
    )
    assert AuditEventType.TRANSACTION_PAID.value not in story


# ---------------------------------------------------------------------------
# Correlation: the AI chapter reaches the right trail, and only that trail
# ---------------------------------------------------------------------------


def test_agent_product_selected_appears_in_the_transaction_trail(
    agent_purchase,
    fake_llm,
    agent_catalogue,
    order_from_quote,
    fake_razorpay,
    client,
    seed,
):
    """The regression this file's ordering test originally exposed.

    The selection happens before any quote or transaction exists, so it
    has nothing to be filed under at the moment it is decided. It is
    recorded against the quote the instant that quote exists, which is the
    deterministic relation the transaction already uses to find it.
    """
    fake_llm.set_intent(item="wireless keyboard", quantity=1, required_brand="Logitech")
    fake_llm.select_product(agent_catalogue["cheap_id"])  # 900, under the threshold

    result = agent_purchase("a Logitech wireless keyboard")
    quote_id = result["quote"]["quote_id"]

    order = order_from_quote(quote_id)
    body = client.get(f"/app/v1/transactions/{order['transaction']['id']}/audit").json()
    story = [event["event_type"] for event in body["events"]]

    assert AuditEventType.AGENT_PRODUCT_SELECTED.value in story
    # It is the first chapter, and it precedes the quote it caused.
    assert story[0] == AuditEventType.AGENT_PRODUCT_SELECTED.value
    assert story.index(AuditEventType.AGENT_PRODUCT_SELECTED.value) < story.index(
        AuditEventType.QUOTE_CREATED.value
    )

    selected = next(
        event
        for event in body["events"]
        if event["event_type"] == AuditEventType.AGENT_PRODUCT_SELECTED.value
    )
    # Correlated through the quote, not through an invented trace id.
    assert selected["quote_id"] == quote_id
    assert selected["buyer_id"] == seed["buyer_id"]
    assert selected["details"]["product_id"] == agent_catalogue["cheap_id"]


def test_another_buyers_events_do_not_leak_into_this_trail(
    client, db_session, seed, fake_razorpay, quote_for, order_from_quote
):
    """A second buyer, quoting and ordering the same product, shares
    nothing with the first buyer's trail."""
    from backend.databases.buyer_db import buyer_db
    from backend.databases.mandate_db import mandate_db

    mine = quote_for(quantity=2)
    my_order = order_from_quote(mine["quote_id"])

    other = buyer_db(name="Other Buyer", balance=BALANCE)
    db_session.add(other)
    db_session.flush()
    db_session.add(
        mandate_db(
            buyer_id=other.id,
            autonomous_limit=AUTONOMOUS_LIMIT,
            absolute_transaction_limit=ABSOLUTE_LIMIT,
            monthly_cap=5000.0,
        )
    )
    db_session.commit()

    theirs = client.post(
        "/app/v1/quote",
        json={
            "buyer_id": str(other.id),
            "product_id": seed["widget_id"],
            "quantity": 2,
        },
    ).json()
    their_order = order_from_quote(theirs["quote_id"])

    body = client.get(f"/app/v1/transactions/{my_order['transaction']['id']}/audit").json()

    assert body["events"], "the trail should not be empty"
    for event in body["events"]:
        assert event["buyer_id"] == seed["buyer_id"]
        assert event["quote_id"] in (mine["quote_id"], None)
        assert event["transaction_id"] in (my_order["transaction"]["id"], None)
        assert event["transaction_id"] != their_order["transaction"]["id"]


def test_a_sibling_transaction_on_the_same_quote_does_not_leak(
    quote_for, order_from_quote, fake_razorpay, client
):
    """One quote can produce two transactions -- a duplicate attempt is
    recorded and BLOCKED. Each trail must tell only its own story.

    The shared pre-transaction chapters (the quote) legitimately appear in
    both. Everything that names a transaction appears in exactly one.
    """
    quote = quote_for(quantity=2)
    first = order_from_quote(quote["quote_id"])
    second = order_from_quote(quote["quote_id"])

    first_id = first["transaction"]["id"]
    second_id = second["transaction"]["id"]
    assert first_id != second_id
    # The second attempt is caught as a duplicate of the live first one.
    assert second["transaction"]["status"] == TransactionStatus.BLOCKED.value

    first_body = client.get(f"/app/v1/transactions/{first_id}/audit").json()
    second_body = client.get(f"/app/v1/transactions/{second_id}/audit").json()

    for event in first_body["events"]:
        assert event["transaction_id"] in (first_id, None)
    for event in second_body["events"]:
        assert event["transaction_id"] in (second_id, None)

    # The first trail has the provider chapter; the second has the block.
    first_story = [event["event_type"] for event in first_body["events"]]
    second_story = [event["event_type"] for event in second_body["events"]]
    assert AuditEventType.ORDER_CREATED.value in first_story
    assert AuditEventType.ORDER_CREATED.value not in second_story
    assert AuditEventType.TRANSACTION_BLOCKED.value in second_story
    assert AuditEventType.TRANSACTION_BLOCKED.value not in first_story

    # Both legitimately share the quote that produced them.
    assert AuditEventType.QUOTE_CREATED.value in first_story
    assert AuditEventType.QUOTE_CREATED.value in second_story
