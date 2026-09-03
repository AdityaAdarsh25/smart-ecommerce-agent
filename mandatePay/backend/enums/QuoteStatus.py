from enum import Enum


class QuoteStatus(str, Enum):
    """Where a persisted price quote is in its own short lifecycle.

    Deliberately separate from `TransactionStatus`: a quote is a priced
    offer with an expiry, not a financial event. There is no QUOTED
    transaction state and there must never be one.
    """

    # Live and usable: the quoted price may still be turned into an order.
    ACTIVE = "active"

    # A verified payment used this quote. Terminal, and never reusable --
    # this is what stops one quote funding two purchases.
    CONSUMED = "consumed"

    # Timed out, or invalidated because the world moved underneath it
    # (price drift). The buyer must re-quote.
    EXPIRED = "expired"

    # Withdrawn deliberately.
    CANCELLED = "cancelled"


# The only status from which an order may be created.
USABLE_QUOTE_STATUSES = frozenset({QuoteStatus.ACTIVE})
