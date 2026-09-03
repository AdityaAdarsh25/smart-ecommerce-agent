"""Group 2 -- quote, price and stock.

The quote is the promise that the number the buyer agreed to is the number
that gets charged. These scenarios attack that promise from both sides:
the price moving underneath it, and the quote outliving the moment it
described.
"""

from evaluation.scenario import (
    GROUP_QUOTE,
    METRIC_PRICE_DRIFT,
    METRIC_QUOTE_INTEGRITY,
    Scenario,
)
from evaluation.scenarios._common import (
    MUST_NOT_CALL_PROVIDER,
    MUST_NOT_MOVE_MONEY,
    MUST_NOT_PAY,
    refusal_reason,
)


def _scenario(scenario_id, description, metric, expected, run, unsafe_if=()):
    return Scenario(
        id=scenario_id,
        group=GROUP_QUOTE,
        metric=metric,
        description=description,
        expected=expected,
        run=run,
        unsafe_if=unsafe_if,
    )


# --- the price the server read --------------------------------------------


def _server_price(env):
    _, body = env.quote("widget", 1)
    return {"unit_price": body["quoted_unit_price"], "total": body["quoted_total"]}


def _total_is_unit_times_quantity(env):
    _, body = env.quote("widget", 3)
    return {"unit_price": body["quoted_unit_price"], "total": body["quoted_total"]}


def _exact_integer_money(env):
    _, body = env.quote("gadget", 7)
    return {"unit_price": body["quoted_unit_price"], "total": body["quoted_total"]}


def _fractional_price_is_exact(env):
    env.set_price("widget", 100.07)
    _, body = env.quote("widget", 3)
    return {"unit_price": body["quoted_unit_price"], "total": body["quoted_total"]}


def _quote_persists_active(env):
    _, body = env.quote("widget", 2)
    row = env.quote_row(body["quote_id"])
    return {
        "quote_status": body["quote_status"],
        "persisted_status": row.status.value,
        "persisted_total": str(row.quoted_total),
    }


def _quote_carries_a_future_expiry(env):
    from datetime import datetime

    _, body = env.quote("widget", 2)
    created = datetime.fromisoformat(body["created_at"])
    expires = datetime.fromisoformat(body["expires_at"])
    return {"expiry_is_in_the_future": expires > created}


# --- expiry ----------------------------------------------------------------


def _expired_quote_refused(env):
    _, quote = env.quote("widget", 2)
    env.expire_quote(quote["quote_id"])
    status, body = env.create_order(quote["quote_id"])
    return {
        "http": status,
        "reason": refusal_reason(body),
        "provider_called": env.provider_calls() > 0,
        "transactions": env.transaction_count(),
    }


def _consumed_quote_cannot_be_reused(env):
    quote, order, _, _ = env.pay("widget", 2)
    status, body = env.create_order(quote["quote_id"])
    return {
        "http": status,
        "reason": refusal_reason(body),
        # One provider call from the original purchase, and no second one.
        "provider_calls": env.provider_calls(),
    }


# --- drift -----------------------------------------------------------------


def _drift_case(new_price, scenario_id, description, expected_extra=None):
    def run(env):
        _, quote = env.quote("widget", 2)
        quoted_unit = quote["quoted_unit_price"]
        quoted_total = quote["quoted_total"]

        env.set_price("widget", new_price)
        status, body = env.create_order(quote["quote_id"])

        row = env.quote_row(quote["quote_id"])
        return {
            "http": status,
            "reason": refusal_reason(body),
            "provider_called": env.provider_calls() > 0,
            "transactions": env.transaction_count(),
            # The quote is invalidated, never silently repriced.
            "quote_status_after": row.status.value,
            "quoted_unit_unchanged": str(row.quoted_unit_price) == quoted_unit,
            "quoted_total_unchanged": str(row.quoted_total) == quoted_total,
        }

    expected = {
        "http": 409,
        "reason": "PRICE_DRIFT",
        "provider_called": False,
        "transactions": 0,
        "quote_status_after": "expired",
        "quoted_unit_unchanged": True,
        "quoted_total_unchanged": True,
    }
    expected.update(expected_extra or {})
    return _scenario(
        scenario_id,
        description,
        METRIC_PRICE_DRIFT,
        expected,
        run,
        unsafe_if=MUST_NOT_CALL_PROVIDER + MUST_NOT_PAY,
    )


def _cosmetic_change_is_not_drift(env):
    _, quote = env.quote("widget", 2)
    # 100.0 and 100.00 are the same amount of money. Compared in integer
    # paise, so this must NOT read as drift.
    env.set_price("widget", 100.00)
    status, body = env.create_order(quote["quote_id"])
    return {
        "http": status,
        "status": body["transaction"]["status"],
        "provider_called": env.provider_calls() > 0,
    }


# --- stock -----------------------------------------------------------------


def _stock_short_at_quote(env):
    status, body = env.quote("scarce", 3)
    return {"http": status, "reason": refusal_reason(body)}


def _stock_exactly_sufficient(env):
    status, body = env.quote("scarce", 1)
    return {"http": status, "decision": body["policy"]["decision"]}


def _stock_falls_between_quote_and_order(env):
    _, quote = env.quote("widget", 5)
    env.set_stock("widget", 2)
    status, body = env.create_order(quote["quote_id"])
    return {
        "http": status,
        "reason": refusal_reason(body),
        "provider_called": env.provider_calls() > 0,
        "transactions": env.transaction_count(),
    }


def _stock_falls_between_order_and_payment(env):
    before_balance = env.balance()
    quote, _, order = env.buy("widget", 5)
    env.set_stock("widget", 2)

    status, body = env.verify(
        order["transaction"]["id"],
        order_id=order["transaction"]["razorpay_order_id"],
    )
    return {
        "http": status,
        "reason": refusal_reason(body),
        "status": env.transaction(order["transaction"]["id"]).status.value,
        "balance_changed": env.balance() != before_balance,
        "stock": env.stock("widget"),
    }


SCENARIOS = [
    _scenario(
        "QTE-001",
        "The unit price comes from the product row, and only from there",
        METRIC_QUOTE_INTEGRITY,
        {"unit_price": "100.00", "total": "100.00"},
        _server_price,
    ),
    _scenario(
        "QTE-002",
        "The total is unit price times quantity, exactly",
        METRIC_QUOTE_INTEGRITY,
        {"unit_price": "100.00", "total": "300.00"},
        _total_is_unit_times_quantity,
    ),
    _scenario(
        "QTE-003",
        "7 x 250.00 is 1750.00 with no floating-point residue",
        METRIC_QUOTE_INTEGRITY,
        {"unit_price": "250.00", "total": "1750.00"},
        _exact_integer_money,
    ),
    _scenario(
        "QTE-004",
        "3 x 100.07 is 300.21 -- integer paise arithmetic, not 300.20999...",
        METRIC_QUOTE_INTEGRITY,
        {"unit_price": "100.07", "total": "300.21"},
        _fractional_price_is_exact,
    ),
    _scenario(
        "QTE-005",
        "A quote is persisted ACTIVE with the total it reported",
        METRIC_QUOTE_INTEGRITY,
        {
            "quote_status": "active",
            "persisted_status": "active",
            "persisted_total": "200.00",
        },
        _quote_persists_active,
    ),
    _scenario(
        "QTE-006",
        "A quote carries an expiry later than its creation",
        METRIC_QUOTE_INTEGRITY,
        {"expiry_is_in_the_future": True},
        _quote_carries_a_future_expiry,
    ),
    _scenario(
        "QTE-007",
        "An expired quote is refused before the provider is contacted",
        METRIC_QUOTE_INTEGRITY,
        {
            "http": 409,
            "reason": "QUOTE_EXPIRED",
            "provider_called": False,
            "transactions": 0,
        },
        _expired_quote_refused,
        unsafe_if=MUST_NOT_CALL_PROVIDER,
    ),
    _scenario(
        "QTE-008",
        "A quote already consumed by a verified payment cannot fund a second "
        "purchase",
        METRIC_QUOTE_INTEGRITY,
        {"http": 409, "reason": "QUOTE_NOT_ACTIVE", "provider_calls": 1},
        _consumed_quote_cannot_be_reused,
        # A second provider call here would mean one quote funded two
        # purchases, which is the failure this scenario exists to catch.
        unsafe_if=(("provider_calls", (2, 3)),),
    ),
    _drift_case(
        150.0,
        "QTE-009",
        "A price that rose invalidates the quote instead of repricing it",
    ),
    _drift_case(
        50.0,
        "QTE-010",
        "A price that FELL is refused too -- it is still not the price the "
        "buyer agreed to",
    ),
    _drift_case(
        100.01,
        "QTE-011",
        "A one-paise move is drift; the comparison is exact",
    ),
    _scenario(
        "QTE-012",
        "100.0 and 100.00 are the same money and must not read as drift",
        METRIC_PRICE_DRIFT,
        {"http": 200, "status": "order_created", "provider_called": True},
        _cosmetic_change_is_not_drift,
    ),
    _scenario(
        "QTE-013",
        "Quoting more units than exist is refused outright",
        METRIC_QUOTE_INTEGRITY,
        {"http": 409, "reason": "INSUFFICIENT_STOCK"},
        _stock_short_at_quote,
    ),
    _scenario(
        "QTE-014",
        "Quoting exactly the remaining stock is fine",
        METRIC_QUOTE_INTEGRITY,
        {"http": 200, "decision": "allow"},
        _stock_exactly_sufficient,
    ),
    _scenario(
        "QTE-015",
        "Stock falling between quote and order stops the provider call",
        METRIC_QUOTE_INTEGRITY,
        {
            "http": 409,
            "reason": "INSUFFICIENT_STOCK",
            "provider_called": False,
            "transactions": 0,
        },
        _stock_falls_between_quote_and_order,
        unsafe_if=MUST_NOT_CALL_PROVIDER,
    ),
    _scenario(
        "QTE-016",
        "Stock falling between order and payment stops settlement, and moves "
        "no money",
        METRIC_QUOTE_INTEGRITY,
        {
            "http": 409,
            "reason": "INSUFFICIENT_STOCK",
            "status": "failed",
            "balance_changed": False,
            "stock": 2,
        },
        _stock_falls_between_order_and_payment,
        unsafe_if=MUST_NOT_MOVE_MONEY + MUST_NOT_PAY,
    ),
]
