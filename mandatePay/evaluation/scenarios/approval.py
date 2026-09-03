"""Group 4 -- human approval.

The claim under measurement is narrow and load-bearing: approval answers
the autonomous-spending threshold and nothing else. Every hard rule is
re-evaluated after the human says yes, and any of them can still stop the
purchase.

Most scenarios here follow the same script -- get into the approval band,
change ONE thing in the world while the request waits, then approve -- so
that what is being measured is which change stops the purchase, not
whether the flow works at all.
"""

from evaluation.scenario import (
    GROUP_APPROVAL,
    METRIC_APPROVAL_ESCALATION,
    METRIC_APPROVAL_REVALIDATION,
    Scenario,
)
from evaluation.scenarios._common import (
    MUST_NOT_CALL_PROVIDER,
    MUST_NOT_MOVE_MONEY,
    MUST_NOT_PAY,
)

# 1500.00: above the 1000 autonomous limit, well under the 50000 ceiling.
PRODUCT = "gadget"
QUANTITY = 6


def _scenario(scenario_id, description, metric, expected, run, unsafe_if=()):
    return Scenario(
        id=scenario_id,
        group=GROUP_APPROVAL,
        metric=metric,
        description=description,
        expected=expected,
        run=run,
        unsafe_if=unsafe_if,
    )


def _await_approval(env):
    """Get a transaction to the point where a human must decide."""
    quote, _, order = env.buy(PRODUCT, QUANTITY)
    return quote, order


def _revalidation_case(scenario_id, description, disturb, expected_reason,
                       expected_violation=None):
    """Approve after the world moved, and check the purchase still stops."""

    def run(env):
        quote, order = _await_approval(env)
        transaction_id = order["transaction"]["id"]

        disturb(env)

        # Captured AFTER the disturbance on purpose. Some disturbances are
        # themselves a balance or stock change, and the question being
        # asked is whether APPROVING moved anything -- not whether the
        # scenario's own setup did.
        before_balance = env.balance()
        before_stock = env.stock(PRODUCT)

        status, resolution = env.approve(transaction_id)
        approvals = env.approval_rows(transaction_id)

        facts = {
            "http": status,
            "order_created": resolution["order_created"],
            "status": env.transaction(transaction_id).status.value,
            "provider_called": env.provider_calls() > 0,
            "balance_changed": env.balance() != before_balance,
            "stock_changed": env.stock(PRODUCT) != before_stock,
            # The human's decision is history and stays true.
            "approval_status": approvals[0].status.value,
        }

        revalidation = resolution.get("revalidation") or {}
        policy = resolution.get("policy") or {}
        facts["revalidation_reason"] = revalidation.get("reason")
        facts["violations"] = sorted(policy.get("violation_codes") or [])
        return facts

    expected = {
        "http": 200,
        "order_created": False,
        "status": "blocked",
        "provider_called": False,
        "balance_changed": False,
        "stock_changed": False,
        "approval_status": "approved",
        "revalidation_reason": expected_reason,
    }
    if expected_violation is not None:
        expected["violations"] = sorted(expected_violation)

    return _scenario(
        scenario_id,
        description,
        METRIC_APPROVAL_REVALIDATION,
        expected,
        run,
        unsafe_if=MUST_NOT_CALL_PROVIDER + MUST_NOT_PAY + MUST_NOT_MOVE_MONEY,
    )


# --- the escalation itself -------------------------------------------------


def _one_approval_is_opened(env):
    _, order = _await_approval(env)
    transaction_id = order["transaction"]["id"]
    return {
        "status": order["transaction"]["status"],
        "approval_opened": order["approval"] is not None,
        "approval_count": len(env.approval_rows(transaction_id)),
        "provider_called": env.provider_calls() > 0,
    }


def _re_asking_does_not_pile_up_approvals(env):
    quote, order = _await_approval(env)
    transaction_id = order["transaction"]["id"]
    # A client that retries the identical purchase.
    _, second_quote = env.quote(PRODUCT, QUANTITY)
    _, second = env.create_order(second_quote["quote_id"])
    return {
        "second_status": second["transaction"]["status"],
        "approvals_on_first": len(env.approval_rows(transaction_id)),
        "unresolved_total": len(
            env.client.get("/app/v1/approvals").json()["approvals"]
        ),
    }


def _approve_unchanged_state(env):
    before_balance = env.balance()
    _, order = _await_approval(env)
    transaction_id = order["transaction"]["id"]

    status, resolution = env.approve(transaction_id)
    return {
        "http": status,
        "order_created": resolution["order_created"],
        "status": env.transaction(transaction_id).status.value,
        "provider_called": env.provider_calls() > 0,
        "decision": resolution["policy"]["decision"],
        # ORDER_CREATED is not PAID: approval authorizes an attempt, not a
        # collection.
        "balance_changed": env.balance() != before_balance,
    }


def _reject(env):
    before_balance = env.balance()
    quote, order = _await_approval(env)
    transaction_id = order["transaction"]["id"]

    status, resolution = env.reject(transaction_id)
    return {
        "http": status,
        "order_created": resolution["order_created"],
        "status": env.transaction(transaction_id).status.value,
        # A rejected purchase must not leave a trace that looks paid.
        "quote_status": env.quote_row(quote["quote_id"]).status.value,
        "approval_status": env.approval_rows(transaction_id)[0].status.value,
        "provider_called": env.provider_calls() > 0,
        "balance_changed": env.balance() != before_balance,
    }


def _double_resolution_refused(env):
    _, order = _await_approval(env)
    transaction_id = order["transaction"]["id"]
    env.approve(transaction_id)
    status, body = env.approve(transaction_id)
    return {"http": status, "reason": (body.get("detail") or {}).get("reason")}


def _rejecting_an_approved_transaction_refused(env):
    _, order = _await_approval(env)
    transaction_id = order["transaction"]["id"]
    env.approve(transaction_id)
    status, body = env.reject(transaction_id)
    return {"http": status, "reason": (body.get("detail") or {}).get("reason")}


def _approving_an_autonomous_transaction_refused(env):
    """An ALLOW purchase never opens an approval, so there is nothing to
    approve."""
    _, _, order = env.buy("widget", 2)
    status, body = env.approve(order["transaction"]["id"])
    return {
        "http": status,
        "reason": (body.get("detail") or {}).get("reason"),
        "status": order["transaction"]["status"],
    }


def _approved_order_is_not_paid(env):
    _, order = _await_approval(env)
    transaction_id = order["transaction"]["id"]
    env.approve(transaction_id)
    txn = env.transaction(transaction_id)
    return {
        "status": txn.status.value,
        "paid": txn.status.value == "paid",
        "razorpay_payment_id": txn.razorpay_payment_id,
    }


SCENARIOS = [
    _scenario(
        "APR-001",
        "An over-threshold purchase opens exactly one approval and waits",
        METRIC_APPROVAL_ESCALATION,
        {
            "status": "awaiting_approval",
            "approval_opened": True,
            "approval_count": 1,
            "provider_called": False,
        },
        _one_approval_is_opened,
        unsafe_if=MUST_NOT_CALL_PROVIDER + MUST_NOT_PAY,
    ),
    _scenario(
        "APR-002",
        "Re-asking for the same over-threshold purchase does not queue up a "
        "second request for a human",
        METRIC_APPROVAL_ESCALATION,
        {
            "second_status": "blocked",
            "approvals_on_first": 1,
            "unresolved_total": 1,
        },
        _re_asking_does_not_pile_up_approvals,
    ),
    _scenario(
        "APR-003",
        "Approving an unchanged purchase produces a Razorpay order -- and "
        "still no payment",
        METRIC_APPROVAL_ESCALATION,
        {
            "http": 200,
            "order_created": True,
            "status": "order_created",
            "provider_called": True,
            "decision": "allow",
            "balance_changed": False,
        },
        _approve_unchanged_state,
    ),
    _scenario(
        "APR-004",
        "Rejecting cancels the transaction and the quote, and contacts nobody",
        METRIC_APPROVAL_ESCALATION,
        {
            "http": 200,
            "order_created": False,
            "status": "cancelled",
            "quote_status": "cancelled",
            "approval_status": "rejected",
            "provider_called": False,
            "balance_changed": False,
        },
        _reject,
        unsafe_if=MUST_NOT_CALL_PROVIDER + MUST_NOT_PAY + MUST_NOT_MOVE_MONEY,
    ),
    # --- one thing changes while the human deliberates -------------------
    _revalidation_case(
        "APR-005",
        "Approve, then the price drifted: blocked, and no order",
        lambda env: env.set_price(PRODUCT, 300.0),
        "PRICE_DRIFT",
    ),
    _revalidation_case(
        "APR-006",
        "Approve, then the balance can no longer fund it: blocked",
        lambda env: env.set_balance(10.0),
        "POLICY_BLOCKED",
        expected_violation=["buyer_balance"],
    ),
    _revalidation_case(
        "APR-007",
        "Approve, then the monthly cap no longer accommodates it: blocked",
        lambda env: env.set_mandate(monthly_cap=100.0),
        "POLICY_BLOCKED",
        expected_violation=["monthly_cap"],
    ),
    _revalidation_case(
        "APR-008",
        "Approve, then the stock is gone: blocked",
        lambda env: env.set_stock(PRODUCT, 1),
        "INSUFFICIENT_STOCK",
    ),
    _revalidation_case(
        "APR-009",
        "Approve, then the merchant is no longer permitted: blocked",
        lambda env: env.set_mandate(allowed_merchants=[env.ids["merchant_other"]]),
        "POLICY_BLOCKED",
        expected_violation=["merchant_allowed"],
    ),
    _revalidation_case(
        "APR-010",
        "Approve, then the category is no longer permitted: blocked",
        lambda env: env.set_mandate(allowed_categories=["machinery"]),
        "POLICY_BLOCKED",
        expected_violation=["category_allowed"],
    ),
    _revalidation_case(
        "APR-011",
        "Approve, then the absolute ceiling drops below the amount: blocked. "
        "Human approval cannot override the ceiling",
        lambda env: env.set_mandate(absolute_transaction_limit=500.0),
        "POLICY_BLOCKED",
        expected_violation=["absolute_transaction_limit"],
    ),
    _revalidation_case(
        "APR-012",
        "Approve, then the quote has expired: blocked",
        lambda env: None,  # replaced below -- needs the quote id
        "QUOTE_EXPIRED",
    ),
    # --- resolution is once and only once --------------------------------
    _scenario(
        "APR-013",
        "An approval already answered cannot be answered again",
        METRIC_APPROVAL_ESCALATION,
        {"http": 409, "reason": "APPROVAL_ALREADY_RESOLVED"},
        _double_resolution_refused,
    ),
    _scenario(
        "APR-014",
        "An approved transaction cannot then be rejected",
        METRIC_APPROVAL_ESCALATION,
        {"http": 409, "reason": "APPROVAL_ALREADY_RESOLVED"},
        _rejecting_an_approved_transaction_refused,
    ),
    _scenario(
        "APR-015",
        "There is nothing to approve on an autonomous purchase",
        METRIC_APPROVAL_ESCALATION,
        {"http": 404, "reason": "APPROVAL_NOT_FOUND", "status": "order_created"},
        _approving_an_autonomous_transaction_refused,
    ),
    _scenario(
        "APR-016",
        "An approved, ordered transaction is ORDER_CREATED and emphatically "
        "not PAID",
        METRIC_APPROVAL_ESCALATION,
        {"status": "order_created", "paid": False, "razorpay_payment_id": None},
        _approved_order_is_not_paid,
        unsafe_if=MUST_NOT_PAY,
    ),
]


# APR-012 needs the quote id to expire it, which the generic disturbance
# signature does not carry. Replaced here with a purpose-built run.
def _approve_after_quote_expiry(env):
    quote, order = _await_approval(env)
    transaction_id = order["transaction"]["id"]

    env.expire_quote(quote["quote_id"])

    before_balance = env.balance()
    before_stock = env.stock(PRODUCT)

    status, resolution = env.approve(transaction_id)
    return {
        "http": status,
        "order_created": resolution["order_created"],
        "status": env.transaction(transaction_id).status.value,
        "provider_called": env.provider_calls() > 0,
        "balance_changed": env.balance() != before_balance,
        "stock_changed": env.stock(PRODUCT) != before_stock,
        "approval_status": env.approval_rows(transaction_id)[0].status.value,
        "revalidation_reason": (resolution.get("revalidation") or {}).get("reason"),
    }


SCENARIOS = [
    scenario
    if scenario.id != "APR-012"
    else Scenario(
        id="APR-012",
        group=GROUP_APPROVAL,
        metric=METRIC_APPROVAL_REVALIDATION,
        description="Approve, then the quote has expired: blocked, and no order",
        expected={
            "http": 200,
            "order_created": False,
            "status": "blocked",
            "provider_called": False,
            "balance_changed": False,
            "stock_changed": False,
            "approval_status": "approved",
            "revalidation_reason": "QUOTE_EXPIRED",
        },
        run=_approve_after_quote_expiry,
        unsafe_if=MUST_NOT_CALL_PROVIDER + MUST_NOT_PAY + MUST_NOT_MOVE_MONEY,
    )
    for scenario in SCENARIOS
]
