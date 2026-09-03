"""The natural-language entry point.

One endpoint: a buyer says what they want in a sentence, and the agent
comes back with either a question or a priced, policy-evaluated proposal.

What this route deliberately does NOT do:

  * write a transaction row
  * call Razorpay
  * set any payment status

It stops at the quote and the policy verdict. Executing an authorized
purchase is still the job of `/create-order`, the approval endpoints and
`/payment/verify`, which are unchanged.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.agents.buyer_agent import AgentOutcome, run_buyer_agent
from backend.agents.llm_client import (
    LLMCallable,
    LLMConfigurationError,
    LLMRateLimitError,
    LLMResponseError,
    get_llm,
)
from backend.audit import record_event
from backend.database import get_db
from backend.databases.buyer_db import buyer_db
from backend.enums.AuditEventType import AuditEventType
from backend.enums.NextAction import NextAction, next_action_for
from backend.enums.RejectionReason import RejectionReason
from backend.enums.SelectionAction import SelectionAction
from backend.models.Intent import Intent
from backend.models.RankedCandidates import RankedCandidate
from backend.models.agent_models import (
    AgentPurchaseRequest,
    AgentPurchaseResponse,
    InterpretedIntent,
    SelectedProduct,
)
from backend.models.responses import ProductSummary, RejectionDetail

router = APIRouter(prefix="/app/v1/agent", tags=["agent"])


def _reject(
    status_code: int,
    reason: RejectionReason,
    message: str,
    *,
    retry_after_seconds: Optional[float] = None,
) -> None:
    detail = RejectionDetail(
        reason=reason, message=message, retry_after_seconds=retry_after_seconds
    )
    raise HTTPException(status_code=status_code, detail=detail.model_dump(mode="json"))


def _agent_failed(db: Session, buyer_id, reason: RejectionReason) -> None:
    """Record that the model half failed and produced nothing.

    The interesting fields are the ones asserting absence: a failed agent
    run creates no quote, no transaction and no payment, so the trail says
    so explicitly rather than leaving a gap for a reader to interpret.

    The exception text is deliberately not stored -- it can carry provider
    detail, and the reason code is what a reader actually branches on.
    """
    record_event(
        db,
        AuditEventType.AGENT_REQUEST_FAILED,
        buyer_id=buyer_id,
        reason=reason,
        quote_created=False,
        transaction_created=False,
        provider_called=False,
    )


def _interpreted(intent: Intent) -> InterpretedIntent:
    return InterpretedIntent(
        raw_text=intent.raw_text,
        item=intent.item,
        quantity=intent.quantity,
        max_budget=intent.max_budget,
        required_brand=intent.required_brand,
        required_category=intent.required_category,
        hard_constraints=intent.hard_constraints,
        preferred_brand=intent.preferred_brand,
        soft_preferences=intent.soft_preferences,
        is_multi_item=intent.is_multi_item,
        requested_items=intent.requested_items,
    )


def _summary(candidate: RankedCandidate) -> ProductSummary:
    product = candidate.product
    return ProductSummary(
        product_id=product.product_id,
        product_name=product.product_name,
        brand=product.brand,
        category=product.category,
        merchant_id=product.merchant_id,
    )


def _selection(outcome: AgentOutcome) -> Optional[SelectedProduct]:
    """The chosen product, described from the persisted quote.

    `unit_price` is taken from the quote -- which took it from the product
    row -- so the price shown to the caller and the price policy was
    evaluated against are the same number by construction.
    """
    chosen = outcome.decision.selected
    if chosen is None or outcome.quote is None:
        return None

    return SelectedProduct(
        product=outcome.quote.product,
        unit_price=outcome.quote.quoted_unit_price,
        rationale=outcome.decision.message,
        eligible_candidates=len(outcome.eligible),
        alternatives=[_summary(candidate) for candidate in outcome.decision.alternatives],
    )


def _to_response(buyer_id, outcome: AgentOutcome) -> AgentPurchaseResponse:
    """Assemble the response. Every field here is server-derived.

    `next_action` comes from `next_action_for`, a pure function of the
    selection outcome and the policy verdict. No branch of it reads model
    output, and none can turn a BLOCK into a payable action.
    """
    policy = outcome.quote.policy if outcome.quote is not None else None
    action = outcome.decision.action

    return AgentPurchaseResponse(
        buyer_id=buyer_id,
        selection_action=action,
        next_action=next_action_for(action, policy.decision if policy else None),
        message=outcome.decision.message,
        intent=_interpreted(outcome.intent) if outcome.intent is not None else None,
        selection=_selection(outcome),
        quote=outcome.quote,
        policy=policy,
        clarification_question=(
            outcome.decision.message if action is SelectionAction.CLARIFY else None
        ),
        notes=outcome.notes,
    )


@router.post("/purchase", response_model=AgentPurchaseResponse)
def agent_purchase(
    request: AgentPurchaseRequest,
    db: Session = Depends(get_db),
    llm: LLMCallable = Depends(get_llm),
) -> AgentPurchaseResponse:
    """Interpret a buyer's sentence, choose a product, quote it, and report
    the deterministic policy verdict.

    The request carries a buyer and a sentence. There is no field for a
    price, an amount, a limit, a policy decision or a payment status, and
    no value in the sentence is ever treated as one.

    A `next_action` of CREATE_ORDER means the caller may now attempt
    payment via `/app/v1/create-order`. It does not mean anything has been
    paid; only `/app/v1/payment/verify` can ever report that.
    """
    # Buildathon identity: a demo buyer is selected, not authenticated.
    # It still has to be a real buyer -- an agent that will happily reason
    # about a nonexistent buyer's money is not a safe demo.
    if db.get(buyer_db, str(request.buyer_id)) is None:
        _reject(404, RejectionReason.BUYER_NOT_FOUND, "Buyer not found.")

    try:
        outcome = run_buyer_agent(
            db,
            llm=llm,
            buyer_id=request.buyer_id,
            message=request.message,
        )
    except LLMConfigurationError as exc:
        # Fail loudly rather than quietly substituting a heuristic and
        # letting a demo claim an AI parsed the request.
        _agent_failed(db, request.buyer_id, RejectionReason.AGENT_NOT_CONFIGURED)
        _reject(503, RejectionReason.AGENT_NOT_CONFIGURED, str(exc))
    except LLMRateLimitError as exc:
        # The provider throttled the call, so no AI ran and nothing was
        # quoted. This is temporary and the request itself was fine, which
        # is why the caller is told it may retry rather than being handed
        # a generic failure. 503 rather than 429: the limit belongs to
        # this service's provider account, not to the caller.
        _agent_failed(db, request.buyer_id, RejectionReason.AGENT_RATE_LIMITED)
        _reject(
            503,
            RejectionReason.AGENT_RATE_LIMITED,
            "The AI provider is temporarily rate limited, so nothing was "
            "quoted and nothing was ordered. Please try again shortly.",
            retry_after_seconds=exc.retry_after_seconds,
        )
    except LLMResponseError as exc:
        # A reply we cannot read is an error, never a licence to guess.
        # Nothing was quoted and nothing was ordered.
        _agent_failed(db, request.buyer_id, RejectionReason.AGENT_RESPONSE_INVALID)
        _reject(
            502,
            RejectionReason.AGENT_RESPONSE_INVALID,
            f"The AI buyer agent returned an unusable response and nothing "
            f"was quoted. ({exc})",
        )

    return _to_response(request.buyer_id, outcome)


__all__ = ["router", "NextAction"]
