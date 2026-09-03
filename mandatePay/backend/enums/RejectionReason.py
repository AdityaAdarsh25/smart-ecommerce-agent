from enum import Enum


class RejectionReason(str, Enum):
    """Machine-readable reasons a quote or payment was refused.

    These are transport-level refusals -- the world changed, or the caller
    presented something inconsistent. They are NOT policy decisions, which
    remain the sole property of `PolicyDecision`.
    """

    # The catalogue price moved after the quote was taken. For v1 ANY change
    # invalidates the quote; the quoted price is never silently replaced.
    PRICE_DRIFT = "PRICE_DRIFT"

    QUOTE_EXPIRED = "QUOTE_EXPIRED"
    QUOTE_NOT_ACTIVE = "QUOTE_NOT_ACTIVE"
    QUOTE_NOT_FOUND = "QUOTE_NOT_FOUND"

    PRODUCT_NOT_FOUND = "PRODUCT_NOT_FOUND"
    INSUFFICIENT_STOCK = "INSUFFICIENT_STOCK"
    INSUFFICIENT_BALANCE = "INSUFFICIENT_BALANCE"

    # Payment verification refusals.
    TRANSACTION_NOT_FOUND = "TRANSACTION_NOT_FOUND"
    TRANSACTION_NOT_AWAITING_PAYMENT = "TRANSACTION_NOT_AWAITING_PAYMENT"
    ORDER_ID_MISMATCH = "ORDER_ID_MISMATCH"
    PAYMENT_ALREADY_SETTLED = "PAYMENT_ALREADY_SETTLED"
    SIGNATURE_VERIFICATION_FAILED = "SIGNATURE_VERIFICATION_FAILED"
    PROVIDER_ERROR = "PROVIDER_ERROR"

    # Settling this payment would take the buyer's VERIFIED spend for the
    # month of settlement past the mandate's monthly cap. Order creation
    # checks the cap too, but against the spend at that moment -- two
    # orders can each be affordable when created and unaffordable
    # together once one of them settles.
    MONTHLY_CAP_EXCEEDED = "MONTHLY_CAP_EXCEEDED"

    # The mandate that authorized this purchase no longer exists, so
    # there is nothing left to check the settlement against.
    MANDATE_NOT_FOUND = "MANDATE_NOT_FOUND"

    # Approval-flow refusals.
    APPROVAL_NOT_FOUND = "APPROVAL_NOT_FOUND"
    APPROVAL_ALREADY_RESOLVED = "APPROVAL_ALREADY_RESOLVED"
    TRANSACTION_NOT_AWAITING_APPROVAL = "TRANSACTION_NOT_AWAITING_APPROVAL"

    # A hard mandate rule failed on re-evaluation after human approval.
    # Human approval satisfies the approval threshold and nothing else.
    POLICY_BLOCKED = "POLICY_BLOCKED"

    # AI buyer agent refusals. Each means nothing was quoted; none is a
    # policy decision, and none is ever reported as a successful
    # interpretation of the buyer's request.
    BUYER_NOT_FOUND = "BUYER_NOT_FOUND"
    AGENT_NOT_CONFIGURED = "AGENT_NOT_CONFIGURED"
    AGENT_RESPONSE_INVALID = "AGENT_RESPONSE_INVALID"

    # The provider throttled the call. Nothing about the request was wrong
    # and nothing was quoted, so the same request may be retried shortly.
    AGENT_RATE_LIMITED = "AGENT_RATE_LIMITED"
