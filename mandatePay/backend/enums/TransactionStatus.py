from enum import Enum


class TransactionStatus(str, Enum):
    """Where a transaction actually is in the financial lifecycle.

    This answers ONLY: "What is the real payment state?"

    It is NOT a policy decision. `PolicyDecision` answers authorization.

    Deliberately absent:
      - COMPLETED  -- ambiguous; it previously meant "policy approved" while
                      implying "paid". Replaced by AUTHORIZED / ORDER_CREATED / PAID.
      - QUOTED     -- a quote is not a transaction. It becomes its own
                      persisted entity in Package 2.
    """

    # Policy said REQUIRE_APPROVAL. No money movement attempted.
    AWAITING_APPROVAL = "awaiting_approval"

    # Policy said ALLOW. The agent may attempt payment. Nothing paid yet.
    AUTHORIZED = "authorized"

    # A Razorpay order exists. Still NOT paid -- an order is an intent to
    # collect, not a collection.
    ORDER_CREATED = "order_created"

    # Payment verified against the provider. This is the ONLY state that
    # means money moved, and only payment verification may set it.
    PAID = "paid"

    # Attempted but did not succeed (e.g. provider error).
    FAILED = "failed"

    # Policy said BLOCK.
    BLOCKED = "blocked"

    # Withdrawn by buyer or approver.
    CANCELLED = "cancelled"


# Statuses representing a live or already-honoured commitment for the same
# purchase. A new identical attempt while one of these is recent is a
# duplicate -- this is what stops repeated identical approval requests.
ACTIVE_DUPLICATE_STATUSES = frozenset(
    {
        TransactionStatus.AWAITING_APPROVAL,
        TransactionStatus.AUTHORIZED,
        TransactionStatus.ORDER_CREATED,
        TransactionStatus.PAID,
    }
)

# Dead attempts. These must never block a legitimate retry.
RETRYABLE_STATUSES = frozenset(
    {
        TransactionStatus.BLOCKED,
        TransactionStatus.FAILED,
        TransactionStatus.CANCELLED,
    }
)

# Only verified payments consume the buyer's monthly allowance.
SPEND_COUNTING_STATUSES = frozenset({TransactionStatus.PAID})
