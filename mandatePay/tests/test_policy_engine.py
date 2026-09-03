"""Deterministic policy engine: precedence and rule correctness.

Pure unit tests. No database, no network.
"""

import pytest
from pydantic import ValidationError

from backend.enums.PolicyDecision import PolicyDecision
from backend.enums.TransactionStatus import TransactionStatus
from backend.models.PolicyResult import PolicyResult
from backend.policies.policy_engine import (
    RULE_AMOUNT_POSITIVE,
    RULE_AUTONOMOUS_LIMIT,
    RULE_BUYER_BALANCE,
    RULE_DUPLICATE,
    RULE_MONTHLY_CAP,
    evaluate_transaction,
)

ALL_RULES = {
    RULE_AMOUNT_POSITIVE,
    RULE_BUYER_BALANCE,
    RULE_MONTHLY_CAP,
    RULE_DUPLICATE,
    RULE_AUTONOMOUS_LIMIT,
}


def evaluate(**overrides):
    """Evaluate against a baseline that comfortably passes every rule."""
    params = {
        "amount": 500.0,
        "balance": 50000.0,
        "autonomous_limit": 1000.0,
        "monthly_cap": 5000.0,
        "monthly_spent_so_far": 0.0,
        "is_duplicate": False,
    }
    params.update(overrides)
    return evaluate_transaction(**params)


# --- 1. normal valid amount -> ALLOW ---------------------------------------


def test_normal_amount_is_allowed():
    result = evaluate(amount=500.0)
    assert result.decision is PolicyDecision.ALLOW
    assert result.hard_violations == []
    assert result.approval_reasons == []


# --- 2. above autonomous threshold only -> REQUIRE_APPROVAL ----------------


def test_above_autonomous_threshold_requires_approval():
    result = evaluate(amount=1500.0)
    assert result.decision is PolicyDecision.REQUIRE_APPROVAL
    assert result.hard_violations == []
    assert len(result.approval_reasons) == 1


# --- 3. monthly cap exceeded -> BLOCK --------------------------------------


def test_monthly_cap_exceeded_blocks():
    # Under the autonomous threshold, so the cap is the only finding.
    result = evaluate(amount=900.0, monthly_spent_so_far=4500.0)
    assert result.decision is PolicyDecision.BLOCK
    assert any("monthly spend" in v for v in result.hard_violations)
    assert result.approval_reasons == []


# --- 4. cap exceeded AND threshold exceeded -> BLOCK, never approval -------


def test_hard_block_outranks_approval_requirement():
    result = evaluate(amount=4000.0, monthly_spent_so_far=4500.0)

    assert result.decision is PolicyDecision.BLOCK
    assert result.decision is not PolicyDecision.REQUIRE_APPROVAL

    # The approval reason is still *recorded* -- the audit trail should show
    # everything that was true -- but it cannot rescue a blocked purchase.
    assert result.approval_reasons, "approval reason should still be recorded"
    assert result.hard_violations


# --- 5. duplicate AND threshold exceeded -> BLOCK --------------------------


def test_duplicate_with_threshold_exceeded_blocks():
    result = evaluate(amount=4000.0, is_duplicate=True)
    assert result.decision is PolicyDecision.BLOCK
    assert any("identical purchase" in v for v in result.hard_violations)
    # Previously the approval branch returned first and the duplicate check
    # was never reached at all.
    assert result.approval_reasons


# --- 6. insufficient balance -> BLOCK --------------------------------------


def test_insufficient_balance_blocks():
    result = evaluate(amount=900.0, balance=100.0)
    assert result.decision is PolicyDecision.BLOCK
    assert any("exceeds buyer balance" in v for v in result.hard_violations)


# --- 7 & 8. zero and negative amounts -> BLOCK -----------------------------


@pytest.mark.parametrize("amount", [0.0, -0.01, -1000.0])
def test_non_positive_amount_blocks(amount):
    result = evaluate(amount=amount)
    assert result.decision is PolicyDecision.BLOCK
    assert any("greater than zero" in v for v in result.hard_violations)


def test_negative_amount_is_not_rescued_by_other_rules_passing():
    # A negative amount trivially satisfies balance, cap and threshold. Only
    # the positivity rule stands between it and the payment provider.
    result = evaluate(amount=-1000.0)
    assert result.decision is PolicyDecision.BLOCK
    assert len(result.hard_violations) == 1


# --- Precedence / structure -----------------------------------------------


def test_every_rule_is_always_evaluated():
    """No early returns: all five rules appear in every result."""
    for result in [
        evaluate(),
        evaluate(amount=1500.0),
        evaluate(amount=-5.0, balance=0.0, monthly_cap=0.0, is_duplicate=True),
        evaluate(amount=4000.0, monthly_spent_so_far=4500.0, is_duplicate=True),
    ]:
        assert len(result.evaluated_rules) == len(ALL_RULES)
        recorded = {entry.split(":")[0] for entry in result.evaluated_rules}
        assert recorded == ALL_RULES


def test_all_findings_are_collected_not_short_circuited():
    """Over balance, over cap, duplicate, and over the approval threshold --
    all four are reported, not just the first one hit."""
    result = evaluate(
        amount=6000.0,
        balance=1000.0,
        monthly_cap=2000.0,
        is_duplicate=True,
    )
    assert result.decision is PolicyDecision.BLOCK
    assert len(result.hard_violations) == 3  # balance, monthly cap, duplicate
    assert len(result.approval_reasons) == 1  # autonomous threshold


def test_engine_returns_policy_result_not_transaction_status():
    result = evaluate()
    assert isinstance(result, PolicyResult)
    assert isinstance(result.decision, PolicyDecision)
    assert not isinstance(result.decision, TransactionStatus)
    assert result.decision.value not in {s.value for s in TransactionStatus}


def test_policy_result_cannot_contradict_its_findings():
    """The ALLOW-with-violations shape is structurally unconstructible."""
    with pytest.raises(ValidationError):
        PolicyResult(
            decision=PolicyDecision.ALLOW,
            hard_violations=["insufficient balance"],
        )
    with pytest.raises(ValidationError):
        PolicyResult(
            decision=PolicyDecision.REQUIRE_APPROVAL,
            hard_violations=["insufficient balance"],
            approval_reasons=["over threshold"],
        )


def test_policy_decision_and_transaction_status_share_no_values():
    """The two vocabularies must not overlap, so neither can stand in for
    the other. In particular nothing in PolicyDecision means 'paid'."""
    assert {d.value for d in PolicyDecision}.isdisjoint(
        {s.value for s in TransactionStatus}
    )
    assert not hasattr(TransactionStatus, "COMPLETED")
