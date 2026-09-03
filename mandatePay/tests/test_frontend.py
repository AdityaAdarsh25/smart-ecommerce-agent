"""Package 6 -- the integrated demo front-end.

The front-end is a presentation layer over the same endpoints Packages
1-5 built, so what is worth testing here is not what the page looks like.
It is that adding a browser to the system did not open a door:

  * the page and its assets are served, and carry no secret
  * the browser is given the PUBLIC Razorpay key and nothing else
  * the safety evidence shown in the UI is read from the committed
    evaluation results rather than restated in source
  * every UI state the agent response can produce still leads to exactly
    the payment path the deterministic layer permits -- and no other

There are no visual snapshots here on purpose. A screenshot test would
fail on a font and pass on a bypass.
"""

import json
import re
from pathlib import Path

import pytest

from backend.commerce import demo_routes
from backend.databases.transaction_db import transaction_db
from backend.enums.TransactionStatus import TransactionStatus
from tests.conftest import TEST_KEY_ID, TEST_KEY_SECRET, sign

STATIC_DIR = Path("backend/static")
STATIC_FILES = ["index.html", "app.css", "app.js"]


# ---------------------------------------------------------------------------
# 1. The page itself
# ---------------------------------------------------------------------------


def test_root_serves_the_demo_ui(client):
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "MandatePay" in response.text


def test_the_api_docs_are_still_reachable(client):
    """Adding a UI at / must not displace the API reference."""
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200


@pytest.mark.parametrize("name", STATIC_FILES)
def test_static_assets_are_served(client, name):
    response = client.get(f"/static/{name}")
    assert response.status_code == 200
    assert response.content


def test_the_page_references_only_assets_that_exist(client):
    """A broken asset link is a demo that opens to an unstyled page."""
    html = client.get("/").text
    for path in re.findall(r'(?:href|src)="(/static/[^"]+)"', html):
        assert client.get(path).status_code == 200, path


# ---------------------------------------------------------------------------
# 2. Demo identity
# ---------------------------------------------------------------------------


def test_demo_buyer_endpoint_returns_the_seeded_identity(client, seed):
    response = client.get("/app/v1/buyers")

    assert response.status_code == 200
    buyers = response.json()
    assert len(buyers) == 1

    buyer = buyers[0]
    assert buyer["buyer_id"] == seed["buyer_id"]
    assert buyer["name"] == "Test Buyer"
    assert buyer["mandate"]["autonomous_limit"] == "1000.0"
    assert buyer["mandate"]["monthly_cap"] == "5000.0"


def test_demo_buyer_endpoint_exposes_no_secret(client, seed, monkeypatch):
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "secret-value-must-not-leak")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-value-must-not-leak")

    body = client.get("/app/v1/buyers").text

    assert "secret-value-must-not-leak" not in body
    assert "openai-value-must-not-leak" not in body
    for fragment in ("secret", "api_key", "password", "signature"):
        assert fragment not in body.lower()


def test_the_demo_buyer_is_a_seeded_identity_not_an_authenticated_one(client):
    """There is no auth in v1, and the schema must not imply there is."""
    schema = client.get("/openapi.json").json()["components"]["schemas"]
    assert set(schema["DemoBuyerResponse"]["properties"]) == {
        "buyer_id",
        "name",
        "balance",
        "mandate",
    }
    assert "securitySchemes" not in client.get("/openapi.json").json().get(
        "components", {}
    )


# ---------------------------------------------------------------------------
# 3. Public config -- the one value that crosses to the browser
# ---------------------------------------------------------------------------


def test_public_config_exposes_the_public_key_id_only(client, monkeypatch):
    monkeypatch.setenv("RAZORPAY_KEY_ID", TEST_KEY_ID)
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", TEST_KEY_SECRET)

    body = client.get("/app/v1/config").json()

    assert body["razorpay_key_id"] == TEST_KEY_ID
    assert body["razorpay_configured"] is True
    assert set(body) == {"razorpay_key_id", "razorpay_configured", "agent_configured"}


def test_public_config_never_exposes_a_secret(client, monkeypatch):
    """The value of every server-side credential, checked by value."""
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_public_id")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "razorpay-secret-must-not-leak")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key-must-not-leak")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-test")

    raw = client.get("/app/v1/config").text

    assert "razorpay-secret-must-not-leak" not in raw
    assert "openai-key-must-not-leak" not in raw
    assert "rzp_test_public_id" in raw


def test_the_config_schema_has_nowhere_to_put_a_secret(client):
    """Structural, not incidental: no field exists that could carry one."""
    schema = client.get("/openapi.json").json()["components"]["schemas"]
    properties = set(schema["PublicConfigResponse"]["properties"])
    assert properties == {"razorpay_key_id", "razorpay_configured", "agent_configured"}
    assert not any(
        fragment in name.lower()
        for name in properties
        for fragment in ("secret", "api_key", "token", "password")
    )


def test_missing_razorpay_public_config_is_reported_not_crashed(client, monkeypatch):
    monkeypatch.delenv("RAZORPAY_KEY_ID", raising=False)
    monkeypatch.delenv("RAZORPAY_KEY_SECRET", raising=False)

    body = client.get("/app/v1/config").json()

    assert body["razorpay_key_id"] is None
    assert body["razorpay_configured"] is False


def test_missing_openai_config_is_reported_not_crashed(client, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)

    assert client.get("/app/v1/config").json()["agent_configured"] is False


def test_an_unconfigured_agent_refuses_cleanly_from_the_ui_path(
    client, seed, db_session
):
    """What the front-end actually gets when OPENAI_API_KEY is absent: a
    typed 503, not a stack trace, and nothing priced."""
    from backend.agents.llm_client import LLMConfigurationError, get_llm
    from backend.databases.quote_db import quote_db
    from backend.main import app

    def _unconfigured(**kwargs):
        raise LLMConfigurationError("OPENAI_API_KEY is not set.")

    app.dependency_overrides[get_llm] = lambda: _unconfigured

    response = client.post(
        "/app/v1/agent/purchase",
        json={"buyer_id": seed["buyer_id"], "message": "a widget"},
    )

    assert response.status_code == 503
    assert response.json()["detail"]["reason"] == "AGENT_NOT_CONFIGURED"
    assert response.json()["detail"]["message"]
    assert db_session.query(quote_db).count() == 0


# ---------------------------------------------------------------------------
# 4. Evaluation evidence -- read from disk, never restated
# ---------------------------------------------------------------------------


def test_evaluation_summary_reports_the_committed_results(client):
    recorded = json.loads(demo_routes.RESULTS_JSON.read_text(encoding="utf-8"))
    body = client.get("/app/v1/evaluation/summary").json()

    assert body["scenarios"] == recorded["totals"]["scenarios"]
    assert body["passed"] == recorded["totals"]["passed"]
    assert body["failed"] == recorded["totals"]["failed"]
    assert body["pass_rate"] == recorded["totals"]["pass_rate"]
    assert (
        body["unsafe_financial_bypass_count"]
        == recorded["unsafe_financial_bypass"]["count"]
    )
    assert (
        body["unsafe_financial_bypass_rate"]
        == recorded["unsafe_financial_bypass"]["rate_over_all_scenarios"]
    )
    assert body["live_provider_calls"] == recorded["network"]["live_calls_made"]


def test_the_displayed_evidence_is_the_accepted_package_5_result(client):
    """The demo claim, checked against the artefact behind it."""
    body = client.get("/app/v1/evaluation/summary").json()

    assert body["scenarios"] == 122
    assert body["passed"] == 122
    assert body["failed"] == 0
    assert body["pass_rate"] == 100.0
    assert body["unsafe_financial_bypass_count"] == 0
    assert body["unsafe_financial_bypass_rate"] == 0.0
    assert sum(group["total"] for group in body["groups"]) == 122


def test_missing_evaluation_results_are_reported_not_faked(client, monkeypatch):
    absent = demo_routes.RESULTS_JSON.parent / "results-that-do-not-exist.json"
    monkeypatch.setattr(demo_routes, "RESULTS_JSON", absent)

    response = client.get("/app/v1/evaluation/summary")

    assert response.status_code == 503
    assert "run_evaluation" in response.json()["detail"]


# ---------------------------------------------------------------------------
# 5. The UI states an agent response can produce
# ---------------------------------------------------------------------------


def _agent(client, seed, message="a wireless keyboard"):
    response = client.post(
        "/app/v1/agent/purchase",
        json={"buyer_id": seed["buyer_id"], "message": message},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_allow_maps_to_a_payment_enabled_ui_state(
    client, seed, fake_llm, agent_catalogue, fake_razorpay
):
    """ALLOW is the only shape from which the UI renders a pay button, and
    it still has to go through create-order to get one."""
    fake_llm.set_intent(item="wireless keyboard", quantity=1, max_budget=3000.0)
    fake_llm.select_product(agent_catalogue["cheap_id"])

    body = _agent(client, seed)

    assert body["next_action"] == "create_order"
    assert body["policy"]["decision"] == "allow"
    assert body["quote"] is not None
    # Nothing payable exists yet: the agent response carries no transaction
    # and no provider order for the browser to act on.
    assert "transaction" not in body
    assert fake_razorpay.orders_created == []

    order = client.post(
        "/app/v1/create-order", json={"quote_id": body["quote"]["quote_id"]}
    ).json()
    assert order["transaction"]["status"] == TransactionStatus.ORDER_CREATED.value
    assert order["razorpay"]["razorpay_order_id"]
    assert order["razorpay"]["amount_in_paise"] == 90000


def test_require_approval_creates_no_payment_prematurely(
    client, seed, fake_llm, agent_catalogue, fake_razorpay, set_mandate
):
    set_mandate(autonomous_limit=500.0)
    fake_llm.set_intent(item="wireless keyboard", quantity=1, max_budget=3000.0)
    fake_llm.select_product(agent_catalogue["cheap_id"])

    body = _agent(client, seed)

    assert body["next_action"] == "await_approval"
    assert body["policy"]["decision"] == "require_approval"

    order = client.post(
        "/app/v1/create-order", json={"quote_id": body["quote"]["quote_id"]}
    ).json()

    # The state the UI renders as "Human approval required": an approval to
    # resolve, no provider order, and nothing for a pay button to use.
    assert order["transaction"]["status"] == TransactionStatus.AWAITING_APPROVAL.value
    assert order["razorpay"] is None
    assert order["transaction"]["razorpay_order_id"] is None
    assert order["approval"]["status"] == "pending"
    assert fake_razorpay.orders_created == []


def test_approval_is_what_reaches_razorpay_not_the_ui(
    client, seed, fake_llm, agent_catalogue, fake_razorpay, set_mandate, resolve_approval
):
    set_mandate(autonomous_limit=500.0)
    fake_llm.set_intent(item="wireless keyboard", quantity=1, max_budget=3000.0)
    fake_llm.select_product(agent_catalogue["cheap_id"])

    body = _agent(client, seed)
    order = client.post(
        "/app/v1/create-order", json={"quote_id": body["quote"]["quote_id"]}
    ).json()
    assert fake_razorpay.orders_created == []

    resolution = resolve_approval(order["transaction"]["id"], "approve")

    assert resolution["order_created"] is True
    assert resolution["razorpay"]["razorpay_order_id"]
    assert len(fake_razorpay.orders_created) == 1


def test_rejection_leaves_no_payment_path(
    client, seed, fake_llm, agent_catalogue, fake_razorpay, set_mandate, resolve_approval
):
    set_mandate(autonomous_limit=500.0)
    fake_llm.set_intent(item="wireless keyboard", quantity=1, max_budget=3000.0)
    fake_llm.select_product(agent_catalogue["cheap_id"])

    body = _agent(client, seed)
    order = client.post(
        "/app/v1/create-order", json={"quote_id": body["quote"]["quote_id"]}
    ).json()

    resolution = resolve_approval(order["transaction"]["id"], "reject")

    assert resolution["order_created"] is False
    assert resolution["razorpay"] is None
    assert resolution["transaction"]["status"] == TransactionStatus.CANCELLED.value
    assert fake_razorpay.orders_created == []


def test_block_exposes_no_execution_path(
    client, seed, fake_llm, agent_catalogue, fake_razorpay, set_mandate
):
    set_mandate(absolute_transaction_limit=1000.0)
    fake_llm.set_intent(item="wireless keyboard", quantity=1, max_budget=3000.0)
    fake_llm.select_product(agent_catalogue["mid_id"])

    body = _agent(client, seed)

    assert body["policy"]["decision"] == "block"
    # STOP is what the UI reads to render a block with no buttons. There is
    # no NextAction value reachable from BLOCK that permits payment.
    assert body["next_action"] == "stop"
    assert body["policy"]["hard_violations"]

    # And the endpoint behind a hypothetical button refuses anyway.
    order = client.post(
        "/app/v1/create-order", json={"quote_id": body["quote"]["quote_id"]}
    ).json()
    assert order["transaction"]["status"] == TransactionStatus.BLOCKED.value
    assert order["razorpay"] is None
    assert fake_razorpay.orders_created == []


def test_clarification_has_no_payment_path(client, seed, fake_llm, agent_catalogue):
    fake_llm.set_intent(item="wireless keyboard", quantity=1, max_budget=3000.0)
    fake_llm.ask_to_clarify("Membrane or mechanical?")

    body = _agent(client, seed)

    assert body["next_action"] == "clarify"
    assert body["clarification_question"] == "Membrane or mechanical?"
    # No quote id means the UI has nothing to send to create-order.
    assert body["quote"] is None
    assert body["policy"] is None


def test_multi_item_unsupported_has_no_payment_path(
    client, seed, fake_llm, agent_catalogue
):
    fake_llm.set_intent(
        item="keyboard and mouse",
        quantity=1,
        is_multi_item=True,
        requested_items=["keyboard", "mouse"],
    )

    body = _agent(client, seed)

    assert body["selection_action"] == "UNSUPPORTED"
    assert body["next_action"] == "stop"
    assert body["quote"] is None
    assert body["policy"] is None
    assert body["selection"] is None


# ---------------------------------------------------------------------------
# 6. What the browser is structurally unable to assert
# ---------------------------------------------------------------------------


def test_the_client_cannot_supply_an_amount_to_create_order(
    client, seed, quote_for, fake_razorpay
):
    """Extra fields are ignored, and the order is created for the quoted
    amount regardless of what the browser sent."""
    quote = quote_for(quantity=2)  # 2 x 100.00

    order = client.post(
        "/app/v1/create-order",
        json={
            "quote_id": quote["quote_id"],
            "amount": 1,
            "quoted_total": 1,
            "amount_in_paise": 100,
            "price": 0.01,
        },
    )

    assert order.status_code == 200
    assert order.json()["razorpay"]["amount_in_paise"] == 20000
    assert order.json()["transaction"]["amount"] == "200.0"
    assert fake_razorpay.orders_created[0]["amount"] == 20000


def test_create_order_has_nowhere_to_put_a_price(client):
    schema = client.get("/openapi.json").json()["components"]["schemas"]
    assert set(schema["CreateOrderRequest"]["properties"]) == {"quote_id"}


def test_the_client_cannot_assert_a_policy_verdict(client, seed, fake_llm, agent_catalogue, set_mandate):
    """A browser that says ALLOW gets whatever the engine decides."""
    set_mandate(absolute_transaction_limit=1000.0)
    fake_llm.set_intent(item="wireless keyboard", quantity=1, max_budget=3000.0)
    fake_llm.select_product(agent_catalogue["mid_id"])

    response = client.post(
        "/app/v1/agent/purchase",
        json={
            "buyer_id": seed["buyer_id"],
            "message": "a wireless keyboard",
            "policy": "ALLOW",
            "decision": "ALLOW",
            "next_action": "create_order",
        },
    )

    assert response.status_code == 200
    assert response.json()["policy"]["decision"] == "block"
    assert response.json()["next_action"] == "stop"


def test_only_verification_produces_paid(client, seed, quote_for, fake_razorpay):
    """The UI renders PAID off this response and nothing else."""
    quote = quote_for(quantity=2)
    order = client.post(
        "/app/v1/create-order", json={"quote_id": quote["quote_id"]}
    ).json()
    order_id = order["transaction"]["razorpay_order_id"]

    assert order["transaction"]["status"] == TransactionStatus.ORDER_CREATED.value

    verification = client.post(
        "/app/v1/payment/verify",
        json={
            "transaction_id": order["transaction"]["id"],
            "razorpay_order_id": order_id,
            "razorpay_payment_id": "pay_UI123",
            "razorpay_signature": sign(order_id, "pay_UI123"),
        },
    ).json()

    assert verification["verified"] is True
    assert verification["transaction"]["status"] == TransactionStatus.PAID.value


def test_an_invalid_verification_cannot_produce_paid(
    client, seed, quote_for, fake_razorpay, db_session
):
    """A forged Checkout callback -- the exact thing a UI must not be
    allowed to turn into a green PAID badge."""
    quote = quote_for(quantity=2)
    order = client.post(
        "/app/v1/create-order", json={"quote_id": quote["quote_id"]}
    ).json()
    transaction_id = order["transaction"]["id"]

    response = client.post(
        "/app/v1/payment/verify",
        json={
            "transaction_id": transaction_id,
            "razorpay_order_id": order["transaction"]["razorpay_order_id"],
            "razorpay_payment_id": "pay_FORGED",
            "razorpay_signature": "not-a-real-signature",
        },
    )

    assert response.status_code == 400
    txn = db_session.get(transaction_db, transaction_id)
    assert txn.status is not TransactionStatus.PAID


def test_a_refused_verification_reports_the_resulting_transaction_status(
    client, seed, quote_for, fake_razorpay, db_session
):
    """The UI must not be left rendering ORDER_CREATED after a failure.

    The refusal body carries the status the transaction actually ended up
    in, read server-side after the failure was applied, so the front-end
    can show FAILED without inventing it.
    """
    from backend.databases.buyer_db import buyer_db
    from backend.databases.product_db import product_db
    from backend.databases.quote_db import quote_db
    from backend.enums.QuoteStatus import QuoteStatus

    quote = quote_for(quantity=2)
    order = client.post(
        "/app/v1/create-order", json={"quote_id": quote["quote_id"]}
    ).json()
    transaction_id = order["transaction"]["id"]

    # 1. the transaction starts ORDER_CREATED
    assert order["transaction"]["status"] == TransactionStatus.ORDER_CREATED.value

    balance_before = db_session.get(buyer_db, seed["buyer_id"]).balance
    stock_before = db_session.get(product_db, seed["widget_id"]).quantity

    # 2. an invalid verification occurs
    response = client.post(
        "/app/v1/payment/verify",
        json={
            "transaction_id": transaction_id,
            "razorpay_order_id": order["transaction"]["razorpay_order_id"],
            "razorpay_payment_id": "pay_FORGED",
            "razorpay_signature": "not-a-real-signature",
        },
    )
    detail = response.json()["detail"]

    assert response.status_code == 400
    assert detail["reason"] == "SIGNATURE_VERIFICATION_FAILED"

    # 3. the backend transaction is FAILED
    db_session.expire_all()
    txn = db_session.get(transaction_db, transaction_id)
    assert txn.status is TransactionStatus.FAILED

    # 4. and the refusal says so, so the UI shows FAILED and not the stale
    #    ORDER_CREATED it was holding.
    assert detail["transaction_status"] == TransactionStatus.FAILED.value
    assert detail["transaction_status"] != TransactionStatus.ORDER_CREATED.value
    assert detail["transaction_status"] != TransactionStatus.PAID.value

    # 5. nothing moved.
    assert db_session.get(buyer_db, seed["buyer_id"]).balance == balance_before
    assert db_session.get(product_db, seed["widget_id"]).quantity == stock_before
    assert (
        db_session.get(quote_db, quote["quote_id"]).status is QuoteStatus.ACTIVE
    ), "a quote must not be consumed by a payment that never settled"


def test_a_refusal_that_leaves_the_transaction_payable_says_so(
    client, seed, quote_for, fake_razorpay, db_session
):
    """The status reported is read, not assumed to be FAILED.

    An order-id mismatch does not belong to this transaction, so the
    transaction is untouched and still ORDER_CREATED -- and the refusal
    reports exactly that.
    """
    quote = quote_for(quantity=2)
    order = client.post(
        "/app/v1/create-order", json={"quote_id": quote["quote_id"]}
    ).json()

    response = client.post(
        "/app/v1/payment/verify",
        json={
            "transaction_id": order["transaction"]["id"],
            "razorpay_order_id": "order_SOMEONE_ELSES",
            "razorpay_payment_id": "pay_X",
            "razorpay_signature": sign("order_SOMEONE_ELSES", "pay_X"),
        },
    )
    detail = response.json()["detail"]

    assert response.status_code == 409
    assert detail["reason"] == "ORDER_ID_MISMATCH"
    assert detail["transaction_status"] == TransactionStatus.ORDER_CREATED.value
    txn = db_session.get(transaction_db, order["transaction"]["id"])
    assert txn.status is TransactionStatus.ORDER_CREATED


def test_a_refusal_can_never_report_paid(client, seed, quote_for, fake_razorpay):
    """`transaction_status` reports state; it cannot assert a settlement.

    Every refusal exit is exercised here that a browser can reach, and
    none of them may come back claiming the transaction is paid.
    """
    quote = quote_for(quantity=2)
    order = client.post(
        "/app/v1/create-order", json={"quote_id": quote["quote_id"]}
    ).json()
    transaction_id = order["transaction"]["id"]
    order_id = order["transaction"]["razorpay_order_id"]

    refusals = [
        # unknown transaction
        {
            "transaction_id": "00000000-0000-0000-0000-000000000001",
            "razorpay_order_id": order_id,
            "razorpay_payment_id": "pay_A",
            "razorpay_signature": "x",
        },
        # order id belongs to someone else
        {
            "transaction_id": transaction_id,
            "razorpay_order_id": "order_OTHER",
            "razorpay_payment_id": "pay_B",
            "razorpay_signature": "x",
        },
        # forged signature
        {
            "transaction_id": transaction_id,
            "razorpay_order_id": order_id,
            "razorpay_payment_id": "pay_C",
            "razorpay_signature": "x",
        },
        # and now the transaction is FAILED, so it is no longer payable
        {
            "transaction_id": transaction_id,
            "razorpay_order_id": order_id,
            "razorpay_payment_id": "pay_D",
            "razorpay_signature": sign(order_id, "pay_D"),
        },
    ]

    for body in refusals:
        response = client.post("/app/v1/payment/verify", json=body)
        assert response.status_code >= 400, body
        status = response.json()["detail"].get("transaction_status")
        assert status != TransactionStatus.PAID.value, body


def test_verification_ignores_a_client_supplied_status(
    client, seed, quote_for, fake_razorpay, db_session
):
    quote = quote_for(quantity=2)
    order = client.post(
        "/app/v1/create-order", json={"quote_id": quote["quote_id"]}
    ).json()

    response = client.post(
        "/app/v1/payment/verify",
        json={
            "transaction_id": order["transaction"]["id"],
            "razorpay_order_id": order["transaction"]["razorpay_order_id"],
            "razorpay_payment_id": "pay_LIE",
            "razorpay_signature": "wrong",
            "status": "paid",
            "verified": True,
            "amount": 1,
        },
    )

    assert response.status_code == 400
    txn = db_session.get(transaction_db, order["transaction"]["id"])
    assert txn.status is TransactionStatus.FAILED


def test_payment_verify_has_nowhere_to_put_an_amount(client):
    schema = client.get("/openapi.json").json()["components"]["schemas"]
    assert set(schema["PaymentVerificationRequest"]["properties"]) == {
        "transaction_id",
        "razorpay_order_id",
        "razorpay_payment_id",
        "razorpay_signature",
    }


# ---------------------------------------------------------------------------
# 7. Audit, as the UI reads it
# ---------------------------------------------------------------------------


def test_the_audit_endpoint_tells_the_whole_ui_story(
    client, seed, fake_llm, agent_catalogue, fake_razorpay
):
    fake_llm.set_intent(item="wireless keyboard", quantity=1, max_budget=3000.0)
    fake_llm.select_product(agent_catalogue["cheap_id"])

    body = _agent(client, seed)
    order = client.post(
        "/app/v1/create-order", json={"quote_id": body["quote"]["quote_id"]}
    ).json()
    order_id = order["transaction"]["razorpay_order_id"]
    client.post(
        "/app/v1/payment/verify",
        json={
            "transaction_id": order["transaction"]["id"],
            "razorpay_order_id": order_id,
            "razorpay_payment_id": "pay_AUDIT",
            "razorpay_signature": sign(order_id, "pay_AUDIT"),
        },
    )

    audit = client.get(
        f"/app/v1/transactions/{order['transaction']['id']}/audit"
    ).json()

    types = [event["event_type"] for event in audit["events"]]
    for expected in (
        "AGENT_PRODUCT_SELECTED",
        "QUOTE_CREATED",
        "POLICY_ALLOWED",
        "ORDER_CREATED",
        "PAYMENT_VERIFIED",
        "TRANSACTION_PAID",
    ):
        assert expected in types, types
    assert audit["event_count"] == len(audit["events"])
    assert audit["status"] == TransactionStatus.PAID.value


def test_the_audit_a_blocked_transaction_shows_the_ui_why(
    client, seed, fake_llm, agent_catalogue, fake_razorpay, set_mandate
):
    set_mandate(absolute_transaction_limit=1000.0)
    fake_llm.set_intent(item="wireless keyboard", quantity=1, max_budget=3000.0)
    fake_llm.select_product(agent_catalogue["mid_id"])

    body = _agent(client, seed)
    order = client.post(
        "/app/v1/create-order", json={"quote_id": body["quote"]["quote_id"]}
    ).json()

    audit = client.get(
        f"/app/v1/transactions/{order['transaction']['id']}/audit"
    ).json()
    types = [event["event_type"] for event in audit["events"]]

    assert "POLICY_BLOCKED" in types
    assert "TRANSACTION_BLOCKED" in types
    assert "ORDER_CREATED" not in types
    assert "PAYMENT_VERIFIED" not in types


def test_audit_details_carry_no_secret(client, seed, quote_for, fake_razorpay):
    """The UI renders these details verbatim in an expandable block."""
    quote = quote_for(quantity=2)
    order = client.post(
        "/app/v1/create-order", json={"quote_id": quote["quote_id"]}
    ).json()
    order_id = order["transaction"]["razorpay_order_id"]
    client.post(
        "/app/v1/payment/verify",
        json={
            "transaction_id": order["transaction"]["id"],
            "razorpay_order_id": order_id,
            "razorpay_payment_id": "pay_AUDIT2",
            "razorpay_signature": sign(order_id, "pay_AUDIT2"),
        },
    )

    raw = client.get(
        f"/app/v1/transactions/{order['transaction']['id']}/audit"
    ).text

    assert TEST_KEY_SECRET not in raw
    assert sign(order_id, "pay_AUDIT2") not in raw


# ---------------------------------------------------------------------------
# 8. Nothing secret is shipped to the browser
# ---------------------------------------------------------------------------


SECRET_NAMES = (
    "RAZORPAY_KEY_SECRET",
    "OPENAI_API_KEY",
    "razorpay_key_secret",
    "openai_api_key",
    "key_secret",
)


@pytest.mark.parametrize("name", STATIC_FILES)
def test_no_secret_name_appears_in_the_static_source(name):
    source = (STATIC_DIR / name).read_text(encoding="utf-8")
    for secret in SECRET_NAMES:
        assert secret not in source, f"{secret} appears in {name}"


@pytest.mark.parametrize("name", STATIC_FILES)
def test_no_credential_shaped_literal_appears_in_the_static_source(name):
    """Nothing that looks like a live key or a signature is hardcoded."""
    source = (STATIC_DIR / name).read_text(encoding="utf-8")

    assert not re.search(r"rzp_(test|live)_[A-Za-z0-9]{6,}", source)
    assert not re.search(r"sk-[A-Za-z0-9_\-]{16,}", source)
    # A Razorpay signature is a 64-char hex digest. No literal one exists.
    assert not re.search(r"\b[0-9a-f]{64}\b", source)


def test_no_env_value_is_embedded_in_the_static_source():
    """Checked by VALUE against the developer's own .env, if present."""
    env_file = Path(".env")
    if not env_file.is_file():
        pytest.skip("No local .env to check against.")

    values = []
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if "=" not in line or line.strip().startswith("#"):
            continue
        name, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        # The public key id is allowed in a page; a secret never is.
        if len(value) >= 8 and name.strip() != "RAZORPAY_KEY_ID":
            values.append(value)

    for name in STATIC_FILES:
        source = (STATIC_DIR / name).read_text(encoding="utf-8")
        for value in values:
            assert value not in source, f"an .env value appears in {name}"


def test_the_served_page_contains_no_injected_configuration(client, monkeypatch):
    """The page is a static file: configuration is fetched, never templated
    in, so a secret cannot be rendered into it."""
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "must-not-be-templated")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-templated-either")

    response = client.get("/")

    assert "must-not-be-templated" not in response.text
    # Byte-for-byte: the response must BE the file on disk, whatever newline
    # representation the checkout uses. Comparing decoded text would compare
    # the served CRLF against read_text()'s universal-newline LF.
    assert response.content == (STATIC_DIR / "index.html").read_bytes()


def test_the_demo_module_never_reads_the_razorpay_secret_value():
    """Presence check only -- the secret's value has no path to a response."""
    source = Path("backend/commerce/demo_routes.py").read_text(encoding="utf-8")
    assert 'os.getenv(ENV_RAZORPAY_KEY_SECRET)' not in source
    assert source.count("ENV_RAZORPAY_KEY_SECRET") == 2  # the constant, and _env()
