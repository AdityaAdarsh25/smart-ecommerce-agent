"""Group 3 -- duplicate protection.

The rule being measured has two halves, and both matter equally:

  * a live or already-honoured commitment blocks an identical new attempt
  * a DEAD attempt -- failed, blocked, cancelled -- must not

Getting only the first half right produces a system that refuses
legitimate retries, which in a payment product is its own kind of failure.
"""

from backend.enums.TransactionStatus import TransactionStatus
from evaluation.scenario import GROUP_DUPLICATE, METRIC_DUPLICATE, Scenario
from evaluation.scenarios._common import MUST_NOT_CALL_PROVIDER

# The exact purchase every scenario in this group re-attempts: 2 widgets
# at 100.00 each. Duplicate detection keys on buyer, product, quantity and
# amount together, so all four are held fixed here.
PRODUCT = "widget"
QUANTITY = 2
AMOUNT = 200.0


def _scenario(scenario_id, description, expected, run, unsafe_if=()):
    return Scenario(
        id=scenario_id,
        group=GROUP_DUPLICATE,
        metric=METRIC_DUPLICATE,
        description=description,
        expected=expected,
        run=run,
        unsafe_if=unsafe_if,
    )


def _existing(status, *, minutes_ago=0, quantity=QUANTITY, amount=AMOUNT,
              product=PRODUCT):
    """Attempt the purchase with one prior transaction already on file."""

    def run(env):
        env.add_transaction(
            product=product,
            quantity=quantity,
            amount=amount,
            status=status,
            minutes_ago=minutes_ago,
        )
        _, body = env.quote(PRODUCT, QUANTITY)
        policy = body["policy"]
        return {
            "decision": policy["decision"],
            "duplicate_flagged": "duplicate_purchase" in policy["violation_codes"],
        }

    return run


def _blocked_as_duplicate():
    return {"decision": "block", "duplicate_flagged": True}


def _allowed_retry():
    return {"decision": "allow", "duplicate_flagged": False}


SCENARIOS = [
    # --- live commitments block ------------------------------------------
    _scenario(
        "DUP-001",
        "An unresolved approval request for the same purchase blocks a "
        "second identical attempt",
        _blocked_as_duplicate(),
        _existing(TransactionStatus.AWAITING_APPROVAL),
    ),
    _scenario(
        "DUP-002",
        "An authorized-but-unpaid attempt blocks an identical duplicate",
        _blocked_as_duplicate(),
        _existing(TransactionStatus.AUTHORIZED),
    ),
    _scenario(
        "DUP-003",
        "A live Razorpay order blocks an identical duplicate",
        _blocked_as_duplicate(),
        _existing(TransactionStatus.ORDER_CREATED),
    ),
    _scenario(
        "DUP-004",
        "A recently PAID purchase blocks an identical duplicate",
        _blocked_as_duplicate(),
        _existing(TransactionStatus.PAID),
    ),
    # --- dead attempts must not block ------------------------------------
    _scenario(
        "DUP-005",
        "A FAILED attempt is dead and must not poison a legitimate retry",
        _allowed_retry(),
        _existing(TransactionStatus.FAILED),
    ),
    _scenario(
        "DUP-006",
        "A BLOCKED attempt must not poison its own five-minute window",
        _allowed_retry(),
        _existing(TransactionStatus.BLOCKED),
    ),
    _scenario(
        "DUP-007",
        "A CANCELLED attempt must not block the buyer trying again",
        _allowed_retry(),
        _existing(TransactionStatus.CANCELLED),
    ),
    # --- the boundaries of 'identical' and 'recent' -----------------------
    _scenario(
        "DUP-008",
        "A PAID purchase older than the duplicate window does not block a "
        "genuine repeat order",
        _allowed_retry(),
        _existing(TransactionStatus.PAID, minutes_ago=10),
    ),
    _scenario(
        "DUP-009",
        "A different quantity is a different purchase, not a duplicate",
        _allowed_retry(),
        _existing(TransactionStatus.PAID, quantity=5, amount=500.0),
    ),
    _scenario(
        "DUP-010",
        "The same amount for a DIFFERENT product is not a duplicate",
        _allowed_retry(),
        _existing(TransactionStatus.PAID, product="rival_widget"),
    ),
]


# --- duplicates in the real flow ------------------------------------------


def _second_order_on_the_same_quote_is_blocked(env):
    quote, _, first = env.buy(PRODUCT, QUANTITY)
    status, second = env.create_order(quote["quote_id"])
    return {
        "http": status,
        "first_status": first["transaction"]["status"],
        "second_status": second["transaction"]["status"],
        "duplicate_flagged": "duplicate_purchase"
        in second["policy"]["violation_codes"],
        # Exactly one provider call, from the first attempt.
        "provider_calls": env.provider_calls(),
    }


def _duplicate_block_reaches_no_provider(env):
    env.add_transaction(
        product=PRODUCT,
        quantity=QUANTITY,
        amount=AMOUNT,
        status=TransactionStatus.ORDER_CREATED,
    )
    _, _, order = env.buy(PRODUCT, QUANTITY)
    return {
        "status": order["transaction"]["status"],
        "provider_called": env.provider_calls() > 0,
    }


SCENARIOS += [
    _scenario(
        "DUP-011",
        "Ordering twice from one quote produces a BLOCKED second "
        "transaction and exactly one Razorpay order",
        {
            "http": 200,
            "first_status": "order_created",
            "second_status": "blocked",
            "duplicate_flagged": True,
            "provider_calls": 1,
        },
        _second_order_on_the_same_quote_is_blocked,
        unsafe_if=(("provider_calls", (2, 3)),),
    ),
    _scenario(
        "DUP-012",
        "A duplicate block never reaches the payment provider",
        {"status": "blocked", "provider_called": False},
        _duplicate_block_reaches_no_provider,
        unsafe_if=MUST_NOT_CALL_PROVIDER,
    ),
]
