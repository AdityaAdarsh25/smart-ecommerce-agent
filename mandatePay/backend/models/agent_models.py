"""Request and response shapes for the natural-language buyer agent.

Kept separate from `responses.py` so the boundary is easy to read: this
file is the entire public surface of the AI entry point.

Two things are deliberately absent from the request: any price field and
any policy field. The caller says who is buying and what they said. It
cannot say what something costs, what it may cost, or whether it is
allowed -- there is nowhere to put such a value.
"""

from decimal import Decimal
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field

from backend.enums.NextAction import NextAction
from backend.enums.SelectionAction import SelectionAction
from backend.models.PolicyResult import PolicyResult
from backend.models.responses import ProductSummary, QuoteResponse

MAX_MESSAGE_LENGTH = 1000


class AgentPurchaseRequest(BaseModel):
    """Everything the demo client is allowed to send.

    `buyer_id` is the Buildathon identity step -- a demo buyer is selected,
    not authenticated. Production authentication is explicitly future work
    and is not simulated here.
    """

    buyer_id: UUID
    message: str = Field(..., min_length=1, max_length=MAX_MESSAGE_LENGTH)


class InterpretedIntent(BaseModel):
    """How the language model read the buyer's sentence.

    Reported so a demo can show the AI's actual contribution. Every value
    is a reading of the REQUEST; none of it is a fact about the catalogue
    or the buyer's authority. `max_budget` in particular is what the buyer
    asked for, never what anything costs.
    """

    raw_text: str
    item: str
    quantity: int
    max_budget: Optional[float] = None
    required_brand: Optional[str] = None
    required_category: Optional[str] = None
    hard_constraints: list[str] = Field(default_factory=list)
    preferred_brand: Optional[str] = None
    soft_preferences: list[str] = Field(default_factory=list)
    is_multi_item: bool = False
    requested_items: list[str] = Field(default_factory=list)


class SelectedProduct(BaseModel):
    """The product the agent settled on, as the SERVER knows it.

    `unit_price` is read from the product row, not from anything the model
    wrote. It is echoed here only so a demo can show that the quoted total
    is this price times this quantity.
    """

    product: ProductSummary
    unit_price: Decimal
    rationale: str
    # How many catalogue rows survived the hard-constraint filter and were
    # therefore eligible to be chosen from.
    eligible_candidates: int
    alternatives: list[ProductSummary] = Field(default_factory=list)


class AgentPurchaseResponse(BaseModel):
    """The outcome of one natural-language purchase request.

    The AI contributed `intent` and the free-text `rationale`. Everything
    financial -- `quote`, `policy`, `next_action` -- was produced by
    deterministic server code from database rows.

    `next_action` is derived, never chosen: CREATE_ORDER means the caller
    may now attempt payment through the existing order endpoint. Nothing
    in this response can report a payment, and this endpoint never writes
    a transaction, contacts Razorpay, or sets PAID.
    """

    buyer_id: UUID
    selection_action: SelectionAction
    next_action: NextAction
    message: str

    intent: Optional[InterpretedIntent] = None
    selection: Optional[SelectedProduct] = None

    # Present only for a PROPOSE outcome. A clarification, a no-match and
    # an unsupported request all carry no quote and no policy verdict,
    # because nothing was priced.
    quote: Optional[QuoteResponse] = None
    policy: Optional[PolicyResult] = None

    clarification_question: Optional[str] = None

    # Server-side observations worth surfacing -- notably when an AI
    # answer was discarded and the deterministic ranking was used instead.
    notes: list[str] = Field(default_factory=list)
