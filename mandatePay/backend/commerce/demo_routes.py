"""The demo front-end and the two read-only endpoints it needs.

This module is presentation only. It evaluates no policy, writes no row,
contacts no provider and can move no money -- everything it serves is
either a static file or a read of something another package already
decided.

Two things are load-bearing here:

  * `public_config` reads RAZORPAY_KEY_ID and nothing else. The secret is
    never read in this module, so there is no code path through which it
    could reach a browser.
  * `evaluation_summary` reads `evaluation/results.json` from disk rather
    than restating numbers in source, so the safety evidence shown in the
    UI cannot drift away from the accepted evaluation run.
"""

import json
import os
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from backend.models.responses import (
    EvaluationGroupSummary,
    EvaluationSummaryResponse,
    PublicConfigResponse,
)

# demo_routes.py -> commerce -> backend -> repository root
REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = Path(__file__).resolve().parents[1] / "static"
INDEX_HTML = STATIC_DIR / "index.html"
RESULTS_JSON = REPO_ROOT / "evaluation" / "results.json"

# The public Razorpay key. Checkout requires it in the page; it is not a
# credential that can authorise anything on its own.
ENV_RAZORPAY_KEY_ID = "RAZORPAY_KEY_ID"
# Read as a PRESENCE CHECK ONLY. The value is never returned, logged or
# rendered -- see `test_public_config_never_exposes_a_secret`.
ENV_RAZORPAY_KEY_SECRET = "RAZORPAY_KEY_SECRET"
ENV_OPENAI_API_KEY = "OPENAI_API_KEY"
ENV_OPENAI_MODEL = "OPENAI_MODEL"

page_router = APIRouter(tags=["demo"])
router = APIRouter(prefix="/app/v1", tags=["demo"])


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


@page_router.get("/", include_in_schema=False)
def demo_ui() -> FileResponse:
    """The MandatePay demo UI.

    Served from the same process as the API so the whole thing runs with
    one `uvicorn` command and no separate front-end server. The API docs
    remain at /docs.
    """
    if not INDEX_HTML.is_file():
        raise HTTPException(status_code=404, detail="Demo UI is not installed.")
    return FileResponse(INDEX_HTML, media_type="text/html")


@router.get("/config", response_model=PublicConfigResponse)
def public_config() -> PublicConfigResponse:
    """Everything the browser is allowed to know about our configuration.

    Exactly one value crosses this boundary -- the public Razorpay key id
    that Checkout needs in order to open. The secret is only ever tested
    for presence, so the UI can say "payment is not configured" instead of
    failing inside a provider SDK, and its value stays server-side.
    """
    key_id = _env(ENV_RAZORPAY_KEY_ID)
    has_secret = bool(_env(ENV_RAZORPAY_KEY_SECRET))

    return PublicConfigResponse(
        razorpay_key_id=key_id or None,
        razorpay_configured=bool(key_id and has_secret),
        agent_configured=bool(_env(ENV_OPENAI_API_KEY) and _env(ENV_OPENAI_MODEL)),
    )


def _groups(payload: dict[str, Any]) -> list[EvaluationGroupSummary]:
    by_group = payload.get("by_group") or {}
    return [
        EvaluationGroupSummary(
            name=name,
            total=int(group.get("total", 0)),
            passed=int(group.get("passed", 0)),
            failed=int(group.get("failed", 0)),
            pass_rate=float(group.get("pass_rate", 0.0)),
        )
        for name, group in by_group.items()
    ]


@router.get("/evaluation/summary", response_model=EvaluationSummaryResponse)
def evaluation_summary() -> EvaluationSummaryResponse:
    """The accepted evaluation result, read from `evaluation/results.json`.

    A read of a committed artefact. If the file is absent the answer is an
    explicit 503 -- the UI then says the evidence is unavailable, which is
    the honest outcome, rather than showing numbers nobody measured.
    """
    if not RESULTS_JSON.is_file():
        raise HTTPException(
            status_code=503,
            detail=(
                "No evaluation results are available. Run `python "
                "run_evaluation.py` to produce evaluation/results.json."
            ),
        )

    try:
        payload = json.loads(RESULTS_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=503, detail="Evaluation results could not be read."
        ) from exc

    totals = payload.get("totals") or {}
    bypass = payload.get("unsafe_financial_bypass") or {}
    network = payload.get("network") or {}

    generated_at: Optional[str] = payload.get("generated_at")

    return EvaluationSummaryResponse(
        generated_at=generated_at,
        scenarios=int(totals.get("scenarios", 0)),
        passed=int(totals.get("passed", 0)),
        failed=int(totals.get("failed", 0)),
        pass_rate=float(totals.get("pass_rate", 0.0)),
        unsafe_financial_bypass_count=int(bypass.get("count", 0)),
        unsafe_financial_bypass_rate=float(bypass.get("rate_over_all_scenarios", 0.0)),
        live_provider_calls=int(network.get("live_calls_made", 0)),
        groups=_groups(payload),
    )


__all__ = ["router", "page_router", "STATIC_DIR"]
