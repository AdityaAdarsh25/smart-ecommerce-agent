"""Every scenario, in report order.

Assembled here rather than discovered dynamically, so the suite's contents
are a fact you can read rather than a side effect of what files happen to
be on disk.
"""

from evaluation.scenarios import (
    adversarial,
    ai,
    approval,
    duplicates,
    payment,
    policy,
    quote,
)

ALL_SCENARIOS = [
    *policy.SCENARIOS,
    *quote.SCENARIOS,
    *duplicates.SCENARIOS,
    *approval.SCENARIOS,
    *ai.SCENARIOS,
    *payment.SCENARIOS,
    *adversarial.SCENARIOS,
]


def _assert_ids_are_unique():
    """A duplicated id would silently overwrite a result in the report."""
    seen = set()
    for scenario in ALL_SCENARIOS:
        if scenario.id in seen:
            raise ValueError(f"Duplicate scenario id: {scenario.id}")
        seen.add(scenario.id)


_assert_ids_are_unique()


__all__ = ["ALL_SCENARIOS"]
