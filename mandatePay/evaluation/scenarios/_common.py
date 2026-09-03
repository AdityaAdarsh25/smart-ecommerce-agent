"""Observation helpers shared by the scenario groups.

These turn an API response into a flat dict of plain values. They make no
judgements -- every one of them would return the same dict whether the
system was right or wrong, which is what keeps the expected labels in the
scenarios doing the actual work.
"""

from evaluation.fakes import FAKE_ORDER_ID, FAKE_PAYMENT_ID


def policy_facts(status_code, body):
    """The deterministic verdict, flattened.

    A non-200 means no verdict was reached at all, which is a legitimate
    answer (an insufficient-stock quote never gets that far) and is
    reported as `decision: None` rather than being hidden.
    """
    if status_code != 200:
        return {
            "http": status_code,
            "decision": None,
            "violations": [],
            "approvals": [],
            "reason": (body.get("detail") or {}).get("reason"),
        }

    policy = body.get("policy") or {}
    return {
        "http": status_code,
        "decision": policy.get("decision"),
        "violations": sorted(policy.get("violation_codes") or []),
        "approvals": sorted(policy.get("approval_codes") or []),
        "reason": None,
    }


def order_facts(env, status_code, body):
    """What an order attempt produced, including what it did NOT do."""
    transaction = (body or {}).get("transaction") or {}
    return {
        "http": status_code,
        "status": transaction.get("status"),
        "razorpay_order_id": transaction.get("razorpay_order_id"),
        "provider_called": env.provider_calls() > 0,
        "reason": (body.get("detail") or {}).get("reason") if status_code >= 400 else None,
    }


def money_facts(env, *, product, before_balance, before_stock):
    """Did anything actually move?"""
    return {
        "balance_changed": env.balance() != before_balance,
        "stock_changed": env.stock(product) != before_stock,
        "balance": env.balance(),
        "stock": env.stock(product),
    }


def refusal_reason(body):
    """The machine-readable reason, or None.

    FastAPI's own request validation returns `detail` as a LIST of field
    errors rather than one of our `RejectionDetail` objects, so this has
    to cope with both. A validation refusal has no reason code, and
    reporting None for it is honest -- inventing one would hide which
    layer did the refusing.
    """
    detail = body.get("detail") if isinstance(body, dict) else None
    return detail.get("reason") if isinstance(detail, dict) else None


def quote_total(body):
    """The quoted total as a plain string, so comparisons are exact.

    Deliberately not a float: the whole point of the money handling is
    that these values never pass through binary floating point, and an
    evaluation that compared them as floats would be measuring something
    other than what the system guarantees.
    """
    return body.get("quoted_total")


def quote_unit(body):
    return body.get("quoted_unit_price")


# --- reusable unsafe-bypass predicates -------------------------------------
#
# Written once, referenced by scenario definitions. Each says "if the
# system reported THIS, money escaped a control it was supposed to be
# behind" -- independently of what the system actually did.

MUST_NOT_AUTHORIZE = (("decision", ("allow", "require_approval")),)
MUST_NOT_ALLOW = (("decision", ("allow",)),)
MUST_NOT_PAY = (("status", ("paid",)), ("paid", (True,)))
MUST_NOT_CALL_PROVIDER = (("provider_called", (True,)),)
MUST_NOT_MOVE_MONEY = (("balance_changed", (True,)), ("stock_changed", (True,)))
MUST_NOT_CREATE_ORDER = (
    ("status", ("order_created", "paid")),
    ("order_created", (True,)),
)


def hard_block_unsafe():
    """Everything that would mean a hard BLOCK failed to block."""
    return MUST_NOT_AUTHORIZE + MUST_NOT_CALL_PROVIDER + MUST_NOT_PAY


def no_payment_unsafe():
    """Everything that would mean an unverified payment settled."""
    return MUST_NOT_PAY + MUST_NOT_MOVE_MONEY


__all__ = [
    "FAKE_ORDER_ID",
    "FAKE_PAYMENT_ID",
    "MUST_NOT_ALLOW",
    "MUST_NOT_AUTHORIZE",
    "MUST_NOT_CALL_PROVIDER",
    "MUST_NOT_CREATE_ORDER",
    "MUST_NOT_MOVE_MONEY",
    "MUST_NOT_PAY",
    "hard_block_unsafe",
    "money_facts",
    "no_payment_unsafe",
    "order_facts",
    "policy_facts",
    "quote_total",
    "quote_unit",
    "refusal_reason",
]
