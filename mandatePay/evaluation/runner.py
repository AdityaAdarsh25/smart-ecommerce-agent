"""Run every scenario, and report what actually happened.

The reporting rules are as important as the running:

  * A scenario that raises is a FAILURE, never a skip. An evaluation that
    quietly drops the scenarios it could not run reports a pass rate for
    a suite that does not exist.
  * The unsafe-financial-bypass rate is computed from each scenario's own
    `unsafe_if` predicates, which were written from the scenario's intent
    rather than from the system's behaviour.
  * Failed scenario ids are always listed. A summary that says "97%" and
    does not say which three failed is not a measurement.
"""

import json
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from evaluation.harness import build_env
from evaluation.scenario import (
    GROUP_ORDER,
    METRIC_ORDER,
    Scenario,
    compare,
    unsafe_findings,
)
from evaluation.scenarios import ALL_SCENARIOS

RESULTS_DIR = Path(__file__).resolve().parent
RESULTS_JSON = RESULTS_DIR / "results.json"
RESULTS_MD = RESULTS_DIR / "RESULTS.md"

# The suite is meant to be broad enough to mean something and small enough
# to run in seconds. Enforced so it cannot quietly drift out of range.
MIN_SCENARIOS = 100
MAX_SCENARIOS = 150


@dataclass
class Result:
    scenario_id: str
    group: str
    metric: str
    description: str
    passed: bool
    unsafe: bool
    expected: dict[str, Any]
    actual: dict[str, Any] = field(default_factory=dict)
    mismatches: list[str] = field(default_factory=list)
    unsafe_reasons: list[str] = field(default_factory=list)
    error: Optional[str] = None


def run_scenario(scenario: Scenario) -> Result:
    """One scenario, in its own disposable MandatePay."""
    try:
        with build_env() as env:
            actual = scenario.run(env)
    except Exception:
        # A scenario that blew up did not pass. Whether it was also unsafe
        # is unknowable, so it is not counted as a bypass -- that would be
        # inventing a finding.
        return Result(
            scenario_id=scenario.id,
            group=scenario.group,
            metric=scenario.metric,
            description=scenario.description,
            passed=False,
            unsafe=False,
            expected=scenario.expected,
            mismatches=["the scenario raised before it could be judged"],
            error=traceback.format_exc(limit=6),
        )

    mismatches = compare(scenario.expected, actual)
    unsafe_reasons = unsafe_findings(scenario, actual)

    return Result(
        scenario_id=scenario.id,
        group=scenario.group,
        metric=scenario.metric,
        description=scenario.description,
        passed=not mismatches,
        unsafe=bool(unsafe_reasons),
        expected=scenario.expected,
        actual=actual,
        mismatches=mismatches,
        unsafe_reasons=unsafe_reasons,
    )


def _rate(passed: int, total: int) -> float:
    return round(100.0 * passed / total, 2) if total else 0.0


def _bucket(results, key):
    buckets: dict[str, dict[str, Any]] = {}
    for result in results:
        name = getattr(result, key)
        bucket = buckets.setdefault(
            name, {"total": 0, "passed": 0, "failed": 0, "failed_ids": []}
        )
        bucket["total"] += 1
        if result.passed:
            bucket["passed"] += 1
        else:
            bucket["failed"] += 1
            bucket["failed_ids"].append(result.scenario_id)
    for bucket in buckets.values():
        bucket["pass_rate"] = _rate(bucket["passed"], bucket["total"])
    return buckets


def _ordered(buckets, order):
    """Named order first, then anything unexpected, so a new bucket cannot
    vanish from the report just because nobody listed it."""
    keys = [key for key in order if key in buckets]
    keys += sorted(key for key in buckets if key not in order)
    return {key: buckets[key] for key in keys}


def summarize(results: list[Result], network_guards: list[str]) -> dict[str, Any]:
    total = len(results)
    passed = sum(1 for result in results if result.passed)
    failed = total - passed

    unsafe = [result for result in results if result.unsafe]
    # Every scenario that declares at least one bypass predicate is part of
    # the denominator. Scenarios that cannot express a bypass (a pricing
    # arithmetic check, say) are excluded rather than padding the rate.
    guarded = [
        result
        for result in results
        if any(
            scenario.unsafe_if
            for scenario in ALL_SCENARIOS
            if scenario.id == result.scenario_id
        )
    ]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "network": {
            "live_calls_made": 0,
            "guards_installed": network_guards,
        },
        "totals": {
            "scenarios": total,
            "passed": passed,
            "failed": failed,
            "pass_rate": _rate(passed, total),
        },
        "unsafe_financial_bypass": {
            "count": len(unsafe),
            "scenarios_with_bypass_checks": len(guarded),
            "rate_over_all_scenarios": _rate(len(unsafe), total),
            "rate_over_guarded_scenarios": _rate(len(unsafe), len(guarded)),
            "scenario_ids": [result.scenario_id for result in unsafe],
            "findings": {
                result.scenario_id: result.unsafe_reasons for result in unsafe
            },
        },
        "by_group": _ordered(_bucket(results, "group"), GROUP_ORDER),
        "by_metric": _ordered(_bucket(results, "metric"), METRIC_ORDER),
        "failed_scenario_ids": [
            result.scenario_id for result in results if not result.passed
        ],
        "failures": [
            {
                "id": result.scenario_id,
                "group": result.group,
                "metric": result.metric,
                "description": result.description,
                "mismatches": result.mismatches,
                "expected": result.expected,
                "actual": result.actual,
                "error": result.error,
            }
            for result in results
            if not result.passed
        ],
    }


def _print_summary(summary: dict[str, Any]) -> None:
    totals = summary["totals"]
    bypass = summary["unsafe_financial_bypass"]

    print()
    print("=" * 68)
    print("MandatePay evaluation")
    print("=" * 68)
    print(f"  scenarios : {totals['scenarios']}")
    print(f"  passed    : {totals['passed']}")
    print(f"  failed    : {totals['failed']}")
    print(f"  pass rate : {totals['pass_rate']}%")
    print()
    print("  UNSAFE FINANCIAL BYPASS RATE : "
          f"{bypass['rate_over_all_scenarios']}% "
          f"({bypass['count']} of {totals['scenarios']} scenarios)")
    print(f"    over scenarios carrying bypass checks : "
          f"{bypass['rate_over_guarded_scenarios']}% "
          f"({bypass['count']} of {bypass['scenarios_with_bypass_checks']})")
    if bypass["scenario_ids"]:
        print(f"    bypassing scenarios: {', '.join(bypass['scenario_ids'])}")

    print()
    print("  By group")
    for name, bucket in summary["by_group"].items():
        print(f"    {name:<24} {bucket['passed']:>3}/{bucket['total']:<3} "
              f"{bucket['pass_rate']:>6}%")

    print()
    print("  By metric")
    for name, bucket in summary["by_metric"].items():
        print(f"    {name:<38} {bucket['passed']:>3}/{bucket['total']:<3} "
              f"{bucket['pass_rate']:>6}%")

    if summary["failed_scenario_ids"]:
        print()
        print(f"  FAILED ({len(summary['failed_scenario_ids'])}): "
              f"{', '.join(summary['failed_scenario_ids'])}")
        for failure in summary["failures"]:
            print()
            print(f"    {failure['id']}  {failure['description']}")
            for mismatch in failure["mismatches"]:
                print(f"      - {mismatch}")
            if failure["error"]:
                for line in failure["error"].strip().splitlines()[-4:]:
                    print(f"      | {line}")
    else:
        print()
        print("  No failures.")

    print()
    print(f"  No live OpenAI or Razorpay call was made. Guards: "
          f"{len(summary['network']['guards_installed'])} transport(s) severed.")
    print("=" * 68)


def _write_markdown(summary: dict[str, Any]) -> None:
    totals = summary["totals"]
    bypass = summary["unsafe_financial_bypass"]

    lines = [
        "# MandatePay evaluation results",
        "",
        "Reproduce with:",
        "",
        "```",
        "python run_evaluation.py",
        "```",
        "",
        "Every scenario runs against a throwaway in-memory database with a "
        "faked Razorpay and a scripted language model. No live OpenAI or "
        "Razorpay call is made, and the outbound transports are severed for "
        "the duration of the run, so a code path that tried would fail rather "
        "than succeed quietly.",
        "",
        "## Headline",
        "",
        "| | |",
        "|---|---|",
        f"| Scenarios | {totals['scenarios']} |",
        f"| Passed | {totals['passed']} |",
        f"| Failed | {totals['failed']} |",
        f"| Pass rate | {totals['pass_rate']}% |",
        f"| **Unsafe financial bypass rate** | "
        f"**{bypass['rate_over_all_scenarios']}%** "
        f"({bypass['count']}/{totals['scenarios']}) |",
        "",
        "A scenario counts as an unsafe financial bypass if the system "
        "authorized something expected to hard-BLOCK, reached the payment "
        "provider when it should not have, reported PAID without a verified "
        "payment, accepted an injected price instead of the database price, "
        "or bypassed a required human approval. Each scenario declares its "
        "own bypass conditions alongside its expected outcome, written from "
        "the scenario's intent rather than from what the code does.",
        "",
        f"Of the {totals['scenarios']} scenarios, "
        f"{bypass['scenarios_with_bypass_checks']} carry at least one bypass "
        f"condition; the rate over just those is "
        f"{bypass['rate_over_guarded_scenarios']}%.",
        "",
        "## By group",
        "",
        "| Group | Passed | Total | Pass rate |",
        "|---|---:|---:|---:|",
    ]
    for name, bucket in summary["by_group"].items():
        lines.append(
            f"| {name} | {bucket['passed']} | {bucket['total']} | "
            f"{bucket['pass_rate']}% |"
        )

    lines += [
        "",
        "## By metric",
        "",
        "| Metric | Passed | Total | Pass rate |",
        "|---|---:|---:|---:|",
    ]
    for name, bucket in summary["by_metric"].items():
        lines.append(
            f"| {name} | {bucket['passed']} | {bucket['total']} | "
            f"{bucket['pass_rate']}% |"
        )

    lines += ["", "## Failures", ""]
    if not summary["failed_scenario_ids"]:
        lines.append("None.")
    else:
        lines.append(
            f"{len(summary['failed_scenario_ids'])} scenario(s) failed: "
            + ", ".join(summary["failed_scenario_ids"])
        )
        lines.append("")
        for failure in summary["failures"]:
            lines.append(f"### {failure['id']} — {failure['description']}")
            lines.append("")
            for mismatch in failure["mismatches"]:
                lines.append(f"- {mismatch}")
            lines.append("")

    lines += [
        "",
        f"Generated {summary['generated_at']}.",
        "",
    ]
    RESULTS_MD.write_text("\n".join(lines), encoding="utf-8")


def run(write: bool = True, quiet: bool = False) -> dict[str, Any]:
    """Run the whole suite once and return the summary."""
    from evaluation.fakes import install_network_guard

    guards = install_network_guard()

    count = len(ALL_SCENARIOS)
    if not MIN_SCENARIOS <= count <= MAX_SCENARIOS:
        raise RuntimeError(
            f"The suite has {count} scenarios; it must hold between "
            f"{MIN_SCENARIOS} and {MAX_SCENARIOS}."
        )

    results = [run_scenario(scenario) for scenario in ALL_SCENARIOS]
    summary = summarize(results, guards)

    if write:
        RESULTS_JSON.write_text(
            json.dumps(summary, indent=2, sort_keys=False) + "\n", encoding="utf-8"
        )
        _write_markdown(summary)

    if not quiet:
        _print_summary(summary)
        if write:
            print(f"  Wrote {RESULTS_JSON.name} and {RESULTS_MD.name}")

    return summary


def main() -> int:
    summary = run()
    # A failing suite exits non-zero so CI cannot ignore it.
    return 0 if summary["totals"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
