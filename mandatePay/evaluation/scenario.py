"""What a scenario is, and what makes one meaningful.

The anti-tautology rule is enforced by this file's shape. A `Scenario`
carries its `expected` outcome as literal data written by hand:

    expected={"decision": "block", "violation": "absolute_transaction_limit"}

The production system is then run and its observations compared against
that literal. Nowhere in the comparison is the code under test consulted
for what the answer should have been, so a bug that flips a decision flips
the result rather than moving the goalposts with it.

`unsafe_if` is separate from `expected` on purpose. Failing a scenario
means the system was wrong; tripping `unsafe_if` means it was wrong in the
specific direction that lets money move when it should not. Those are
different severities and the report keeps them apart.
"""

from dataclasses import dataclass, field
from typing import Any, Callable

# Scenario groups, in report order.
GROUP_POLICY = "policy_mandate"
GROUP_QUOTE = "quote_price_stock"
GROUP_DUPLICATE = "duplicates"
GROUP_APPROVAL = "approval"
GROUP_PAYMENT = "payment"
GROUP_AI = "ai_intent_selection"
GROUP_ADVERSARIAL = "adversarial_catalogue"

GROUP_ORDER = [
    GROUP_POLICY,
    GROUP_QUOTE,
    GROUP_DUPLICATE,
    GROUP_APPROVAL,
    GROUP_AI,
    GROUP_PAYMENT,
    GROUP_ADVERSARIAL,
]

# Metric buckets. A scenario names the one property it is really about, so
# a category rate answers a question ("does hard blocking work?") rather
# than averaging unrelated things together.
METRIC_POLICY_DECISION = "policy_decision_correctness"
METRIC_HARD_BLOCK = "hard_block_correctness"
METRIC_APPROVAL_ESCALATION = "approval_escalation_correctness"
METRIC_APPROVAL_REVALIDATION = "approval_revalidation_correctness"
METRIC_PRICE_DRIFT = "price_drift_detection"
METRIC_QUOTE_INTEGRITY = "quote_integrity"
METRIC_DUPLICATE = "duplicate_protection_correctness"
METRIC_PAYMENT_STATE = "payment_state_correctness"
METRIC_SELECTION = "clarification_selection_correctness"
METRIC_ADVERSARIAL = "adversarial_bypass_resistance"

METRIC_ORDER = [
    METRIC_POLICY_DECISION,
    METRIC_HARD_BLOCK,
    METRIC_APPROVAL_ESCALATION,
    METRIC_APPROVAL_REVALIDATION,
    METRIC_QUOTE_INTEGRITY,
    METRIC_PRICE_DRIFT,
    METRIC_DUPLICATE,
    METRIC_PAYMENT_STATE,
    METRIC_SELECTION,
    METRIC_ADVERSARIAL,
]


@dataclass(frozen=True)
class Scenario:
    """One controlled experiment.

    `run` receives a freshly built `Env` and returns a flat dict of
    observations. It asserts nothing -- judgement belongs to `expected`,
    so the same observations can be checked against several claims and a
    failure reports the actual value rather than an assertion message.
    """

    id: str
    group: str
    metric: str
    description: str
    expected: dict[str, Any]
    run: Callable[[Any], dict[str, Any]]

    # (observation key, values that would mean money escaped its controls).
    # Written per scenario, from the scenario's own intent -- never derived
    # from what the system did.
    unsafe_if: tuple[tuple[str, tuple], ...] = field(default_factory=tuple)


def compare(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    """Every expectation that did not hold, described plainly.

    A missing observation is a mismatch, not a pass: a scenario that
    silently stopped producing a fact must not read as a success.
    """
    mismatches = []
    for key, want in expected.items():
        if key not in actual:
            mismatches.append(f"{key}: expected {want!r}, but it was not observed")
        elif actual[key] != want:
            mismatches.append(f"{key}: expected {want!r}, got {actual[key]!r}")
    return mismatches


def unsafe_findings(scenario: Scenario, actual: dict[str, Any]) -> list[str]:
    """Every way this run let money move when it should not have."""
    findings = []
    for key, forbidden in scenario.unsafe_if:
        if key in actual and actual[key] in forbidden:
            findings.append(f"{key} was {actual[key]!r}, which must never happen here")
    return findings
