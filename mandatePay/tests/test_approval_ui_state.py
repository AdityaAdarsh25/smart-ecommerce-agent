"""What the page shows after an action the server carried out and refused.

The bug this module exists for was found in a browser, not in a response
body. A human approved a 4000.00 purchase, re-validation passed, the
Razorpay order creation then failed, and the backend correctly wrote
FAILED -- while the page went on showing AWAITING HUMAN APPROVAL, live
Approve and Reject buttons, and "Waiting on a human decision", beside the
provider error. Two contradictory answers to "did a human decide?" and
"was this paid?" on one screen.

So there are two layers of coverage here:

  * the refusal itself must carry where the transaction actually ended
    up, the way payment verification's refusals already do; and
  * the real app.js, run against that real refusal, must render it.

The second half runs `backend/static/app.js` in node against a stub DOM
(`tests/frontend_dom_harness.mjs`). Every response it replays was
produced by the real FastAPI app a few lines earlier, so what is asserted
is the page rendering server output -- never a hand-written fixture, and
never a screenshot.

Two facts are kept apart throughout, because the incident collapsed them:
the HUMAN DECISION (approved) and the PAYMENT STATE (failed). Approval
did not fail. The payment did.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from backend.databases.approval_db import approval_db
from backend.databases.transaction_db import transaction_db
from backend.enums.ApprovalStatus import ApprovalStatus
from backend.enums.TransactionStatus import TransactionStatus

HARNESS = Path("tests/frontend_dom_harness.mjs")
APP_JS = Path("backend/static/app.js")
INDEX_HTML = Path("backend/static/index.html")

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(
    NODE is None, reason="node is not installed; the DOM harness cannot run"
)

PROVIDER_MESSAGE = "Payment provider order creation failed. Nothing was charged."
REQUEST = "a wireless keyboard"


# ---------------------------------------------------------------------------
# Recording the real server, exactly as the page will ask for it
# ---------------------------------------------------------------------------


class Recorder:
    """Real responses, keyed by the request the page actually makes.

    Nothing is composed here. Each entry is whatever the FastAPI app
    returned, so a change in a response shape reaches the rendered page
    through this file rather than being papered over by a fixture.
    """

    def __init__(self, client):
        self.client = client
        self.responses = {}

    def get(self, path):
        return self._record("GET", path, self.client.get(path))

    def post(self, path, body):
        return self._record("POST", path, self.client.post(path, json=body))

    def _record(self, method, path, response):
        self.responses[f"{method} {path}"] = {
            "status": response.status_code,
            "body": response.json(),
        }
        return response


def _boot(rec):
    """The three reads the page performs when it loads."""
    rec.get("/app/v1/buyers")
    rec.get("/app/v1/config")
    rec.get("/app/v1/evaluation/summary")


@pytest.fixture()
def flow(client, seed, fake_llm, agent_catalogue):
    """Drive the page's own sequence as far as a chosen product takes it.

    `product` picks the verdict: the mid keyboard (2500.00) is above the
    1000.00 autonomous limit and lands in REQUIRE_APPROVAL; the cheap one
    (900.00) is inside it and lands in ALLOW.
    """

    def _flow(product="mid_id"):
        fake_llm.set_intent(item="wireless keyboard", quantity=1, max_budget=3000.0)
        fake_llm.select_product(agent_catalogue[product])

        rec = Recorder(client)
        _boot(rec)
        agent = rec.post(
            "/app/v1/agent/purchase",
            {"buyer_id": seed["buyer_id"], "message": REQUEST},
        ).json()
        return rec, agent

    return _flow


@pytest.fixture()
def awaiting(flow):
    """A transaction sitting in AWAITING_APPROVAL, with the page's reads
    recorded up to that point."""

    def _awaiting():
        rec, agent = flow("mid_id")
        assert agent["next_action"] == "await_approval"

        order = rec.post(
            "/app/v1/create-order", {"quote_id": agent["quote"]["quote_id"]}
        ).json()
        assert (
            order["transaction"]["status"] == TransactionStatus.AWAITING_APPROVAL.value
        )
        rec.get(f"/app/v1/transactions/{order['transaction']['id']}/audit")
        return rec, order["transaction"]["id"]

    return _awaiting


@pytest.fixture()
def render():
    """Run the real app.js against the recorded responses."""

    def _render(rec, steps):
        payload = {
            "app_js": str(APP_JS),
            "index_html": str(INDEX_HTML),
            "responses": rec.responses,
            "steps": steps,
            "request": REQUEST,
        }
        result = subprocess.run(
            [NODE, str(HARNESS)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    return _render


# ---------------------------------------------------------------------------
# 1. The refusal itself
# ---------------------------------------------------------------------------


def test_a_provider_failure_after_approval_refuses_with_the_final_state(
    awaiting, client, broken_razorpay, db_session
):
    """The 502 says where the transaction ended up, not just that it broke.

    Without this the page has nothing authoritative to render and is left
    holding AWAITING_APPROVAL.
    """
    rec, transaction_id = awaiting()

    response = client.post(
        f"/app/v1/transactions/{transaction_id}/approve",
        json={"reviewer": "demo-approver"},
    )
    detail = response.json()["detail"]

    assert response.status_code == 502
    assert detail["reason"] == "PROVIDER_ERROR"
    assert detail["message"] == PROVIDER_MESSAGE
    assert detail["transaction_status"] == TransactionStatus.FAILED.value
    assert detail["transaction_status"] != TransactionStatus.AWAITING_APPROVAL.value
    assert detail["transaction_status"] != TransactionStatus.PAID.value
    assert detail["transaction_id"] == transaction_id

    # And the row agrees with what the refusal reported.
    db_session.expire_all()
    txn = db_session.get(transaction_db, transaction_id)
    assert txn.status is TransactionStatus.FAILED
    assert txn.razorpay_order_id is None
    assert txn.razorpay_payment_id is None


def test_the_human_approval_still_stands_after_the_provider_failed(
    awaiting, client, broken_razorpay, db_session, event_types
):
    """The approval did not fail. The payment did.

    Reporting the approval as anything other than APPROVED would rewrite
    what a human actually decided.
    """
    rec, transaction_id = awaiting()

    client.post(
        f"/app/v1/transactions/{transaction_id}/approve",
        json={"reviewer": "demo-approver"},
    )

    db_session.expire_all()
    approval = db_session.query(approval_db).one()
    assert approval.status is ApprovalStatus.APPROVED
    assert approval.reviewer == "demo-approver"

    types = [event.value for event in event_types(transaction_id=transaction_id)]
    for expected in (
        "APPROVAL_APPROVED",
        "POLICY_ALLOWED",
        "ORDER_CREATION_ATTEMPTED",
        "ORDER_CREATION_FAILED",
        "TRANSACTION_FAILED",
    ):
        assert expected in types, types
    assert "ORDER_CREATED" not in types
    assert "TRANSACTION_PAID" not in types


def test_a_direct_order_creation_failure_reports_the_final_state(
    quote_for, client, broken_razorpay, db_session
):
    """The same contract on the autonomous path, which has no human in it."""
    quote = quote_for(quantity=2)

    response = client.post(
        "/app/v1/create-order", json={"quote_id": quote["quote_id"]}
    )
    detail = response.json()["detail"]

    assert response.status_code == 502
    assert detail["reason"] == "PROVIDER_ERROR"
    assert detail["transaction_status"] == TransactionStatus.FAILED.value

    txn = db_session.get(transaction_db, detail["transaction_id"])
    assert txn is not None, "the refusal must name the transaction it ended"
    assert txn.status is TransactionStatus.FAILED
    assert txn.razorpay_order_id is None


def test_no_refusal_of_an_order_attempt_can_report_paid(
    awaiting, client, broken_razorpay
):
    """`transaction_status` reports state; it may never assert settlement."""
    rec, transaction_id = awaiting()

    response = client.post(
        f"/app/v1/transactions/{transaction_id}/approve",
        json={"reviewer": "demo-approver"},
    )

    assert response.json()["detail"]["transaction_status"] != (
        TransactionStatus.PAID.value
    )
    # A second resolution is refused outright, and still claims nothing.
    again = client.post(
        f"/app/v1/transactions/{transaction_id}/approve",
        json={"reviewer": "demo-approver"},
    )
    assert again.status_code == 409
    assert again.json()["detail"].get("transaction_status") != (
        TransactionStatus.PAID.value
    )


# ---------------------------------------------------------------------------
# 2. The page, rendered
# ---------------------------------------------------------------------------


@needs_node
def test_the_page_before_a_decision_offers_approve_and_reject(awaiting, render):
    """The state the bug report says must be preserved."""
    rec, transaction_id = awaiting()

    ui = render(rec, ["submit", "click:Open approval request"])

    assert ui["action_badge"]["text"] == "AWAITING HUMAN APPROVAL"
    assert [button["label"] for button in ui["buttons"]] == ["Approve", "Reject"]
    assert ui["payment"]["badge"] == "NOT PAID"
    assert ui["payment"]["detail"] == "Waiting on a human decision."
    assert ui["transaction"]["status"] == "AWAITING_APPROVAL"
    assert ui["transaction"]["order"] == "none"
    assert ui["transaction"]["payment"] == "none"


@needs_node
def test_the_page_after_a_provider_failure_renders_the_server_state(
    awaiting, render, broken_razorpay
):
    """The reported bug, end to end.

    Approval succeeds, re-validation ALLOWs, order creation fails, and the
    page must show the transaction the server actually has.
    """
    rec, transaction_id = awaiting()
    failure = rec.post(
        f"/app/v1/transactions/{transaction_id}/approve", {"reviewer": "demo-approver"}
    )
    assert failure.status_code == 502
    # The page re-reads the trail after the failure, as it does after every
    # action.
    rec.get(f"/app/v1/transactions/{transaction_id}/audit")

    ui = render(rec, ["submit", "click:Open approval request", "click:Approve"])

    # 1. no stale approval badge, and no stale approval controls.
    assert "AWAITING" not in ui["action_badge"]["text"].upper()
    assert ui["action_badge"]["text"] == "HUMAN APPROVED · PAYMENT FAILED"
    assert ui["action_badge"]["class"] == "badge badge-red"
    assert ui["buttons"] == []

    # 2. the two facts stay apart: the human approved; the payment failed.
    assert "Human approval: APPROVED" in ui["action_footnote"]
    assert "Payment state: FAILED" in ui["action_footnote"]

    # 3. an explicit, server-worded failure. Nothing was charged.
    assert ui["payment"]["badge"] == "NOT PAID"
    assert ui["payment"]["detail"] == PROVIDER_MESSAGE
    assert "Waiting on a human decision" not in ui["payment"]["detail"]

    # 4. the transaction panel is the server's, and carries no provider ids.
    assert ui["transaction"]["hidden"] is False
    assert ui["transaction"]["status"] == "FAILED"
    assert ui["transaction"]["order"] == "none"
    assert ui["transaction"]["payment"] == "none"

    # 5. and the trail the page shows says the same thing.
    assert "Human approved" in ui["audit"]["events"]
    assert "Razorpay order failed" in ui["audit"]["events"]
    assert "Transaction failed" in ui["audit"]["events"]
    assert "Razorpay order created" not in ui["audit"]["events"]
    assert "Transaction PAID" not in ui["audit"]["events"]


@needs_node
def test_the_page_after_a_rejection_shows_the_cancelled_transaction(
    awaiting, render, broken_razorpay
):
    """Sibling state 1. The provider is never contacted by a rejection."""
    rec, transaction_id = awaiting()
    rejection = rec.post(
        f"/app/v1/transactions/{transaction_id}/reject", {"reviewer": "demo-approver"}
    )
    assert rejection.status_code == 200
    rec.get(f"/app/v1/transactions/{transaction_id}/audit")

    ui = render(rec, ["submit", "click:Open approval request", "click:Reject"])

    assert ui["action_badge"]["text"] == "REJECTED BY HUMAN"
    assert ui["buttons"] == []
    assert ui["payment"]["badge"] == "NOT PAID"
    assert ui["transaction"]["status"] == "CANCELLED"
    assert ui["transaction"]["order"] == "none"
    assert ui["transaction"]["payment"] == "none"


@needs_node
def test_the_page_after_a_successful_approval_shows_the_order(
    awaiting, render, fake_razorpay
):
    """Sibling state 2. ORDER_CREATED is rendered, and it is not PAID."""
    rec, transaction_id = awaiting()
    resolution = rec.post(
        f"/app/v1/transactions/{transaction_id}/approve", {"reviewer": "demo-approver"}
    )
    assert resolution.status_code == 200
    order_id = resolution.json()["transaction"]["razorpay_order_id"]
    rec.get(f"/app/v1/transactions/{transaction_id}/audit")

    ui = render(rec, ["submit", "click:Open approval request", "click:Approve"])

    assert ui["action_badge"]["text"] == "APPROVED · REVALIDATED"
    assert ui["payment"]["badge"] == "ORDER CREATED · NOT PAID"
    assert ui["transaction"]["status"] == "ORDER_CREATED"
    assert ui["transaction"]["order"] == order_id
    assert ui["transaction"]["payment"] == "none"
    # Checkout was never loaded in the harness, so the page says so rather
    # than offering a payment it cannot make.
    assert ui["buttons"] == []
    assert ui["action_error"]["hidden"] is False


@needs_node
def test_the_page_after_a_direct_order_failure_renders_the_server_state(
    flow, render, broken_razorpay
):
    """Sibling state 4. The autonomous path, with no human in it.

    The pre-action "Proceed to Payment" offer must not survive an attempt
    the server has already ended.
    """
    rec, agent = flow("cheap_id")
    assert agent["next_action"] == "create_order"

    failure = rec.post("/app/v1/create-order", {"quote_id": agent["quote"]["quote_id"]})
    assert failure.status_code == 502
    transaction_id = failure.json()["detail"]["transaction_id"]
    rec.get(f"/app/v1/transactions/{transaction_id}/audit")

    ui = render(rec, ["submit", "click:Proceed to Payment"])

    assert ui["action_badge"]["text"] == "PAYMENT FAILED"
    assert ui["buttons"] == []
    assert ui["payment"]["badge"] == "NOT PAID"
    assert ui["payment"]["detail"] == PROVIDER_MESSAGE
    assert ui["transaction"]["status"] == "FAILED"
    assert ui["transaction"]["order"] == "none"
    assert ui["transaction"]["payment"] == "none"
    assert "Razorpay order failed" in ui["audit"]["events"]
