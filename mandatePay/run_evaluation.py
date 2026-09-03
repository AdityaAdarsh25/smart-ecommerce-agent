"""Run the MandatePay evaluation suite.

    python run_evaluation.py

Writes `evaluation/results.json` and `evaluation/RESULTS.md`, prints a
summary, and exits non-zero if any scenario failed.
"""

from evaluation.runner import main

if __name__ == "__main__":
    raise SystemExit(main())
