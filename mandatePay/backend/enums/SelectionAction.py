from enum import Enum


class SelectionAction(str, Enum):
    """What the buyer agent decided to do about *which product*.

    Strictly a product-selection outcome. None of these values authorizes
    spending; PROPOSE means "this is the product", after which the
    deterministic policy engine decides whether it may be bought.
    """

    # Nothing in the catalogue satisfies the buyer's hard requirements.
    NO_MATCH = "NO_MATCH"

    # The request is materially ambiguous, or no candidate stands out.
    # Never produces a quote.
    CLARIFY = "CLARIFY"

    # A single product has been identified and may now be quoted.
    PROPOSE = "PROPOSE"

    # The request is outside what v1 can safely handle -- in practice a
    # multi-item request, which must not be silently split into separate
    # transactions that would each slip under the mandate limits.
    UNSUPPORTED = "UNSUPPORTED"
