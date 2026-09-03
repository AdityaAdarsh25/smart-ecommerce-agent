# MandatePay evaluation results

Reproduce with:

```
python run_evaluation.py
```

Every scenario runs against a throwaway in-memory database with a faked Razorpay and a scripted language model. No live OpenAI or Razorpay call is made, and the outbound transports are severed for the duration of the run, so a code path that tried would fail rather than succeed quietly.

## Headline

| | |
|---|---|
| Scenarios | 122 |
| Passed | 122 |
| Failed | 0 |
| Pass rate | 100.0% |
| **Unsafe financial bypass rate** | **0.0%** (0/122) |

A scenario counts as an unsafe financial bypass if the system authorized something expected to hard-BLOCK, reached the payment provider when it should not have, reported PAID without a verified payment, accepted an injected price instead of the database price, or bypassed a required human approval. Each scenario declares its own bypass conditions alongside its expected outcome, written from the scenario's intent rather than from what the code does.

Of the 122 scenarios, 65 carry at least one bypass condition; the rate over just those is 0.0%.

## By group

| Group | Passed | Total | Pass rate |
|---|---:|---:|---:|
| policy_mandate | 24 | 24 | 100.0% |
| quote_price_stock | 16 | 16 | 100.0% |
| duplicates | 12 | 12 | 100.0% |
| approval | 16 | 16 | 100.0% |
| ai_intent_selection | 22 | 22 | 100.0% |
| payment | 18 | 18 | 100.0% |
| adversarial_catalogue | 14 | 14 | 100.0% |

## By metric

| Metric | Passed | Total | Pass rate |
|---|---:|---:|---:|
| policy_decision_correctness | 11 | 11 | 100.0% |
| hard_block_correctness | 12 | 12 | 100.0% |
| approval_escalation_correctness | 11 | 11 | 100.0% |
| approval_revalidation_correctness | 8 | 8 | 100.0% |
| quote_integrity | 12 | 12 | 100.0% |
| price_drift_detection | 4 | 4 | 100.0% |
| duplicate_protection_correctness | 12 | 12 | 100.0% |
| payment_state_correctness | 18 | 18 | 100.0% |
| clarification_selection_correctness | 20 | 20 | 100.0% |
| adversarial_bypass_resistance | 14 | 14 | 100.0% |

## Failures

None.

Generated 2026-09-02T11:42:51+00:00.
