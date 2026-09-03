"""Deterministic product selection.

This module decides WHICH product, never WHETHER it may be bought. It is
pure, takes catalogue rows the server itself read, and returns a
`SelectionDecision`. Payment authority belongs to the policy engine.

The order of operations is the point:

    all candidates -> HARD constraints -> survivors -> SOFT ranking

Hard constraints filter; soft preferences only reorder what survived. A
product that violates a hard constraint is never in the set being ranked,
so it cannot win by scoring well on preferences.
"""

from typing import Any, Optional

from backend.enums.SelectionAction import SelectionAction
from backend.models.Intent import Intent
from backend.models.Product import Product
from backend.models.RankedCandidates import RankedCandidate
from backend.models.SelectionDecision import SelectionDecision

# How far ahead the best candidate must be before the agent proposes it
# without asking. Small and understandable on purpose -- this is a
# tie-break rule, not a confidence model.
CLEAR_WINNER_MARGIN = 10

MAX_ALTERNATIVES = 4


def is_materially_vague(intent: Intent) -> bool:
    """Is the buyer's request too underspecified to choose safely?

    A property of the REQUEST, never of the result set. "Buy me something
    useful" is vague whether the catalogue answers it with one product or
    a hundred -- the fact that only one row happened to match does not
    tell us the buyer wanted that row.
    """
    return (
        intent.max_budget is None
        and intent.required_brand is None
        and intent.required_category is None
        and intent.preferred_brand is None
        and intent.preferred_merchant is None
        and not intent.hard_constraints
        and not intent.soft_preferences
    )


def _candidate_from_dict(product: dict[str, Any]) -> RankedCandidate:
    return RankedCandidate(
        product=Product(
            product_id=product["product_id"],
            product_name=product["product_name"],
            brand=product["brand"],
            cost=float(product["cost"]),
            category=product.get("category"),
            quantity=int(product["quantity"]),
            merchant_id=product["merchant_id"],
        )
    )


def _searchable_text(product: Product) -> str:
    """Everything the catalogue actually knows about a product, lowercased.

    Only these fields exist, so only claims about these fields can be
    verified. We never invent an attribute the catalogue does not store.
    """
    return " ".join(
        part for part in (product.product_name, product.brand, product.category or "")
    ).lower()


def _passes_structured_hard_constraints(
    intent: Intent,
    candidate: RankedCandidate,
) -> bool:
    """Every hard requirement, checked deterministically against the row.

    Free-text hard constraints fail CLOSED: a requirement we cannot verify
    from catalogue data is treated as unmet rather than waved through. For
    a non-negotiable requirement that is the only safe direction, and it
    keeps the model out of the decision entirely -- the server compares
    numbers and strings it read itself.
    """
    product = candidate.product

    # Inventory is a hard constraint.
    if product.quantity < intent.quantity:
        return False

    # Budget is a hard ceiling on the TOTAL, compared by the server. The
    # model is never asked whether a price fits a budget.
    if (
        intent.max_budget is not None
        and product.cost * intent.quantity > intent.max_budget
    ):
        return False

    # A required brand is a filter; a preferred brand is not.
    if (
        intent.required_brand is not None
        and product.brand.strip().lower() != intent.required_brand.strip().lower()
    ):
        return False

    if intent.required_category is not None:
        category = (product.category or "").strip().lower()
        if category != intent.required_category.strip().lower():
            return False

    text = _searchable_text(product)
    for constraint in intent.hard_constraints:
        token = constraint.strip().lower()
        if token and token not in text:
            return False

    return True


def eligible_candidates(
    intent: Intent,
    products: list[dict[str, Any]],
) -> list[RankedCandidate]:
    """The survivor set: candidates that satisfy every hard constraint.

    This is the ONLY set anything downstream -- including the language
    model -- is permitted to choose from.
    """
    return [
        candidate
        for candidate in (_candidate_from_dict(product) for product in products)
        if _passes_structured_hard_constraints(intent, candidate)
    ]


def _rank_candidate(
    intent: Intent,
    candidate: RankedCandidate,
) -> RankedCandidate:
    """Score a survivor on preferences only.

    Ranking must never override hard constraints or financial policy.
    """
    score = 0.0
    reasons: list[str] = []

    requested_item = intent.item.strip().lower()
    product_name = candidate.product.product_name.strip().lower()

    # Stronger textual match to requested item.
    if product_name == requested_item:
        score += 30
        reasons.append("exact item-name match")
    elif requested_item and requested_item in product_name:
        score += 20
        reasons.append("strong item-name match")

    if (
        intent.preferred_brand
        and candidate.product.brand.lower() == intent.preferred_brand.lower()
    ):
        score += 25
        reasons.append("preferred brand")

    # Lightweight support for free-text soft preferences. We only reward
    # preferences that are actually visible in the product metadata. We do
    # NOT invent product characteristics.
    searchable_text = _searchable_text(candidate.product)

    for preference in intent.soft_preferences:
        if preference.lower() in searchable_text:
            score += 5
            reasons.append(f"matches preference: {preference}")

    candidate.score = score
    candidate.reasons = reasons

    return candidate


def rank_candidates(
    intent: Intent,
    candidates: list[RankedCandidate],
) -> list[RankedCandidate]:
    """Survivors, best first."""
    return sorted(
        (_rank_candidate(intent, candidate) for candidate in candidates),
        key=lambda candidate: candidate.score,
        reverse=True,
    )


def vague_clarification(candidates: list[RankedCandidate]) -> SelectionDecision:
    """Ask, because the buyer did not say enough -- whatever the catalogue
    happened to return.

    Reached on the strength of the request alone. Zero, one or twenty
    matching rows all produce the same outcome here, which is the whole
    point: a single accidental match is not consent to buy it.
    """
    shortlist = candidates[:MAX_ALTERNATIVES]
    preamble = "That request is too open-ended for me to spend against safely."
    if shortlist:
        option_text = "; ".join(
            f"{candidate.product.product_name} by {candidate.product.brand} "
            f"(Rs. {candidate.product.cost:.2f})"
            for candidate in shortlist
        )
        preamble = f"{preamble} Candidates I can see: {option_text}."
    return SelectionDecision(
        action=SelectionAction.CLARIFY,
        message=(
            f"{preamble} What exactly should I buy, and is there a budget, "
            "brand or requirement I must respect?"
        ),
        alternatives=shortlist,
    )


def multi_item_decision(intent: Intent) -> SelectionDecision:
    """Refuse a multi-item request instead of quietly splitting it.

    Splitting "a keyboard and a mouse" into two independent purchases
    would evaluate each against the transaction limits separately, so a
    basket the mandate would refuse as one purchase could slip through as
    two. That is a control bypass, so v1 refuses rather than guesses.
    """
    items = ", ".join(intent.requested_items) if intent.requested_items else intent.item
    return SelectionDecision(
        action=SelectionAction.UNSUPPORTED,
        message=(
            f"That request covers more than one item ({items}). "
            "MandatePay v1 authorizes one item per purchase, and splitting "
            "a basket into separate transactions could slip past your "
            "spending limits. Please ask for one item at a time."
        ),
    )


def decide_product_selection(
    intent: Intent,
    products: list[dict[str, Any]],
) -> SelectionDecision:
    """Decide what the buyer agent should do AFTER catalogue search.

    This function does NOT authorize payment.

    Output meanings:
    - UNSUPPORTED: the request shape is outside v1 (multi-item)
    - NO_MATCH: nothing valid satisfies the structured requirements
    - CLARIFY: user input is too ambiguous to safely choose
    - PROPOSE: agent has a best candidate and can move to quote generation

    Payment authority belongs to the deterministic Policy Engine later.
    """
    if intent.is_multi_item:
        return multi_item_decision(intent)

    if intent.needs_clarification:
        return SelectionDecision(
            action=SelectionAction.CLARIFY,
            message=(
                intent.clarification_question
                or "I need a little more information before choosing a product."
            ),
        )

    valid_candidates = eligible_candidates(intent, products)

    # Nothing satisfies the actual hard requirements.
    if not valid_candidates:
        return SelectionDecision(
            action=SelectionAction.NO_MATCH,
            message=(
                "I couldn't find a product that satisfies the "
                "required constraints."
            ),
        )

    # IMPORTANT: checked BEFORE any candidate-count logic. One matching row
    # does not make a vague request precise, and many matching rows do not
    # make a precise one ambiguous.
    if is_materially_vague(intent):
        return vague_clarification(rank_candidates(intent, valid_candidates))

    ranked = rank_candidates(intent, valid_candidates)

    # Exactly one survivor of a specific request: that is the answer.
    if len(ranked) == 1:
        winner = ranked[0]
        return SelectionDecision(
            action=SelectionAction.PROPOSE,
            message=(
                f"{winner.product.product_name} is the valid match "
                "for the purchase intent."
            ),
            selected=winner,
        )

    best, second_best = ranked[0], ranked[1]

    # If preferences clearly distinguish one candidate, let the agent
    # propose it rather than forcing the human to choose every time.
    if best.score - second_best.score >= CLEAR_WINNER_MARGIN:
        return SelectionDecision(
            action=SelectionAction.PROPOSE,
            message=(
                f"{best.product.product_name} is the best match for the "
                "buyer's stated requirements and preferences."
            ),
            selected=best,
            alternatives=ranked[1:MAX_ALTERNATIVES],
        )

    # Several products remain similarly suitable.
    # Don't invent a preference that the buyer never expressed.
    option_text = "; ".join(
        f"{candidate.product.product_name} by {candidate.product.brand} "
        f"(Rs. {candidate.product.cost:.2f})"
        for candidate in ranked[:MAX_ALTERNATIVES]
    )

    return SelectionDecision(
        action=SelectionAction.CLARIFY,
        message=(
            "I found multiple similarly suitable options and don't "
            "have enough preference information to choose confidently: "
            f"{option_text}. Which do you prefer, or what matters most?"
        ),
        alternatives=ranked[:MAX_ALTERNATIVES],
    )


def selection_for_candidates(
    intent: Intent,
    candidates: list[RankedCandidate],
    *,
    chosen: Optional[RankedCandidate] = None,
    rationale: Optional[str] = None,
) -> SelectionDecision:
    """Build a PROPOSE decision around an already-validated survivor.

    `chosen` must have come out of `eligible_candidates`. Used by the AI
    ranking path, where the model nominates one of the survivors and the
    server -- not the model -- decides that the nomination is admissible.
    """
    if chosen is None:
        raise ValueError("selection_for_candidates requires a chosen candidate.")

    others = rank_candidates(
        intent, [c for c in candidates if c.product.product_id != chosen.product.product_id]
    )
    return SelectionDecision(
        action=SelectionAction.PROPOSE,
        message=rationale
        or (
            f"{chosen.product.product_name} best matches the stated "
            "requirements and preferences."
        ),
        selected=chosen,
        alternatives=others[: MAX_ALTERNATIVES - 1],
    )
