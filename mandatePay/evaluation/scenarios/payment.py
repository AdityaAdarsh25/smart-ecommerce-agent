"""Group 5 -- payment state.

PAID is the only status that means money moved, and exactly one code path
may write it. These scenarios attack that path from every angle a real
callback could: a forged signature, someone else's order id, the same
callback twice, a second charge on a settled transaction, and a provider
that falls over mid-flow.

Each one checks the same three things -- status, balance, stock -- because
a payment system is only correct if all three agree.
"""

from backend.databases.transaction_db import transaction_db
from backend.enums.TransactionStatus import TransactionStatus
from evaluation.scenario import GROUP_PAYMENT, METRIC_PAYMENT_STATE, Scenario
from evaluation.scenarios._common import (
    MUST_NOT_MOVE_MONEY,
    MUST_NOT_PAY,
    no_payment_unsafe,
    refusal_reason,
)

PRODUCT = "widget"
QUANTITY = 2
TOTAL = 200.0


def _scenario(scenario_id, description, expected, run, unsafe_if=()):
    return Scenario(
        id=scenario_id,
        group=GROUP_PAYMENT,
        metric=METRIC_PAYMENT_STATE,
        description=description,
        expected=expected,
        run=run,
        unsafe_if=unsafe_if,
    )


def _ordered(env, product=PRODUCT, quantity=QUANTITY):
    """A transaction with a live Razorpay order behind it."""
    quote, _, order = env.buy(product, quantity)
    return quote, order, order["transaction"]["id"], order["transaction"][
        "razorpay_order_id"
    ]


# --- an order is not a payment ---------------------------------------------


def _order_created_is_not_paid(env):
    before_balance = env.balance()
    before_stock = env.stock(PRODUCT)
    _, order, transaction_id, _ = _ordered(env)
    txn = env.transaction(transaction_id)
    return {
        "status": txn.status.value,
        "paid": txn.status.value == "paid",
        "razorpay_payment_id": txn.razorpay_payment_id,
        "balance_changed": env.balance() != before_balance,
        "stock_changed": env.stock(PRODUCT) != before_stock,
    }


# --- the happy path --------------------------------------------------------


def _valid_verification_settles(env):
    before_balance = env.balance()
    before_stock = env.stock(PRODUCT)
    quote, order, transaction_id, order_id = _ordered(env)

    status, body = env.verify(transaction_id, order_id=order_id)
    return {
        "http": status,
        "verified": body.get("verified"),
        "already_verified": body.get("already_verified"),
        "status": env.transaction(transaction_id).status.value,
        "balance_delta": round(before_balance - env.balance(), 2),
        "stock_delta": before_stock - env.stock(PRODUCT),
        "quote_status": env.quote_row(quote["quote_id"]).status.value,
    }


def _payment_id_is_recorded(env):
    _, _, transaction_id, order_id = _ordered(env)
    env.verify(transaction_id, order_id=order_id, payment_id="pay_SETTLED")
    txn = env.transaction(transaction_id)
    return {"status": txn.status.value, "razorpay_payment_id": txn.razorpay_payment_id}


# --- forged and misdirected callbacks --------------------------------------


def _invalid_signature(env):
    before_balance = env.balance()
    before_stock = env.stock(PRODUCT)
    quote, _, transaction_id, order_id = _ordered(env)

    status, body = env.verify(
        transaction_id, order_id=order_id, signature="f" * 64
    )
    return {
        "http": status,
        "reason": refusal_reason(body),
        "status": env.transaction(transaction_id).status.value,
        "paid": env.transaction(transaction_id).status.value == "paid",
        "balance_changed": env.balance() != before_balance,
        "stock_changed": env.stock(PRODUCT) != before_stock,
        # An unverified payment must not consume the quote either.
        "quote_status": env.quote_row(quote["quote_id"]).status.value,
    }


def _empty_signature(env):
    """Refused one layer earlier than a forged one.

    `razorpay_signature` is declared with a minimum length, so an empty
    string is rejected by request validation before the route runs. The
    transaction is left untouched and still payable -- which is the right
    outcome, and a different one from the forged-signature case that marks
    it FAILED.
    """
    before_balance = env.balance()
    _, _, transaction_id, order_id = _ordered(env)
    status, body = env.verify(transaction_id, order_id=order_id, signature="")
    return {
        "http": status,
        "reason": refusal_reason(body),
        "status": env.transaction(transaction_id).status.value,
        "paid": env.transaction(transaction_id).status.value == "paid",
        "balance_changed": env.balance() != before_balance,
    }


def _wrong_order_id(env):
    before_balance = env.balance()
    before_stock = env.stock(PRODUCT)
    _, _, transaction_id, _ = _ordered(env)

    status, body = env.verify(transaction_id, order_id="order_SOMEONE_ELSE")
    return {
        "http": status,
        "reason": refusal_reason(body),
        # Refused without being marked failed: this transaction is still
        # perfectly payable by its real callback.
        "status": env.transaction(transaction_id).status.value,
        "paid": env.transaction(transaction_id).status.value == "paid",
        "balance_changed": env.balance() != before_balance,
        "stock_changed": env.stock(PRODUCT) != before_stock,
    }


# --- replays and second charges --------------------------------------------


def _repeated_verification_is_idempotent(env):
    before_balance = env.balance()
    before_stock = env.stock(PRODUCT)
    _, _, transaction_id, order_id = _ordered(env)

    first_status, first = env.verify(transaction_id, order_id=order_id)
    second_status, second = env.verify(transaction_id, order_id=order_id)

    return {
        "first_http": first_status,
        "second_http": second_status,
        "first_already_verified": first["already_verified"],
        "second_already_verified": second["already_verified"],
        # Exactly ONE mutation, however many times the callback arrives.
        "balance_delta": round(before_balance - env.balance(), 2),
        "stock_delta": before_stock - env.stock(PRODUCT),
    }


def _three_replays_still_one_mutation(env):
    before_balance = env.balance()
    before_stock = env.stock(PRODUCT)
    _, _, transaction_id, order_id = _ordered(env)

    for _ in range(3):
        env.verify(transaction_id, order_id=order_id)

    return {
        "balance_delta": round(before_balance - env.balance(), 2),
        "stock_delta": before_stock - env.stock(PRODUCT),
        "status": env.transaction(transaction_id).status.value,
    }


def _second_charge_on_settled_transaction(env):
    _, _, transaction_id, order_id = _ordered(env)
    env.verify(transaction_id, order_id=order_id, payment_id="pay_FIRST")
    balance_after_first = env.balance()

    status, body = env.verify(
        transaction_id, order_id=order_id, payment_id="pay_SECOND"
    )
    return {
        "http": status,
        "reason": refusal_reason(body),
        "balance_unchanged_by_second": env.balance() == balance_after_first,
        "razorpay_payment_id": env.transaction(transaction_id).razorpay_payment_id,
    }


# --- wrong state -----------------------------------------------------------


def _verify_before_an_order_exists(env):
    """An AWAITING_APPROVAL transaction has no order to settle."""
    before_balance = env.balance()
    _, _, order = env.buy("gadget", 6)  # 1500 -> approval band
    transaction_id = order["transaction"]["id"]

    status, body = env.verify(transaction_id, order_id="order_INVENTED")
    return {
        "http": status,
        "reason": refusal_reason(body),
        "status": env.transaction(transaction_id).status.value,
        "balance_changed": env.balance() != before_balance,
    }


def _verify_a_blocked_transaction(env):
    before_balance = env.balance()
    env.set_mandate(absolute_transaction_limit=100.0)
    _, _, order = env.buy(PRODUCT, QUANTITY)
    transaction_id = order["transaction"]["id"]

    status, body = env.verify(transaction_id, order_id="order_INVENTED")
    return {
        "http": status,
        "reason": refusal_reason(body),
        "status": env.transaction(transaction_id).status.value,
        "balance_changed": env.balance() != before_balance,
    }


def _verify_unknown_transaction(env):
    status, body = env.verify("00000000-0000-0000-0000-000000000000")
    return {"http": status, "reason": refusal_reason(body)}


# --- the provider falls over -----------------------------------------------


def _provider_failure_never_pays(env):
    before_balance = env.balance()
    before_stock = env.stock(PRODUCT)
    env.break_provider()

    _, quote = env.quote(PRODUCT, QUANTITY)
    status, body = env.create_order(quote["quote_id"])

    env.db.expire_all()
    txn = env.db.query(transaction_db).one()

    return {
        "http": status,
        "status": txn.status.value,
        "paid": txn.status.value == "paid",
        "razorpay_order_id": txn.razorpay_order_id,
        "balance_changed": env.balance() != before_balance,
        "stock_changed": env.stock(PRODUCT) != before_stock,
        # The call WAS attempted -- this is a provider failure, not a
        # policy block, and the two must not look the same.
        "provider_attempted": env.provider_calls() > 0,
        "quote_status": env.quote_row(quote["quote_id"]).status.value,
    }


def _provider_failure_then_retry_succeeds(env):
    env.break_provider()
    _, quote = env.quote(PRODUCT, QUANTITY)
    env.create_order(quote["quote_id"])

    # The provider comes back. The quote is still live and the failed
    # attempt is dead, so a retry must be allowed through.
    env.razorpay.working = True
    status, body = env.create_order(quote["quote_id"])
    return {
        "http": status,
        "status": body["transaction"]["status"],
        "razorpay_order_id": body["transaction"]["razorpay_order_id"],
    }


SCENARIOS = [
    _scenario(
        "PAY-001",
        "A Razorpay order is an intent to collect, never a collection",
        {
            "status": "order_created",
            "paid": False,
            "razorpay_payment_id": None,
            "balance_changed": False,
            "stock_changed": False,
        },
        _order_created_is_not_paid,
        unsafe_if=MUST_NOT_PAY + MUST_NOT_MOVE_MONEY,
    ),
    _scenario(
        "PAY-002",
        "A verified payment settles: PAID, balance down, stock down, quote "
        "consumed",
        {
            "http": 200,
            "verified": True,
            "already_verified": False,
            "status": "paid",
            "balance_delta": TOTAL,
            "stock_delta": QUANTITY,
            "quote_status": "consumed",
        },
        _valid_verification_settles,
    ),
    _scenario(
        "PAY-003",
        "The settling payment id is recorded alongside PAID",
        {"status": "paid", "razorpay_payment_id": "pay_SETTLED"},
        _payment_id_is_recorded,
    ),
    _scenario(
        "PAY-004",
        "A forged signature settles nothing and moves nothing",
        {
            "http": 400,
            "reason": "SIGNATURE_VERIFICATION_FAILED",
            "status": "failed",
            "paid": False,
            "balance_changed": False,
            "stock_changed": False,
            "quote_status": "active",
        },
        _invalid_signature,
        unsafe_if=no_payment_unsafe(),
    ),
    _scenario(
        "PAY-005",
        "An empty signature is refused by request validation, before the "
        "settlement path runs at all",
        {
            # 422, not 400: this never reaches the route, so there is no
            # RejectionReason to report.
            "http": 422,
            "reason": None,
            "status": "order_created",
            "paid": False,
            "balance_changed": False,
        },
        _empty_signature,
        unsafe_if=no_payment_unsafe(),
    ),
    _scenario(
        "PAY-006",
        "A callback for somebody else's order is refused, and leaves this "
        "transaction payable",
        {
            "http": 409,
            "reason": "ORDER_ID_MISMATCH",
            "status": "order_created",
            "paid": False,
            "balance_changed": False,
            "stock_changed": False,
        },
        _wrong_order_id,
        unsafe_if=no_payment_unsafe(),
    ),
    _scenario(
        "PAY-007",
        "The same callback twice is idempotent, with exactly one mutation",
        {
            "first_http": 200,
            "second_http": 200,
            "first_already_verified": False,
            "second_already_verified": True,
            "balance_delta": TOTAL,
            "stock_delta": QUANTITY,
        },
        _repeated_verification_is_idempotent,
    ),
    _scenario(
        "PAY-008",
        "Three replays still move the money exactly once",
        {"balance_delta": TOTAL, "stock_delta": QUANTITY, "status": "paid"},
        _three_replays_still_one_mutation,
    ),
    _scenario(
        "PAY-009",
        "A DIFFERENT payment against a settled transaction is refused loudly "
        "and charges nothing",
        {
            "http": 409,
            "reason": "PAYMENT_ALREADY_SETTLED",
            "balance_unchanged_by_second": True,
            "razorpay_payment_id": "pay_FIRST",
        },
        _second_charge_on_settled_transaction,
    ),
    _scenario(
        "PAY-010",
        "A transaction still awaiting a human cannot be settled",
        {
            "http": 409,
            "reason": "TRANSACTION_NOT_AWAITING_PAYMENT",
            "status": "awaiting_approval",
            "balance_changed": False,
        },
        _verify_before_an_order_exists,
        unsafe_if=no_payment_unsafe(),
    ),
    _scenario(
        "PAY-011",
        "A BLOCKED transaction cannot be settled",
        {
            "http": 409,
            "reason": "TRANSACTION_NOT_AWAITING_PAYMENT",
            "status": "blocked",
            "balance_changed": False,
        },
        _verify_a_blocked_transaction,
        unsafe_if=no_payment_unsafe(),
    ),
    _scenario(
        "PAY-012",
        "A callback for a transaction that does not exist is refused",
        {"http": 404, "reason": "TRANSACTION_NOT_FOUND"},
        _verify_unknown_transaction,
    ),
    _scenario(
        "PAY-013",
        "A provider that throws leaves FAILED, no order id, and no money "
        "moved",
        {
            "http": 502,
            "status": "failed",
            "paid": False,
            "razorpay_order_id": None,
            "balance_changed": False,
            "stock_changed": False,
            "provider_attempted": True,
            "quote_status": "active",
        },
        _provider_failure_never_pays,
        unsafe_if=no_payment_unsafe(),
    ),
    _scenario(
        "PAY-014",
        "A dead provider attempt does not stop the retry that follows it",
        {
            "http": 200,
            "status": "order_created",
            "razorpay_order_id": "order_EVAL123",
        },
        _provider_failure_then_retry_succeeds,
    ),
]


# --- settlement-time re-checks and downstream truth ------------------------


def _balance_falls_between_order_and_payment(env):
    _, _, transaction_id, order_id = _ordered(env)
    before_stock = env.stock(PRODUCT)

    # The buyer can no longer fund what they ordered.
    env.set_balance(10.0)

    status, body = env.verify(transaction_id, order_id=order_id)
    return {
        "http": status,
        "reason": refusal_reason(body),
        "status": env.transaction(transaction_id).status.value,
        "paid": env.transaction(transaction_id).status.value == "paid",
        "balance": env.balance(),
        "stock_changed": env.stock(PRODUCT) != before_stock,
    }


def _audit_orders_verification_before_settlement(env):
    """PAYMENT_VERIFIED and TRANSACTION_PAID are two different claims, and
    the trail must not report the second without the first."""
    _, _, transaction_id, order_id = _ordered(env)
    env.verify(transaction_id, order_id=order_id)

    _, audit = env.audit(transaction_id)
    story = [event["event_type"] for event in audit["events"]]
    return {
        "has_verified": "PAYMENT_VERIFIED" in story,
        "has_paid": "TRANSACTION_PAID" in story,
        "verified_precedes_paid": (
            "PAYMENT_VERIFIED" in story
            and "TRANSACTION_PAID" in story
            and story.index("PAYMENT_VERIFIED") < story.index("TRANSACTION_PAID")
        ),
        "ends_paid": story[-1] if story else None,
    }


def _only_paid_spend_counts_against_the_cap(env):
    """A verified payment consumes the monthly allowance; nothing else does.

    The follow-up purchase is a DIFFERENT product on purpose. Re-quoting
    the same one would also trip duplicate detection, and this scenario is
    about the cap alone.
    """
    env.set_mandate(monthly_cap=300.0)
    env.pay(PRODUCT, QUANTITY)  # 200.00, verified and settled

    # 250.00 on top of 200.00 is 450.00, over the 300.00 cap.
    _, body = env.quote("gadget", 1)
    policy = body["policy"]
    return {
        "decision": policy["decision"],
        "violations": sorted(policy["violation_codes"]),
    }


def _two_products_settle_independently(env):
    before_widget = env.stock("widget")
    before_gadget = env.stock("gadget")
    before_balance = env.balance()

    env.pay("widget", 2, payment_id="pay_ONE")  # 200.00
    env.pay("gadget", 2, payment_id="pay_TWO")  # 500.00

    return {
        "widget_delta": before_widget - env.stock("widget"),
        "gadget_delta": before_gadget - env.stock("gadget"),
        "balance_delta": round(before_balance - env.balance(), 2),
        "paid_transactions": env.db.query(transaction_db)
        .filter(transaction_db.status == TransactionStatus.PAID)
        .count(),
    }


SCENARIOS += [
    _scenario(
        "PAY-015",
        "A balance that fell between order and payment stops settlement, and "
        "no stock moves",
        {
            "http": 409,
            "reason": "INSUFFICIENT_BALANCE",
            "status": "failed",
            "paid": False,
            "balance": 10.0,
            "stock_changed": False,
        },
        _balance_falls_between_order_and_payment,
        unsafe_if=no_payment_unsafe(),
    ),
    _scenario(
        "PAY-016",
        "The trail records the signature check before it records the money "
        "moving",
        {
            "has_verified": True,
            "has_paid": True,
            "verified_precedes_paid": True,
            "ends_paid": "TRANSACTION_PAID",
        },
        _audit_orders_verification_before_settlement,
    ),
    _scenario(
        "PAY-017",
        "A settled payment consumes the monthly cap, so the next identical "
        "purchase is blocked on it",
        {"decision": "block", "violations": ["monthly_cap"]},
        _only_paid_spend_counts_against_the_cap,
        unsafe_if=(("decision", ("allow", "require_approval")),),
    ),
    _scenario(
        "PAY-018",
        "Two settled purchases each move their own stock and sum correctly "
        "against the balance",
        {
            "widget_delta": 2,
            "gadget_delta": 2,
            "balance_delta": 700.0,
            "paid_transactions": 2,
        },
        _two_products_settle_independently,
    ),
]
