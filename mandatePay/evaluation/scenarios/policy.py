"""Group 1 -- policy and mandate.

The delegated-authority rules, one threshold at a time and then several at
once. Every expected decision and every expected reason code below was
written by hand from the mandate definition, not read out of the engine.
"""

from backend.enums.TransactionStatus import TransactionStatus
from evaluation.scenario import (
    GROUP_POLICY,
    METRIC_APPROVAL_ESCALATION,
    METRIC_HARD_BLOCK,
    METRIC_POLICY_DECISION,
    Scenario,
)
from evaluation.scenarios._common import (
    hard_block_unsafe,
    order_facts,
    policy_facts,
)


def _quote_case(
    scenario_id,
    description,
    *,
    metric,
    product,
    quantity,
    expected,
    setup=None,
    unsafe_if=(),
):
    """Shape the world, ask for a price, and look at the verdict."""

    def run(env):
        if setup is not None:
            setup(env)
        return policy_facts(*env.quote(product, quantity))

    return Scenario(
        id=scenario_id,
        group=GROUP_POLICY,
        metric=metric,
        description=description,
        expected=expected,
        run=run,
        unsafe_if=unsafe_if,
    )


def _allow(violations=None, approvals=None):
    return {
        "http": 200,
        "decision": "allow",
        "violations": violations or [],
        "approvals": approvals or [],
    }


def _approval(codes=("autonomous_limit",)):
    return {
        "http": 200,
        "decision": "require_approval",
        "violations": [],
        "approvals": sorted(codes),
    }


def _block(violations, approvals=()):
    return {
        "http": 200,
        "decision": "block",
        "violations": sorted(violations),
        "approvals": sorted(approvals),
    }


SCENARIOS = [
    # --- the autonomous threshold ---------------------------------------
    _quote_case(
        "POL-001",
        "200 is well under the 1000 autonomous limit, so the agent may act alone",
        metric=METRIC_POLICY_DECISION,
        product="widget",
        quantity=2,
        expected=_allow(),
    ),
    _quote_case(
        "POL-002",
        "Exactly at the autonomous limit is still autonomous -- the rule is "
        "'above', not 'at'",
        metric=METRIC_POLICY_DECISION,
        product="widget",
        quantity=10,  # 1000.00
        expected=_allow(),
    ),
    _quote_case(
        "POL-003",
        "One rupee past the autonomous limit needs a human",
        metric=METRIC_APPROVAL_ESCALATION,
        product="widget",
        quantity=11,  # 1100.00
        expected=_approval(),
    ),
    _quote_case(
        "POL-004",
        "Comfortably inside the approval band",
        metric=METRIC_APPROVAL_ESCALATION,
        product="machine",
        quantity=1,  # 2000.00
        expected=_approval(),
    ),
    _quote_case(
        "POL-005",
        "Exactly at the absolute ceiling is approvable, not blocked",
        metric=METRIC_APPROVAL_ESCALATION,
        product="machine",
        quantity=1,  # 2000.00
        setup=lambda env: env.set_mandate(absolute_transaction_limit=2000.0),
        expected=_approval(),
    ),
    _quote_case(
        "POL-006",
        "Above the absolute ceiling the mandate delegates nothing at all",
        metric=METRIC_HARD_BLOCK,
        product="luxury",
        quantity=1,  # 60000.00, ceiling is 50000
        expected=_block(["absolute_transaction_limit"], ["autonomous_limit"]),
        unsafe_if=hard_block_unsafe(),
    ),
    _quote_case(
        "POL-007",
        "Far above the ceiling trips the ceiling, the balance and the monthly "
        "cap at once, and reports all three",
        metric=METRIC_HARD_BLOCK,
        product="luxury",
        # 600000.00 against a 500000 balance, a 50000 ceiling and a
        # 100000 monthly cap -- every one of them is genuinely breached.
        quantity=10,
        expected=_block(
            ["absolute_transaction_limit", "buyer_balance", "monthly_cap"],
            ["autonomous_limit"],
        ),
        unsafe_if=hard_block_unsafe(),
    ),
    _quote_case(
        "POL-008",
        "A hard violation outranks an approval reason: no human may approve "
        "past the ceiling",
        metric=METRIC_HARD_BLOCK,
        product="machine",
        quantity=1,  # 2000.00
        setup=lambda env: env.set_mandate(absolute_transaction_limit=500.0),
        # The approval reason is still RECORDED -- the audit trail shows
        # everything that was true -- it just cannot rescue the purchase.
        expected=_block(["absolute_transaction_limit"], ["autonomous_limit"]),
        unsafe_if=hard_block_unsafe(),
    ),
    # --- monthly cap -----------------------------------------------------
    _quote_case(
        "POL-009",
        "Verified spend this month plus this purchase would breach the cap",
        metric=METRIC_HARD_BLOCK,
        product="widget",
        quantity=2,  # 200.00 on top of 900.00 already paid, cap 1000
        setup=lambda env: (
            env.set_mandate(monthly_cap=1000.0),
            env.add_transaction(
                product="gadget",
                quantity=1,
                amount=900.0,
                status=TransactionStatus.PAID,
            ),
        ),
        expected=_block(["monthly_cap"]),
        unsafe_if=hard_block_unsafe(),
    ),
    _quote_case(
        "POL-010",
        "Landing exactly on the cap is permitted",
        metric=METRIC_POLICY_DECISION,
        product="widget",
        quantity=2,  # 200.00 on top of 800.00, cap 1000
        setup=lambda env: (
            env.set_mandate(monthly_cap=1000.0),
            env.add_transaction(
                product="gadget",
                quantity=1,
                amount=800.0,
                status=TransactionStatus.PAID,
            ),
        ),
        expected=_allow(),
    ),
    _quote_case(
        "POL-011",
        "An authorized-but-unpaid attempt has consumed no allowance",
        metric=METRIC_POLICY_DECISION,
        product="widget",
        quantity=2,
        setup=lambda env: (
            env.set_mandate(monthly_cap=1000.0),
            env.add_transaction(
                product="gadget",
                quantity=1,
                amount=900.0,
                status=TransactionStatus.AUTHORIZED,
            ),
        ),
        expected=_allow(),
    ),
    _quote_case(
        "POL-012",
        "A blocked attempt has consumed no allowance either",
        metric=METRIC_POLICY_DECISION,
        product="widget",
        quantity=2,
        setup=lambda env: (
            env.set_mandate(monthly_cap=1000.0),
            env.add_transaction(
                product="gadget",
                quantity=1,
                amount=900.0,
                status=TransactionStatus.BLOCKED,
            ),
        ),
        expected=_allow(),
    ),
    # --- balance ---------------------------------------------------------
    _quote_case(
        "POL-013",
        "A mandate delegates authority, not funds: an unaffordable purchase "
        "is blocked",
        metric=METRIC_HARD_BLOCK,
        product="widget",
        quantity=2,  # 200.00 against a balance of 100.00
        setup=lambda env: env.set_balance(100.0),
        expected=_block(["buyer_balance"]),
        unsafe_if=hard_block_unsafe(),
    ),
    _quote_case(
        "POL-014",
        "Spending the balance down to exactly zero is permitted",
        metric=METRIC_POLICY_DECISION,
        product="widget",
        quantity=2,
        setup=lambda env: env.set_balance(200.0),
        expected=_allow(),
    ),
    # --- merchant --------------------------------------------------------
    _quote_case(
        "POL-015",
        "A permitted merchant passes",
        metric=METRIC_POLICY_DECISION,
        product="widget",
        quantity=2,
        setup=lambda env: env.set_mandate(
            allowed_merchants=[env.ids["merchant_main"]]
        ),
        expected=_allow(),
    ),
    _quote_case(
        "POL-016",
        "A merchant outside a strict allow-list is blocked",
        metric=METRIC_HARD_BLOCK,
        product="widget",
        quantity=2,
        setup=lambda env: env.set_mandate(
            allowed_merchants=[env.ids["merchant_other"]]
        ),
        expected=_block(["merchant_allowed"]),
        unsafe_if=hard_block_unsafe(),
    ),
    _quote_case(
        "POL-017",
        "An EMPTY merchant allow-list means unrestricted, not 'nothing allowed'",
        metric=METRIC_POLICY_DECISION,
        product="rival_widget",
        quantity=2,
        setup=lambda env: env.set_mandate(allowed_merchants=[]),
        expected=_allow(),
    ),
    # --- category --------------------------------------------------------
    _quote_case(
        "POL-018",
        "A permitted category passes",
        metric=METRIC_POLICY_DECISION,
        product="widget",
        quantity=2,
        setup=lambda env: env.set_mandate(allowed_categories=["gadgets"]),
        expected=_allow(),
    ),
    _quote_case(
        "POL-019",
        "A category outside a strict allow-list is blocked",
        metric=METRIC_HARD_BLOCK,
        product="widget",
        quantity=2,
        setup=lambda env: env.set_mandate(allowed_categories=["machinery"]),
        expected=_block(["category_allowed"]),
        unsafe_if=hard_block_unsafe(),
    ),
    _quote_case(
        "POL-020",
        "A product carrying NO category cannot satisfy a category allow-list",
        metric=METRIC_HARD_BLOCK,
        product="uncategorised",
        quantity=1,
        setup=lambda env: env.set_mandate(allowed_categories=["gadgets"]),
        expected=_block(["category_allowed"]),
        unsafe_if=hard_block_unsafe(),
    ),
    # --- several at once -------------------------------------------------
    _quote_case(
        "POL-021",
        "Three simultaneous hard violations are all reported, not just the first",
        metric=METRIC_HARD_BLOCK,
        product="widget",
        quantity=2,  # 200.00
        setup=lambda env: (
            env.set_balance(50.0),
            env.set_mandate(
                absolute_transaction_limit=100.0,
                allowed_merchants=[env.ids["merchant_other"]],
            ),
        ),
        # 200.00 is below the 1000 autonomous threshold, so there is no
        # approval reason here -- only the three hard failures.
        expected=_block(
            ["absolute_transaction_limit", "buyer_balance", "merchant_allowed"]
        ),
        unsafe_if=hard_block_unsafe(),
    ),
    _quote_case(
        "POL-022",
        "Merchant and category can fail together",
        metric=METRIC_HARD_BLOCK,
        product="widget",
        quantity=2,
        setup=lambda env: env.set_mandate(
            allowed_merchants=[env.ids["merchant_other"]],
            allowed_categories=["machinery"],
        ),
        expected=_block(["category_allowed", "merchant_allowed"]),
        unsafe_if=hard_block_unsafe(),
    ),
    _quote_case(
        "POL-023",
        "Stock is a hard requirement checked before anything is priced",
        metric=METRIC_HARD_BLOCK,
        product="scarce",
        quantity=5,  # only 1 in stock
        expected={
            "http": 409,
            "decision": None,
            "reason": "INSUFFICIENT_STOCK",
        },
        unsafe_if=(("decision", ("allow", "require_approval")),),
    ),
]


# --- the block must also stop the provider --------------------------------


def _blocked_order_reaches_no_provider(env):
    env.set_mandate(absolute_transaction_limit=100.0)
    quote, _, order = env.buy("widget", 2)
    facts = order_facts(env, 200, order)
    facts["decision"] = order["policy"]["decision"]
    return facts


SCENARIOS.append(
    Scenario(
        id="POL-024",
        group=GROUP_POLICY,
        metric=METRIC_HARD_BLOCK,
        description="A BLOCKED purchase records a transaction and never "
        "contacts Razorpay",
        expected={
            "http": 200,
            "decision": "block",
            "status": "blocked",
            "razorpay_order_id": None,
            "provider_called": False,
        },
        run=_blocked_order_reaches_no_provider,
        unsafe_if=hard_block_unsafe(),
    )
)
