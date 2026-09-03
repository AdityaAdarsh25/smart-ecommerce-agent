"""Typed API responses.

Every endpoint this package touches declares its shape here, so Swagger
documents a real schema instead of an untyped object -- and so the
money fields are Decimal at the boundary rather than float.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.enums.ApprovalStatus import ApprovalStatus
from backend.enums.AuditEventType import AuditEventType
from backend.enums.QuoteStatus import QuoteStatus
from backend.enums.RejectionReason import RejectionReason
from backend.enums.TransactionStatus import TransactionStatus
from backend.models.PolicyResult import PolicyResult


class ProductSummary(BaseModel):
    """The product as it stood when the quote was taken."""

    product_id: UUID
    product_name: str
    brand: str
    category: Optional[str] = None
    merchant_id: UUID


class QuoteResponse(BaseModel):
    """A persisted, immutable price snapshot plus the policy verdict.

    Carries a PolicyDecision and no TransactionStatus: being told what
    something costs and whether you may buy it is not a financial event.
    """

    quote_id: UUID
    buyer_id: UUID
    product: ProductSummary
    quantity: int
    quoted_unit_price: Decimal
    quoted_total: Decimal
    created_at: datetime
    expires_at: datetime
    quote_status: QuoteStatus
    policy: PolicyResult


class TransactionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    buyer_id: UUID
    product_id: UUID
    quote_id: Optional[UUID] = None
    quantity: Optional[int] = None
    amount: Decimal
    status: TransactionStatus
    reason: str
    razorpay_order_id: Optional[str] = None
    razorpay_payment_id: Optional[str] = None
    timestamp: datetime
    # Null unless this transaction has actually been settled.
    paid_at: Optional[datetime] = None


class RazorpayOrderInfo(BaseModel):
    """What a checkout front-end needs, all of it derived server-side."""

    razorpay_order_id: str
    amount_in_paise: int
    currency: str = "INR"


class CreateOrderResponse(BaseModel):
    """The outcome of attempting a purchase.

    `transaction.status` is the truth. ORDER_CREATED means a Razorpay
    order exists and nothing has been collected; it is not PAID.
    """

    transaction: TransactionResponse
    policy: PolicyResult
    razorpay: Optional[RazorpayOrderInfo] = None
    # Present exactly when the transaction is AWAITING_APPROVAL: the one
    # unresolved request a human must now resolve.
    approval: Optional["ApprovalResponse"] = None


class PaymentVerificationResponse(BaseModel):
    """The only response in the system that can report PAID."""

    verified: bool
    # True when this exact payment had already been verified and settled.
    # The replay performed no second mutation.
    already_verified: bool
    transaction: TransactionResponse
    quote_status: QuoteStatus
    buyer_balance: Decimal
    product_stock_remaining: int


class RejectionDetail(BaseModel):
    """Body of a 4xx refusal, so failures are machine-readable too."""

    reason: RejectionReason
    message: str
    requote_required: bool = False
    quoted_unit_price: Optional[Decimal] = None
    current_unit_price: Optional[Decimal] = None
    # Where the transaction actually ended up, read from the row AFTER the
    # refusal was applied. Set on payment-verification refusals and on a
    # provider order-creation failure, so a caller is not left rendering
    # the state the transaction was in before it failed. It reports state;
    # it never asserts one -- a refusal can no more write PAID here than
    # anywhere else.
    transaction_status: Optional[TransactionStatus] = None
    # Which transaction that status belongs to, set alongside it when the
    # refusal ended a transaction the caller may not yet have an id for.
    # Carries no amount and no order id: a refusal reports where the row
    # is, never what it is worth.
    transaction_id: Optional[UUID] = None
    # Only ever set on a rate-limit refusal, and only when the provider
    # sent a usable hint. Absent is the normal case; a caller must not
    # depend on it to decide whether retrying is allowed.
    retry_after_seconds: Optional[float] = None


# ---------------------------------------------------------------------------
# Delegated authority and human approval
# ---------------------------------------------------------------------------


class ApprovalResponse(BaseModel):
    """A human-approval request and, once resolved, how it was resolved."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    transaction_id: UUID
    status: ApprovalStatus
    reason: str
    created_at: datetime
    resolved_at: Optional[datetime] = None
    reviewer: Optional[str] = None
    reviewer_note: Optional[str] = None


class PendingApprovalsResponse(BaseModel):
    """Everything currently waiting on a human."""

    count: int
    approvals: list[ApprovalResponse]


class ApprovalDecisionRequest(BaseModel):
    """What the demo approver optionally says about their decision.

    Deliberately carries no amount, no price and no policy override. A
    human resolves the approval threshold and nothing else.
    """

    reviewer: Optional[str] = None
    note: Optional[str] = None


class ApprovalResolutionResponse(BaseModel):
    """The outcome of approving or rejecting a transaction.

    `order_created` is the honest headline. An approval that passed
    re-validation produces a Razorpay order (ORDER_CREATED, still not
    paid); one that did not leaves the transaction in a safe terminal
    state with `revalidation` explaining why, and no provider call was
    made either way until it succeeded.
    """

    approval: ApprovalResponse
    transaction: TransactionResponse
    order_created: bool
    # The re-evaluation performed at approval time, with the
    # already-satisfied autonomous threshold suppressed.
    policy: Optional[PolicyResult] = None
    razorpay: Optional[RazorpayOrderInfo] = None
    # Why execution was refused after approval, if it was.
    revalidation: Optional["RejectionDetail"] = None


class MandateSummary(BaseModel):
    """The delegated authority itself, so a demo can show what governs a
    decision before making one."""

    mandate_id: UUID
    autonomous_limit: Decimal
    absolute_transaction_limit: Decimal
    monthly_cap: Decimal
    monthly_spent: Decimal
    allowed_merchants: list[UUID]
    allowed_categories: list[str]


class DemoBuyerResponse(BaseModel):
    """A seeded demo identity.

    There is no authentication in v1: selecting a demo buyer *is* the
    identity step, and `buyer_id` is the only thing carried forward.
    """

    buyer_id: UUID
    name: str
    balance: Decimal
    mandate: Optional[MandateSummary] = None


CreateOrderResponse.model_rebuild()
ApprovalResolutionResponse.model_rebuild()


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------


class AuditEventResponse(BaseModel):
    """One recorded fact, as read back.

    `sequence` is exposed rather than hidden: it is what makes the order
    of two events written in the same clock tick unambiguous, and a reader
    checking the story hangs together needs to see it.
    """

    model_config = ConfigDict(from_attributes=True)

    sequence: int
    event_type: AuditEventType
    timestamp: datetime
    buyer_id: Optional[UUID] = None
    quote_id: Optional[UUID] = None
    transaction_id: Optional[UUID] = None
    approval_id: Optional[UUID] = None
    details: dict[str, Any] = Field(default_factory=dict)


class TransactionAuditResponse(BaseModel):
    """The whole story of one transaction, oldest event first.

    Includes the events recorded against its quote before the transaction
    existed, because that is where the story starts.

    This is a read of history and nothing else. No endpoint consults it,
    and returning it cannot change any transaction's state.
    """

    transaction_id: UUID
    status: TransactionStatus
    quote_id: Optional[UUID] = None
    event_count: int
    events: list[AuditEventResponse]


# ---------------------------------------------------------------------------
# Demo front-end support
# ---------------------------------------------------------------------------


class PublicConfigResponse(BaseModel):
    """The only configuration the browser is ever given.

    `razorpay_key_id` is the PUBLIC Razorpay key -- the value Checkout
    itself requires in the page. There is deliberately no field here for
    a secret: RAZORPAY_KEY_SECRET and OPENAI_API_KEY are never read by
    this model, so no code path exists that could serialise one.

    The two booleans exist so the UI can say "not configured" plainly
    instead of failing somewhere inside a provider SDK.
    """

    razorpay_key_id: Optional[str] = None
    razorpay_configured: bool = False
    agent_configured: bool = False


class EvaluationGroupSummary(BaseModel):
    """One scenario group, as recorded by the evaluation run."""

    name: str
    total: int
    passed: int
    failed: int
    pass_rate: float


class EvaluationSummaryResponse(BaseModel):
    """The accepted Package-5 evaluation result, read from disk.

    Every number here comes out of `evaluation/results.json`, which is
    written by `run_evaluation.py`. Nothing is computed in this process
    and nothing is hardcoded, so the badge in the demo UI cannot drift
    away from the committed evidence.
    """

    generated_at: Optional[str] = None
    scenarios: int
    passed: int
    failed: int
    pass_rate: float
    unsafe_financial_bypass_count: int
    unsafe_financial_bypass_rate: float
    live_provider_calls: int
    groups: list[EvaluationGroupSummary] = Field(default_factory=list)
