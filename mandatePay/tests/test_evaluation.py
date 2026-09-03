"""The evaluation harness itself.

An evaluation suite is a measuring instrument, and an instrument nobody
checks is just a number generator. These tests check the three properties
that make its output worth quoting:

  * it runs offline, against fakes, with the transports severed
  * its expected labels are literal data, not a second call to the code
    under test
  * it actually detects a regression, rather than agreeing with whatever
    the system currently does
"""

import inspect
import json

import pytest

from evaluation.runner import (
    MAX_SCENARIOS,
    MIN_SCENARIOS,
    RESULTS_JSON,
    RESULTS_MD,
    run,
)
from evaluation.scenario import GROUP_ORDER, METRIC_ORDER, compare, unsafe_findings
from evaluation.scenarios import ALL_SCENARIOS


@pytest.fixture(scope="module")
def summary():
    """One full run, shared by every test in this file.

    Deliberately does NOT write results.json: the committed artefact is
    produced by `python run_evaluation.py`, and a test run must not
    silently overwrite it.
    """
    return run(write=False, quiet=True)


# ---------------------------------------------------------------------------
# Shape of the suite
# ---------------------------------------------------------------------------


def test_scenario_count_is_within_the_declared_range():
    assert MIN_SCENARIOS <= len(ALL_SCENARIOS) <= MAX_SCENARIOS


def test_scenario_ids_are_unique():
    ids = [scenario.id for scenario in ALL_SCENARIOS]
    assert len(ids) == len(set(ids))


def test_every_scenario_group_and_metric_is_a_known_one():
    for scenario in ALL_SCENARIOS:
        assert scenario.group in GROUP_ORDER, scenario.id
        assert scenario.metric in METRIC_ORDER, scenario.id


def test_every_group_is_populated():
    """All seven required groups are actually covered."""
    groups = {scenario.group for scenario in ALL_SCENARIOS}
    assert groups == set(GROUP_ORDER)


def test_every_scenario_has_a_description_and_expectations():
    for scenario in ALL_SCENARIOS:
        assert scenario.description.strip(), scenario.id
        assert scenario.expected, f"{scenario.id} asserts nothing"


# ---------------------------------------------------------------------------
# Anti-tautology
# ---------------------------------------------------------------------------


def test_expected_labels_are_literal_data_not_computed(summary):
    """Every expectation is a plain JSON-able value written by hand.

    A callable or an object here would be the door through which a
    scenario could compute its own expectation from the system it is
    supposed to be judging.
    """
    for scenario in ALL_SCENARIOS:
        for key, value in scenario.expected.items():
            assert not callable(value), f"{scenario.id}.{key} is computed"
            assert isinstance(
                value, (str, int, float, bool, list, tuple, type(None))
            ), f"{scenario.id}.{key} is a {type(value).__name__}"


def test_no_scenario_calls_the_policy_engine_to_decide_what_to_expect():
    """The bad pattern this suite exists to avoid:

        expected = evaluate_transaction(...)
        actual   = evaluate_transaction(...)
        assert expected == actual

    Checked by reading the scenario modules: none of them imports the
    engine, so none of them can derive a label from it.
    """
    import evaluation.scenarios as package

    forbidden = ("evaluate_transaction", "policy_engine", "PolicyResult.decide")
    for module_name in (
        "adversarial", "ai", "approval", "duplicates", "payment", "policy", "quote",
    ):
        module = __import__(
            f"evaluation.scenarios.{module_name}", fromlist=[module_name]
        )
        source = inspect.getsource(module)
        for symbol in forbidden:
            assert symbol not in source, f"{module_name} references {symbol}"
    assert package is not None


def test_a_mismatch_is_reported_rather_than_absorbed():
    mismatches = compare({"decision": "block"}, {"decision": "allow"})
    assert mismatches and "expected 'block'" in mismatches[0]


def test_a_missing_observation_is_a_mismatch_not_a_pass():
    """A scenario that stops producing a fact must fail, not pass silently."""
    mismatches = compare({"decision": "block"}, {})
    assert mismatches and "not observed" in mismatches[0]


def test_unsafe_findings_read_the_scenarios_own_predicates():
    scenario = next(s for s in ALL_SCENARIOS if s.id == "POL-006")
    # The system saying ALLOW where a hard block was expected is a bypass.
    assert unsafe_findings(scenario, {"decision": "allow"})
    assert not unsafe_findings(scenario, {"decision": "block"})


# ---------------------------------------------------------------------------
# The run itself
# ---------------------------------------------------------------------------


def test_the_runner_executes_offline(summary):
    """It ran, and it severed the transports before doing so."""
    assert summary["totals"]["scenarios"] == len(ALL_SCENARIOS)
    assert summary["network"]["live_calls_made"] == 0
    guards = summary["network"]["guards_installed"]
    assert any("requests" in guard for guard in guards)
    assert any("llm_client" in guard for guard in guards)


def test_no_scenario_raised_instead_of_being_judged(summary):
    """A scenario that blows up is a failure, and would show up here."""
    crashed = [
        failure["id"] for failure in summary["failures"] if failure["error"]
    ]
    assert crashed == []


def test_category_metrics_are_generated(summary):
    assert summary["by_group"]
    assert summary["by_metric"]
    for bucket in list(summary["by_group"].values()) + list(
        summary["by_metric"].values()
    ):
        assert bucket["total"] == bucket["passed"] + bucket["failed"]
        assert 0.0 <= bucket["pass_rate"] <= 100.0

    # The per-group totals account for every scenario, with none double
    # counted and none quietly dropped.
    assert sum(bucket["total"] for bucket in summary["by_group"].values()) == len(
        ALL_SCENARIOS
    )


def test_unsafe_financial_bypass_metric_is_generated(summary):
    bypass = summary["unsafe_financial_bypass"]
    assert "rate_over_all_scenarios" in bypass
    assert "rate_over_guarded_scenarios" in bypass
    assert bypass["count"] == len(bypass["scenario_ids"])
    # A meaningful denominator: a good number of scenarios can actually
    # express a bypass.
    assert bypass["scenarios_with_bypass_checks"] >= 40


def test_failed_scenario_ids_are_surfaced(summary):
    """Whatever the outcome, the list of failures is reported honestly."""
    assert summary["failed_scenario_ids"] == [
        failure["id"] for failure in summary["failures"]
    ]
    assert len(summary["failed_scenario_ids"]) == summary["totals"]["failed"]


def test_the_summary_is_json_serializable(summary):
    """It has to survive the trip to results.json."""
    round_tripped = json.loads(json.dumps(summary))
    assert round_tripped["totals"] == summary["totals"]


def test_results_artifacts_exist_and_are_valid():
    """The committed results are readable and describe the same suite.

    Regenerated by `python run_evaluation.py`; this checks the artefact on
    disk rather than producing one, so a stale file is caught.
    """
    assert RESULTS_JSON.exists(), "run `python run_evaluation.py`"
    assert RESULTS_MD.exists(), "run `python run_evaluation.py`"

    stored = json.loads(RESULTS_JSON.read_text(encoding="utf-8"))
    assert stored["totals"]["scenarios"] == len(ALL_SCENARIOS)
    assert (
        stored["totals"]["passed"] + stored["totals"]["failed"]
        == stored["totals"]["scenarios"]
    )
    assert "unsafe_financial_bypass" in stored

    markdown = RESULTS_MD.read_text(encoding="utf-8")
    assert "Unsafe financial bypass rate" in markdown
    assert "python run_evaluation.py" in markdown


# ---------------------------------------------------------------------------
# The instrument detects a break
# ---------------------------------------------------------------------------


def test_the_suite_detects_a_broken_control(monkeypatch):
    """Mutation check.

    Disable the mandate's absolute ceiling and the suite must go red and
    report unsafe bypasses. If it stayed green, its 100% would mean
    nothing -- this is the test that makes the headline number worth
    quoting.
    """
    import backend.commerce.product_routes as product_routes
    import backend.policies.policy_engine as policy_engine

    original = policy_engine.evaluate_transaction

    def without_the_ceiling(**kwargs):
        kwargs = dict(kwargs)
        kwargs["absolute_transaction_limit"] = None
        return original(**kwargs)

    monkeypatch.setattr(
        product_routes, "evaluate_transaction", without_the_ceiling
    )

    broken = run(write=False, quiet=True)

    assert broken["totals"]["failed"] > 0
    assert broken["unsafe_financial_bypass"]["count"] > 0
    # And it names the scenarios that depend on the ceiling.
    assert "POL-006" in broken["failed_scenario_ids"]
    assert "APR-011" in broken["failed_scenario_ids"]
