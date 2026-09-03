"""Delegated mandate authority: the full deterministic rule set.

Exercised through the real HTTP path wherever possible, so these prove the
wiring as well as the engine -- a rule the engine knows about but the route
never supplies is a rule that does not exist.

The headline shape, with autonomous 3000 / absolute 6000:

    2500 -> ALLOW              the agent acts alone
    4000 -> REQUIRE_APPROVAL   a human must say yes
    7000 -> BLOCK              no human can say yes
"""

from backend.databases.buyer_db import buyer_db
from backend.enums.PolicyDecision import PolicyDecision
from backend.enums.TransactionStatus import TransactionStatus
from backend.policies.policy_engine import (
    RULE_ABSOLUTE_LIMIT,
    RULE_AUTONOMOUS_LIMIT,
    RULE_BUYER_BALANCE,
    RULE_CATEGORY_ALLOWED,
    RULE_MERCHANT_ALLOWED,
    RULE_MONTHLY_CAP,
    evaluate_transaction,
)

# The widget costs 100.00, so quantity is the amount in hundreds of rupees.
UNIT = 100.0


def shape(set_mandate, **overrides):
    """The demo mandate: autonomous 3000, absolute 6000, cap out of the way."""
    params = {
        "autonomous_limit": 3000.0,
        "absolute_transaction_limit": 6000.0,
        "monthly_cap": 50000.0,
    }
    params.update(overrides)
    return set_mandate(**params)


def attempt(quote_for, order_from_quote, quantity, product="widget_id"):
    quote = quote_for(quantity=quantity, product=product)
    return order_from_quote(quote["quote_id"])


# --- 1. under the autonomous limit -> ALLOW --------------------------------


def test_under_autonomous_limit_is_allowed(
    set_mandate, quote_for, order_from_quote, fake_razorpay
):
    shape(set_mandate)
    body = attempt(quote_for, order_from_quote, 25)  # 2500.00

    assert body["policy"]["decision"] == PolicyDecision.ALLOW.value
    assert body["policy"]["hard_violations"] == []
    assert body["policy"]["approval_reasons"] == []
    assert body["transaction"]["status"] == TransactionStatus.ORDER_CREATED.value
    assert body["approval"] is None
    # ALLOW authorizes an attempt. It is still not PAID.
    assert body["transaction"]["razorpay_payment_id"] is None


# --- 2. over autonomous, under absolute -> REQUIRE_APPROVAL ----------------


def test_over_autonomous_but_under_absolute_requires_approval(
    set_mandate, quote_for, order_from_quote, fake_razorpay
):
    shape(set_mandate)
    body = attempt(quote_for, order_from_quote, 40)  # 4000.00

    assert body["policy"]["decision"] == PolicyDecision.REQUIRE_APPROVAL.value
    assert body["policy"]["hard_violations"] == []
    assert body["policy"]["approval_codes"] == [RULE_AUTONOMOUS_LIMIT]
    assert body["transaction"]["status"] == TransactionStatus.AWAITING_APPROVAL.value
    # The provider is not contacted for something a human has not allowed.
    assert fake_razorpay.orders_created == []


# --- 3. over the absolute limit -> BLOCK -----------------------------------


def test_over_absolute_limit_blocks(
    set_mandate, quote_for, order_from_quote, fake_razorpay
):
    shape(set_mandate)
    body = attempt(quote_for, order_from_quote, 70)  # 7000.00

    assert body["policy"]["decision"] == PolicyDecision.BLOCK.value
    assert RULE_ABSOLUTE_LIMIT in body["policy"]["violation_codes"]
    assert body["transaction"]["status"] == TransactionStatus.BLOCKED.value
    assert fake_razorpay.orders_created == []
    # Nothing to approve: the mandate never delegated this much authority.
    assert body["approval"] is None


# --- 4. monthly cap exceeded -> BLOCK --------------------------------------


def test_monthly_cap_exceeded_blocks(
    set_mandate, quote_for, order_from_quote, add_transaction, seed, fake_razorpay
):
    shape(set_mandate, monthly_cap=3000.0)
    add_transaction(
        buyer_id=seed["buyer_id"],
        product_id=seed["gadget_id"],
        quantity=10,
        amount=2500.0,
        status=TransactionStatus.PAID,
    )

    body = attempt(quote_for, order_from_quote, 10)  # 1000.00, cap allows 500

    assert body["policy"]["decision"] == PolicyDecision.BLOCK.value
    assert RULE_MONTHLY_CAP in body["policy"]["violation_codes"]
    assert fake_razorpay.orders_created == []


# --- 5. insufficient balance -> BLOCK --------------------------------------


def test_insufficient_balance_blocks(
    set_mandate, quote_for, order_from_quote, db_session, seed, fake_razorpay
):
    shape(set_mandate)
    buyer = db_session.get(buyer_db, seed["buyer_id"])
    buyer.balance = 500.0
    db_session.commit()

    body = attempt(quote_for, order_from_quote, 25)  # 2500.00

    assert body["policy"]["decision"] == PolicyDecision.BLOCK.value
    assert RULE_BUYER_BALANCE in body["policy"]["violation_codes"]
    # Balance is an independent hard check: the mandate limits were fine.
    assert RULE_ABSOLUTE_LIMIT not in body["policy"]["violation_codes"]
    assert fake_razorpay.orders_created == []


# --- 6. forbidden merchant -> BLOCK ----------------------------------------


def test_forbidden_merchant_blocks(
    set_mandate, quote_for, order_from_quote, seed, fake_razorpay
):
    shape(set_mandate, allowed_merchants=[seed["other_merchant_id"]])

    body = attempt(quote_for, order_from_quote, 25)

    assert body["policy"]["decision"] == PolicyDecision.BLOCK.value
    assert RULE_MERCHANT_ALLOWED in body["policy"]["violation_codes"]
    assert fake_razorpay.orders_created == []


def test_permitted_merchant_passes(
    set_mandate, quote_for, order_from_quote, seed, fake_razorpay
):
    shape(set_mandate, allowed_merchants=[seed["merchant_id"]])

    body = attempt(quote_for, order_from_quote, 25)

    assert body["policy"]["decision"] == PolicyDecision.ALLOW.value


def test_empty_merchant_allow_list_is_unrestricted(
    set_mandate, quote_for, order_from_quote, fake_razorpay
):
    """An empty list means the mandate places no restriction on merchants.
    A non-empty one is strict."""
    shape(set_mandate, allowed_merchants=[])

    body = attempt(quote_for, order_from_quote, 25)

    assert body["policy"]["decision"] == PolicyDecision.ALLOW.value


# --- 7. forbidden category -> BLOCK ----------------------------------------


def test_forbidden_category_blocks(
    set_mandate, quote_for, order_from_quote, fake_razorpay
):
    # The widget is in "gadgets".
    shape(set_mandate, allowed_categories=["groceries"])

    body = attempt(quote_for, order_from_quote, 25)

    assert body["policy"]["decision"] == PolicyDecision.BLOCK.value
    assert RULE_CATEGORY_ALLOWED in body["policy"]["violation_codes"]
    assert fake_razorpay.orders_created == []


def test_permitted_category_passes(
    set_mandate, quote_for, order_from_quote, fake_razorpay
):
    shape(set_mandate, allowed_categories=["gadgets"])

    body = attempt(quote_for, order_from_quote, 25)

    assert body["policy"]["decision"] == PolicyDecision.ALLOW.value


def test_uncategorised_product_cannot_satisfy_a_strict_category_list(
    set_mandate, quote_for, order_from_quote, db_session, seed, fake_razorpay
):
    """Strict means strict. A product carrying no category is not a
    silent pass through a category allow-list."""
    from backend.databases.product_db import product_db

    shape(set_mandate, allowed_categories=["gadgets"])
    product = db_session.get(product_db, seed["widget_id"])
    product.category = None
    db_session.commit()

    body = attempt(quote_for, order_from_quote, 25)

    assert body["policy"]["decision"] == PolicyDecision.BLOCK.value
    assert RULE_CATEGORY_ALLOWED in body["policy"]["violation_codes"]


# --- 8. multiple hard violations are all collected -------------------------


def test_multiple_hard_violations_are_collected(
    set_mandate, quote_for, order_from_quote, seed, fake_razorpay
):
    shape(
        set_mandate,
        absolute_transaction_limit=1000.0,
        monthly_cap=100.0,
        allowed_merchants=[seed["other_merchant_id"]],
        allowed_categories=["groceries"],
    )

    body = attempt(quote_for, order_from_quote, 25)  # 2500.00
    codes = body["policy"]["violation_codes"]

    assert body["policy"]["decision"] == PolicyDecision.BLOCK.value
    for expected in (
        RULE_ABSOLUTE_LIMIT,
        RULE_MONTHLY_CAP,
        RULE_MERCHANT_ALLOWED,
        RULE_CATEGORY_ALLOWED,
    ):
        assert expected in codes, f"{expected} was not evaluated or not reported"
    assert len(body["policy"]["hard_violations"]) == len(codes) == 4


def test_engine_collects_every_violation_without_short_circuiting():
    """Pure-engine mirror of the above: no rule is skipped because an
    earlier one already failed."""
    result = evaluate_transaction(
        amount=9000.0,
        balance=100.0,
        autonomous_limit=1000.0,
        absolute_transaction_limit=5000.0,
        monthly_cap=500.0,
        monthly_spent_so_far=0.0,
        is_duplicate=True,
        merchant_allowed=False,
        category_allowed=False,
        stock_sufficient=False,
    )

    assert result.decision is PolicyDecision.BLOCK
    assert len(result.violation_codes) == 7
    assert len(result.hard_violations) == 7


# --- 9. a hard violation alongside an approval condition -> BLOCK ----------


def test_hard_violation_with_approval_condition_blocks(
    set_mandate, quote_for, order_from_quote, fake_razorpay
):
    """4000 needs approval AND is in a forbidden category. Approval never
    gets a chance: the hard violation decides it."""
    shape(set_mandate, allowed_categories=["groceries"])

    body = attempt(quote_for, order_from_quote, 40)
    policy = body["policy"]

    assert policy["decision"] == PolicyDecision.BLOCK.value
    assert RULE_CATEGORY_ALLOWED in policy["violation_codes"]
    # The approval reason is still recorded -- the audit trail shows
    # everything that was true -- but it cannot rescue the purchase, and no
    # approval request is opened for something no human may approve.
    assert policy["approval_codes"] == [RULE_AUTONOMOUS_LIMIT]
    assert body["transaction"]["status"] == TransactionStatus.BLOCKED.value
    assert body["approval"] is None
    assert fake_razorpay.orders_created == []


# --- Human approval cannot reach a hard rule, at the engine level ----------


def test_human_approval_suppresses_only_the_threshold_reason():
    approved = evaluate_transaction(
        amount=4000.0,
        balance=50000.0,
        autonomous_limit=3000.0,
        absolute_transaction_limit=6000.0,
        monthly_cap=50000.0,
        monthly_spent_so_far=0.0,
        is_duplicate=False,
        merchant_allowed=True,
        category_allowed=True,
        stock_sufficient=True,
        human_approved=True,
    )
    assert approved.decision is PolicyDecision.ALLOW
    assert approved.approval_reasons == []
    # Suppressed, not hidden: the audit trail says why.
    assert any(
        entry == f"{RULE_AUTONOMOUS_LIMIT}: satisfied_by_human_approval"
        for entry in approved.evaluated_rules
    )


def test_human_approval_cannot_reach_the_absolute_limit():
    result = evaluate_transaction(
        amount=7000.0,
        balance=50000.0,
        autonomous_limit=3000.0,
        absolute_transaction_limit=6000.0,
        monthly_cap=50000.0,
        monthly_spent_so_far=0.0,
        is_duplicate=False,
        merchant_allowed=True,
        category_allowed=True,
        stock_sufficient=True,
        human_approved=True,
    )
    assert result.decision is PolicyDecision.BLOCK
    assert RULE_ABSOLUTE_LIMIT in result.violation_codes


def test_human_approval_cannot_reach_any_hard_rule():
    for override in (
        {"balance": 10.0},
        {"monthly_spent_so_far": 49_999.0},
        {"merchant_allowed": False},
        {"category_allowed": False},
        {"stock_sufficient": False},
        {"is_duplicate": True},
        {"amount": -1.0},
    ):
        params = {
            "amount": 4000.0,
            "balance": 50000.0,
            "autonomous_limit": 3000.0,
            "absolute_transaction_limit": 6000.0,
            "monthly_cap": 50000.0,
            "monthly_spent_so_far": 0.0,
            "is_duplicate": False,
            "merchant_allowed": True,
            "category_allowed": True,
            "stock_sufficient": True,
            "human_approved": True,
        }
        params.update(override)
        result = evaluate_transaction(**params)
        assert result.decision is PolicyDecision.BLOCK, override
