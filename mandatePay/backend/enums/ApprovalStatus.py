from enum import Enum


class ApprovalStatus(str, Enum):
    """Where a human approval request stands.

    Deliberately separate from both `PolicyDecision` and
    `TransactionStatus`. An APPROVED approval means "a human said yes to
    the spending threshold", not "the purchase may proceed" and certainly
    not "money moved" -- every hard rule is re-evaluated after approval.
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


# The only status that still needs a human. Exactly one approval per
# transaction may sit here at a time.
UNRESOLVED_APPROVAL_STATUSES = frozenset({ApprovalStatus.PENDING})
