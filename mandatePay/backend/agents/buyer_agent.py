"""The AI buyer agent: one natural-language request, end to end.

Read the pipeline below and the AI/deterministic boundary is the shape of
the function:

    message
      -> [AI]   parse language into an Intent
      -> [CODE] refuse multi-item requests
      -> [CODE] catalogue discovery from real rows
      -> [CODE] HARD constraints filter        -> survivors
      -> [AI]   rank survivors on SOFT preferences
      -> [CODE] validate the nomination against the survivor set
      -> [CODE] persist a Quote at the database price
      -> [CODE] deterministic policy verdict
      -> [CODE] derive the next action

The model appears exactly twice, and both times it is answering a question
about language: "what did they ask for?" and "which of these fits their
preferences best?". It is never asked what something costs, whether the
buyer can afford it, whether the purchase is permitted, or whether
anything has been paid.

This module stops at the safe boundary. It produces a quote and a policy
verdict and nothing else: no transaction row, no Razorpay call, no
payment state. Executing an authorized purchase remains the job of the
existing create-order, approval and payment endpoints.
"""

from dataclasses import dataclass, field
from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

from backend.agents.catalogue import discover_candidates
from backend.agents.intent_parser import parse_intent
from backend.agents.llm_client import LLMCallable
from backend.agents.product_ranker import rank_with_ai
from backend.agents.selection_decision import (
    decide_product_selection,
    eligible_candidates,
    is_materially_vague,
    multi_item_decision,
    rank_candidates,
    selection_for_candidates,
    vague_clarification,
)
from backend.audit import record_event
from backend.commerce.product_routes import quote_and_evaluate
from backend.enums.AuditEventType import AuditEventType
from backend.enums.SelectionAction import SelectionAction
from backend.models.Intent import Intent
from backend.models.RankedCandidates import RankedCandidate
from backend.models.SelectionDecision import SelectionDecision
from backend.models.responses import QuoteResponse


@dataclass
class AgentOutcome:
    """What one agent run produced.

    `quote` is present exactly when `decision.action` is PROPOSE. There is
    no arrangement of AI output that yields a quote without a selection,
    or a selection that is not one of `eligible` -- both are established
    by code below and neither reads a model reply.
    """

    intent: Optional[Intent]
    decision: SelectionDecision
    quote: Optional[QuoteResponse] = None
    eligible: list[RankedCandidate] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def run_buyer_agent(
    db: Session,
    *,
    llm: LLMCallable,
    buyer_id: UUID,
    message: str,
) -> AgentOutcome:
    """Turn one buyer sentence into a quote and a policy verdict, or a
    question, or a refusal."""
    notes: list[str] = []

    # --- [AI] What did the buyer ask for? --------------------------------
    intent = parse_intent(llm=llm, buyer_id=buyer_id, message=message)

    # What the model understood, as structure. Deliberately NOT the raw
    # sentence, the prompt or the reply -- an audit trail needs the
    # interpretation, and storing the rest would put text in a database
    # that nothing downstream ever needs to read.
    record_event(
        db,
        AuditEventType.AGENT_REQUEST_INTERPRETED,
        buyer_id=buyer_id,
        item=intent.item,
        quantity=intent.quantity,
        max_budget=intent.max_budget,
        required_brand=intent.required_brand,
        required_category=intent.required_category,
        hard_constraints=list(intent.hard_constraints),
        soft_preferences=list(intent.soft_preferences),
        is_multi_item=intent.is_multi_item,
        needs_clarification=intent.needs_clarification,
    )

    # --- [CODE] v1 authorizes one item per purchase ----------------------
    # Checked before anything is priced. Splitting a basket would evaluate
    # each half against the transaction limits separately, so a purchase
    # the mandate refuses as one could pass as two.
    if intent.is_multi_item:
        record_event(
            db,
            AuditEventType.AGENT_REQUEST_UNSUPPORTED,
            buyer_id=buyer_id,
            reason="multi_item_request",
            requested_items=list(intent.requested_items),
            quoted=False,
        )
        return AgentOutcome(intent=intent, decision=multi_item_decision(intent))

    if intent.needs_clarification:
        record_event(
            db,
            AuditEventType.AGENT_CLARIFICATION_REQUIRED,
            buyer_id=buyer_id,
            reason="intent_flagged_ambiguous",
            quoted=False,
        )
        return AgentOutcome(
            intent=intent,
            decision=SelectionDecision(
                action=SelectionAction.CLARIFY,
                message=(
                    intent.clarification_question
                    or "I need a little more information before choosing a product."
                ),
            ),
        )

    # --- [CODE] Real catalogue rows, then the hard filter ----------------
    products = discover_candidates(db, intent)
    survivors = eligible_candidates(intent, products)

    # --- [CODE] Ambiguity is a property of the request -------------------
    # Deliberately evaluated BEFORE anything looks at how many rows came
    # back. "Buy me something useful" is vague whether the catalogue
    # answers with nothing, one accidental row, or fifty.
    if is_materially_vague(intent):
        record_event(
            db,
            AuditEventType.AGENT_CLARIFICATION_REQUIRED,
            buyer_id=buyer_id,
            reason="materially_vague_request",
            discovered=len(products),
            eligible=len(survivors),
            quoted=False,
        )
        return AgentOutcome(
            intent=intent,
            decision=vague_clarification(rank_candidates(intent, survivors)),
            eligible=survivors,
        )

    if not survivors:
        record_event(
            db,
            AuditEventType.AGENT_NO_MATCH,
            buyer_id=buyer_id,
            reason="no_candidate_satisfies_hard_constraints",
            discovered=len(products),
            eligible=0,
            quoted=False,
        )
        return AgentOutcome(
            intent=intent,
            decision=decide_product_selection(intent, products),
            eligible=[],
        )

    decision = _select(llm=llm, intent=intent, survivors=survivors, notes=notes)

    if decision.action is not SelectionAction.PROPOSE or decision.selected is None:
        record_event(
            db,
            AuditEventType.AGENT_CLARIFICATION_REQUIRED,
            buyer_id=buyer_id,
            reason="no_clear_winner_among_eligible",
            selection_action=decision.action,
            eligible=len(survivors),
            quoted=False,
        )
        return AgentOutcome(
            intent=intent, decision=decision, eligible=survivors, notes=notes
        )

    # The chosen product came out of `survivors`, which the server built,
    # so these are server-validated facts rather than model claims. They
    # are filed against the quote below, because that is the first thing
    # this selection can be attributed to.
    selection_facts = {
        "selection_action": decision.action,
        "product_name": decision.selected.product.product_name,
        "eligible": len(survivors),
        "discovered": len(products),
        # Set when the model's nomination was rejected and the
        # deterministic ranking chose instead.
        "ai_nomination_rejected": bool(notes),
    }

    # --- [CODE] Price and policy, from the database ----------------------
    # The only three things carried across this line are a buyer id, a
    # product id the server validated, and a quantity the server bounded.
    # The price is read from the product row inside `quote_and_evaluate`.
    # `agent_selection` is audit payload and reaches nothing else.
    quote = quote_and_evaluate(
        db,
        buyer_id=str(intent.buyer_id),
        product_id=str(decision.selected.product.product_id),
        quantity=intent.quantity,
        agent_selection=selection_facts,
    )

    return AgentOutcome(
        intent=intent,
        decision=decision,
        quote=quote,
        eligible=survivors,
        notes=notes,
    )


def _select(
    *,
    llm: LLMCallable,
    intent: Intent,
    survivors: list[RankedCandidate],
    notes: list[str],
) -> SelectionDecision:
    """Let the model rank the survivors, then check its answer.

    The check is the safety property. `rank_with_ai` resolves the
    nominated id against `survivors` and returns one of those objects or
    nothing, so a hallucinated id, an id for a real product that failed a
    hard constraint, and an id smuggled in through a product description
    all land in the same place: discarded, and the deterministic ranking
    decides instead.
    """
    outcome = rank_with_ai(llm=llm, intent=intent, candidates=survivors)

    if outcome.selected is not None:
        return selection_for_candidates(
            intent,
            survivors,
            chosen=outcome.selected,
            rationale=outcome.rationale,
        )

    if outcome.rejected_reason:
        notes.append(outcome.rejected_reason)
        notes.append("Fell back to deterministic ranking of the eligible candidates.")
        return _deterministic_selection(intent, survivors)

    # The model saw no clear winner and asked a question.
    return SelectionDecision(
        action=SelectionAction.CLARIFY,
        message=(
            outcome.clarification_question
            or "I can see several equally good options. Which would you prefer?"
        ),
        alternatives=rank_candidates(intent, survivors)[:4],
    )


def _deterministic_selection(
    intent: Intent, survivors: list[RankedCandidate]
) -> SelectionDecision:
    """Selection with no AI involvement at all.

    Re-runs the deterministic engine over the survivor set, so the fallback
    can only ever choose something that already passed every hard rule.
    """
    return decide_product_selection(
        intent,
        [
            {
                "product_id": candidate.product.product_id,
                "product_name": candidate.product.product_name,
                "brand": candidate.product.brand,
                "category": candidate.product.category,
                "cost": candidate.product.cost,
                "quantity": candidate.product.quantity,
                "merchant_id": candidate.product.merchant_id,
            }
            for candidate in survivors
        ],
    )
