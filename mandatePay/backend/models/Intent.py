from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


class Intent(BaseModel):
    """The structured reading of what the buyer asked for.

    This is the ONLY thing the language model is allowed to author on the
    purchase path, and even here it authors a *description of a wish* --
    never a price, a limit, a permission or a verdict.

    Hard vs soft is the load-bearing distinction:

      * hard fields (`max_budget`, `required_brand`, `required_category`,
        `hard_constraints`) are non-negotiable and FILTER the catalogue.
      * soft fields (`preferred_brand`, `preferred_merchant`,
        `soft_preferences`) only RANK whatever survived that filter.

    A product that fails a hard constraint can therefore never win on
    preferences, because it is not in the set being ranked.
    """

    buyer_id: UUID
    raw_text: str
    item: str
    max_budget: Optional[float] = None
    quantity: int = 1  # only kicks in if it isnt explicity provided

    # --- Hard, non-negotiable requirements -------------------------------
    required_brand: Optional[str] = None
    required_category: Optional[str] = None
    # Free-text requirements the buyer stated as absolute ("must be
    # wireless"). Verified against catalogue text and nothing else -- see
    # `selection_decision._passes_structured_hard_constraints`.
    hard_constraints: list[str] = Field(default_factory=list)

    # --- Soft, ranking-only preferences ----------------------------------
    preferred_brand: Optional[str] = None
    preferred_merchant: Optional[str] = None
    soft_preferences: list[str] = Field(default_factory=list)

    # --- Request shape ----------------------------------------------------
    # v1 is deliberately single-item. A request naming several distinct
    # goods is refused rather than split, because two independent
    # transactions could each pass a limit the combined purchase would not.
    is_multi_item: bool = False
    requested_items: list[str] = Field(default_factory=list)

    needs_clarification: bool = False
    clarification_question: Optional[str] = None
