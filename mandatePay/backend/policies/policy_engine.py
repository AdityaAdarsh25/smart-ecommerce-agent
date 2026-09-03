"""Deterministic transaction policy engine.

This module is the ONLY thing permitted to decide whether an agent is
authorized to attempt a purchase. It answers exactly one question:

    "Is the agent authorized to attempt this purchase?"

Hard rules of this module:

  * It is pure, deterministic and side-effect free.
  * It returns a `PolicyResult`, never a `TransactionStatus`. Authorization
    is not payment state.
  * No LLM output reaches it except as already-validated numeric fields.
  * It evaluates EVERY applicable rule and collects findings. It never
    returns early just because an approval threshold was crossed -- doing
    so previously let an over-budget purchase be rescued by a human
    approver.

Delegated authority is expressed by two distinct thresholds:

    autonomous_limit            the agent may act alone at or below this
    absolute_transaction_limit  the mandate ceiling; above it the purchase
                                is BLOCKED and no human may approve it

Everything between them is the human-approval band.
"""

from typing import Optional

from backend.models.PolicyResult import PolicyResult

# Stable reason codes. These are the audit/demo vocabulary and must not be
# renamed casually -- stored transaction reasons refer to them.
RULE_AMOUNT_POSITIVE = "amount_positive"
RULE_BUYER_BALANCE = "buyer_balance"
RULE_MONTHLY_CAP = "monthly_cap"
RULE_DUPLICATE = "duplicate_purchase"
RULE_AUTONOMOUS_LIMIT = "autonomous_limit"
RULE_ABSOLUTE_LIMIT = "absolute_transaction_limit"
RULE_MERCHANT_ALLOWED = "merchant_allowed"
RULE_CATEGORY_ALLOWED = "category_allowed"
RULE_STOCK_AVAILABLE = "stock_available"


def evaluate_transaction(
    *,
    amount: float,
    balance: float,
    autonomous_limit: float,
    monthly_cap: float,
    monthly_spent_so_far: float,
    is_duplicate: bool,
    absolute_transaction_limit: Optional[float] = None,
    merchant_allowed: Optional[bool] = None,
    category_allowed: Optional[bool] = None,
    stock_sufficient: Optional[bool] = None,
    human_approved: bool = False,
) -> PolicyResult:
    """Evaluate every applicable policy rule and return the verdict.

    Arguments are keyword-only on purpose: the previous positional signature
    took six interchangeable floats, which is an easy way to silently swap a
    balance for a cap.

    `monthly_spent_so_far` must be computed from verified payments only.

    The optional arguments are mandate facts a caller may not always be able
    to establish. `None` means "this rule does not apply to this evaluation",
    and the rule is then absent from `evaluated_rules` rather than being
    silently recorded as a pass. Every call on the purchase path supplies
    all of them.

    `human_approved` records that a human has already granted the
    autonomous-threshold exception. It suppresses ONLY the
    `autonomous_limit` approval reason -- which is what stops an approved
    transaction being sent round the approval loop forever. It can never
    suppress a hard violation, and in particular it cannot reach
    `absolute_transaction_limit`.
    """

    hard_violations: list[str] = []
    approval_reasons: list[str] = []
    violation_codes: list[str] = []
    approval_codes: list[str] = []
    evaluated_rules: list[str] = []

    def hard(rule: str, message: str) -> None:
        hard_violations.append(message)
        violation_codes.append(rule)
        evaluated_rules.append(f"{rule}: HARD_VIOLATION")

    def approval(rule: str, message: str) -> None:
        approval_reasons.append(message)
        approval_codes.append(rule)
        evaluated_rules.append(f"{rule}: APPROVAL_REQUIRED")

    def passed(rule: str) -> None:
        evaluated_rules.append(f"{rule}: pass")

    # --- Hard rule: the amount must be real money ------------------------
    # Defensive depth. The request schema already rejects quantity < 1, but
    # a non-positive amount must never be able to reach a payment provider
    # even if it arrives by some other path.
    if amount <= 0:
        hard(
            RULE_AMOUNT_POSITIVE,
            f"Transaction amount must be greater than zero (got {amount:.2f}).",
        )
    else:
        passed(RULE_AMOUNT_POSITIVE)

    # --- Hard rule: buyer must be able to fund it ------------------------
    # Balance is an independent hard check, deliberately not folded into any
    # mandate limit: a mandate delegates authority, not funds.
    if amount > balance:
        hard(
            RULE_BUYER_BALANCE,
            f"Amount {amount:.2f} exceeds buyer balance {balance:.2f}.",
        )
    else:
        passed(RULE_BUYER_BALANCE)

    # --- Hard rule: the mandate absolute ceiling -------------------------
    # Above this the mandate does not delegate the authority at all, so
    # there is nothing for a human approver to approve.
    if absolute_transaction_limit is not None:
        if amount > absolute_transaction_limit:
            hard(
                RULE_ABSOLUTE_LIMIT,
                f"Amount {amount:.2f} is above the mandate absolute transaction "
                f"limit of {absolute_transaction_limit:.2f}. Human approval "
                "cannot override this.",
            )
        else:
            passed(RULE_ABSOLUTE_LIMIT)

    # --- Hard rule: monthly cap ------------------------------------------
    projected_spend = monthly_spent_so_far + amount
    if projected_spend > monthly_cap:
        hard(
            RULE_MONTHLY_CAP,
            f"Amount {amount:.2f} would take monthly spend to "
            f"{projected_spend:.2f}, over the cap of {monthly_cap:.2f}.",
        )
    else:
        passed(RULE_MONTHLY_CAP)

    # --- Hard rule: merchant permitted by the mandate --------------------
    if merchant_allowed is not None:
        if not merchant_allowed:
            hard(
                RULE_MERCHANT_ALLOWED,
                "The mandate does not permit purchases from this merchant.",
            )
        else:
            passed(RULE_MERCHANT_ALLOWED)

    # --- Hard rule: category permitted by the mandate --------------------
    if category_allowed is not None:
        if not category_allowed:
            hard(
                RULE_CATEGORY_ALLOWED,
                "The mandate does not permit purchases in this product category.",
            )
        else:
            passed(RULE_CATEGORY_ALLOWED)

    # --- Hard rule: the goods must actually exist ------------------------
    if stock_sufficient is not None:
        if not stock_sufficient:
            hard(
                RULE_STOCK_AVAILABLE,
                "Stock is insufficient for the requested quantity.",
            )
        else:
            passed(RULE_STOCK_AVAILABLE)

    # --- Hard rule: duplicate protection ---------------------------------
    if is_duplicate:
        hard(
            RULE_DUPLICATE,
            "An identical purchase for this buyer and product is already "
            "pending, authorized or recently paid.",
        )
    else:
        passed(RULE_DUPLICATE)

    # --- Approval rule: autonomous spending threshold --------------------
    # Recorded even when a hard violation exists, so the audit trail shows
    # everything that was true -- but it can never rescue a blocked purchase.
    if amount > autonomous_limit:
        if human_approved:
            # Already satisfied. Recorded, not re-raised: re-raising it is
            # precisely the infinite REQUIRE_APPROVAL loop.
            evaluated_rules.append(
                f"{RULE_AUTONOMOUS_LIMIT}: satisfied_by_human_approval"
            )
        else:
            approval(
                RULE_AUTONOMOUS_LIMIT,
                f"Amount {amount:.2f} is above the autonomous spending threshold "
                f"of {autonomous_limit:.2f} and needs human approval.",
            )
    else:
        passed(RULE_AUTONOMOUS_LIMIT)

    return PolicyResult.from_findings(
        hard_violations=hard_violations,
        approval_reasons=approval_reasons,
        evaluated_rules=evaluated_rules,
        violation_codes=violation_codes,
        approval_codes=approval_codes,
    )
