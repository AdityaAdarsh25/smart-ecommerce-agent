"""Human resolution of transactions the mandate would not let the agent
execute alone.

The load-bearing idea of this module is that approval is narrow. A human
approver answers exactly one question -- "may this agent spend above its
autonomous threshold?" -- and nothing else. Everything the mandate treats
as a hard rule is re-evaluated from scratch after the human says yes, and
any of it can still stop the purchase:

    approval  ->  suppresses the autonomous-threshold approval reason
    approval  ->  never suppresses a hard violation

That is why approving does not itself create a Razorpay order. It
re-opens the question, with the threshold already answered.
"""

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.commerce.product_routes import (
    _evaluate,
    create_razorpay_order,
    quote_revalidation_failure,
    unresolved_approval,
)
from backend.commerce.serialization import money_critical_section
from backend.audit import record_event, record_policy
from backend.database import get_db, utcnow_naive
from backend.databases.approval_db import approval_db
from backend.databases.buyer_db import buyer_db
from backend.databases.mandate_db import mandate_db
from backend.databases.product_db import product_db
from backend.databases.quote_db import quote_db
from backend.databases.transaction_db import transaction_db
from backend.enums.ApprovalStatus import ApprovalStatus
from backend.enums.AuditEventType import AuditEventType
from backend.enums.PolicyDecision import PolicyDecision
from backend.enums.QuoteStatus import QuoteStatus
from backend.enums.RejectionReason import RejectionReason
from backend.enums.TransactionStatus import TransactionStatus
from backend.models.PolicyResult import PolicyResult
from backend.models.responses import (
    ApprovalDecisionRequest,
    ApprovalResolutionResponse,
    ApprovalResponse,
    RejectionDetail,
    TransactionResponse,
)
from backend.money import from_paise, to_paise

router = APIRouter(prefix="/app/v1", tags=["approvals"])

# There is no user system, and building one is out of scope for v1. This
# is the name recorded when a demo approver does not give one.
DEMO_REVIEWER = "demo-approver"


def _reject(status_code: int, reason: RejectionReason, message: str) -> None:
    detail = RejectionDetail(reason=reason, message=message)
    raise HTTPException(status_code=status_code, detail=detail.model_dump(mode="json"))


def _load_resolvable(db: Session, transaction_id: UUID):
    """The transaction and its one outstanding approval, or a refusal.

    Refuses a second resolution rather than quietly re-resolving: an
    approval that has already been answered is a record of what a human
    decided, and overwriting it would erase that.
    """
    txn = db.get(transaction_db, str(transaction_id))
    if txn is None:
        _reject(404, RejectionReason.TRANSACTION_NOT_FOUND, "Transaction not found.")

    approval = unresolved_approval(db, txn.id)
    if approval is None:
        resolved = (
            db.query(approval_db)
            .filter(approval_db.transaction_id == str(txn.id))
            .first()
        )
        if resolved is not None:
            _reject(
                409,
                RejectionReason.APPROVAL_ALREADY_RESOLVED,
                f"This approval was already {resolved.status.value} at "
                f"{resolved.resolved_at}. It cannot be resolved again.",
            )
        _reject(
            404,
            RejectionReason.APPROVAL_NOT_FOUND,
            "No approval request exists for this transaction.",
        )

    if txn.status is not TransactionStatus.AWAITING_APPROVAL:
        _reject(
            409,
            RejectionReason.TRANSACTION_NOT_AWAITING_APPROVAL,
            f"Transaction is {txn.status.value}; only an AWAITING_APPROVAL "
            "transaction can be approved or rejected.",
        )

    return txn, approval


_RESOLUTION_EVENTS = {
    ApprovalStatus.APPROVED: AuditEventType.APPROVAL_APPROVED,
    ApprovalStatus.REJECTED: AuditEventType.APPROVAL_REJECTED,
}


def _resolve(
    db: Session,
    txn: transaction_db,
    approval: approval_db,
    status: ApprovalStatus,
    decision: Optional[ApprovalDecisionRequest],
) -> None:
    approval.status = status
    approval.resolved_at = utcnow_naive()
    approval.reviewer = (decision.reviewer if decision else None) or DEMO_REVIEWER
    approval.reviewer_note = decision.note if decision else None
    db.commit()
    db.refresh(approval)

    # What the human actually said. Recorded here, before re-validation
    # runs, and never rewritten afterwards -- an approval that is later
    # overtaken by a hard rule was still genuinely granted, and the trail
    # has to keep both facts.
    record_event(
        db,
        _RESOLUTION_EVENTS[status],
        buyer_id=txn.buyer_id,
        quote_id=txn.quote_id,
        transaction_id=txn.id,
        approval_id=approval.id,
        reviewer=approval.reviewer,
        resolved_at=approval.resolved_at,
        approval_reason=approval.reason,
    )


def _halted(
    db: Session,
    *,
    txn: transaction_db,
    approval: approval_db,
    detail: RejectionDetail,
    policy: Optional[PolicyResult] = None,
) -> ApprovalResolutionResponse:
    """The approval stands; the purchase does not.

    The transaction lands in a terminal BLOCKED state carrying the explicit
    reason, and the provider was never contacted. Nothing is left sitting
    in AWAITING_APPROVAL, so there is no path back into the approval loop.
    """
    txn.status = TransactionStatus.BLOCKED
    txn.reason = f"{txn.reason} | after approval: {detail.reason.value}: {detail.message}"
    db.commit()
    db.refresh(txn)

    record_event(
        db,
        AuditEventType.APPROVAL_REVALIDATION_FAILED,
        buyer_id=txn.buyer_id,
        quote_id=txn.quote_id,
        transaction_id=txn.id,
        approval_id=approval.id,
        # The human approval above stands. This is the separate fact that
        # a hard rule failed afterwards.
        approval_status=approval.status,
        revalidation_reason=detail.reason,
        message=detail.message,
        violation_codes=list(policy.violation_codes) if policy is not None else [],
        provider_called=False,
        order_created=False,
    )
    record_event(
        db,
        AuditEventType.TRANSACTION_BLOCKED,
        buyer_id=txn.buyer_id,
        quote_id=txn.quote_id,
        transaction_id=txn.id,
        status_from=TransactionStatus.AWAITING_APPROVAL,
        status_to=TransactionStatus.BLOCKED,
        reason=detail.reason,
        provider_called=False,
    )

    return ApprovalResolutionResponse(
        approval=ApprovalResponse.model_validate(approval),
        transaction=TransactionResponse.model_validate(txn),
        order_created=False,
        policy=policy,
        revalidation=detail,
    )


@router.post(
    "/transactions/{transaction_id}/approve",
    response_model=ApprovalResolutionResponse,
)
def approve_transaction(
    transaction_id: UUID,
    decision: Optional[ApprovalDecisionRequest] = None,
    db: Session = Depends(get_db),
) -> ApprovalResolutionResponse:
    """Grant the spending-threshold exception, then re-earn the purchase.

    The world may have moved while the request sat waiting, so after the
    human says yes everything is read again from the database -- quote,
    quote status and expiry, current catalogue price, stock, buyer
    balance, monthly PAID spend, absolute limit, merchant and category
    permission, duplicate state -- and re-evaluated.

    Only the autonomous-threshold approval reason is suppressed, because
    only that is what the human answered. If any hard rule now fails, no
    Razorpay order is created and the transaction ends BLOCKED with the
    reason recorded.

    Resolution and re-evaluation happen inside the shared money critical
    section, so a concurrent order creation or settlement cannot land
    between what this reads and what it commits -- and so two approvals
    racing on the same transaction cannot both decide it is theirs to
    execute. The section is released before the provider is contacted;
    AUTHORIZED is already committed by then.
    """
    with money_critical_section():
        outcome, txn, approval, quote, result, total_paise = _resolve_and_authorize(
            db, transaction_id, decision
        )
    if outcome is not None:
        return outcome

    razorpay_info = create_razorpay_order(
        db,
        txn=txn,
        quote=quote,
        total_paise=total_paise,
        summary=txn.reason,
    )

    return ApprovalResolutionResponse(
        approval=ApprovalResponse.model_validate(approval),
        transaction=TransactionResponse.model_validate(txn),
        order_created=True,
        policy=result,
        razorpay=razorpay_info,
    )


def _resolve_and_authorize(
    db: Session,
    transaction_id: UUID,
    decision: Optional[ApprovalDecisionRequest],
):
    """Record the human decision and re-earn the purchase.

    MUST be called with the money critical section held: everything it
    re-reads -- quote state, price, stock, balance, monthly PAID spend,
    duplicate state -- is state another money action can be changing, and
    it commits AUTHORIZED on the strength of those reads.

    Returns `(response, txn, approval, quote, result, total_paise)`. A
    non-None `response` is a terminal outcome that never reaches the
    provider, and the caller returns it unchanged.
    """
    txn, approval = _load_resolvable(db, transaction_id)

    _resolve(db, txn, approval, ApprovalStatus.APPROVED, decision)

    quote = db.get(quote_db, str(txn.quote_id)) if txn.quote_id else None
    if quote is None:
        return (
            _halted(
                db,
                txn=txn,
                approval=approval,
                detail=RejectionDetail(
                    reason=RejectionReason.QUOTE_NOT_FOUND,
                    message="The approved transaction has no quote to execute.",
                    requote_required=True,
                ),
            ),
            txn,
            approval,
            None,
            None,
            None,
        )

    product = db.get(product_db, str(quote.product_id))

    # Quote, expiry, product, stock and price drift -- the same checks
    # order creation makes, turned into a safe terminal state instead of a
    # 4xx, because this transaction already exists.
    failure = quote_revalidation_failure(db, quote, product, transaction_id=txn.id)
    if failure is not None:
        _, detail = failure
        return (
            _halted(db, txn=txn, approval=approval, detail=detail),
            txn,
            approval,
            quote,
            None,
            None,
        )

    buyer = db.get(buyer_db, str(txn.buyer_id))
    if buyer is None:
        raise HTTPException(status_code=404, detail="Buyer not found")

    mandate = (
        db.query(mandate_db).filter(mandate_db.buyer_id == str(txn.buyer_id)).first()
    )
    if mandate is None:
        raise HTTPException(status_code=404, detail="Mandate not found for this buyer")

    total_paise = to_paise(quote.quoted_total)
    amount = float(from_paise(total_paise))

    result = _evaluate(
        db,
        buyer=buyer,
        mandate=mandate,
        product=product,
        buyer_id=str(txn.buyer_id),
        product_id=str(txn.product_id),
        quantity=quote.quantity,
        amount=amount,
        # The threshold has been answered by a human. This suppresses that
        # one approval reason and nothing else.
        human_approved=True,
        # ...and the transaction must not be seen as its own duplicate.
        exclude_transaction_id=txn.id,
    )

    if result.decision is not PolicyDecision.ALLOW:
        # BLOCK, or the impossible-by-construction REQUIRE_APPROVAL. Either
        # way this ends here rather than going round again.
        return (
            _halted(
                db,
                txn=txn,
                approval=approval,
                detail=RejectionDetail(
                    reason=RejectionReason.POLICY_BLOCKED,
                    message=result.summary,
                ),
                policy=result,
            ),
            txn,
            approval,
            quote,
            result,
            total_paise,
        )

    # Approved and still valid. AUTHORIZED is committed before the provider
    # is contacted, exactly as on the autonomous path.
    txn.status = TransactionStatus.AUTHORIZED
    txn.reason = f"{result.summary} | approved by {approval.reviewer}"
    db.commit()

    record_policy(
        db,
        result,
        context="post_approval",
        buyer_id=txn.buyer_id,
        quote_id=quote.id,
        transaction_id=txn.id,
        amount=amount,
        human_approval_required=True,
        human_approved=True,
        reviewer=approval.reviewer,
    )

    return None, txn, approval, quote, result, total_paise


@router.post(
    "/transactions/{transaction_id}/reject",
    response_model=ApprovalResolutionResponse,
)
def reject_transaction(
    transaction_id: UUID,
    decision: Optional[ApprovalDecisionRequest] = None,
    db: Session = Depends(get_db),
) -> ApprovalResolutionResponse:
    """Refuse the request. Nothing moves.

    The provider is never contacted, no balance or stock is touched, and
    the quote is CANCELLED rather than CONSUMED -- a rejected purchase must
    not leave a trace that looks like a paid one.

    It takes the same money critical section the approval path does, so a
    rejection and an approval arriving together cannot both find the
    transaction resolvable and reach opposite conclusions about it.
    Nothing here contacts the provider, so the section covers the whole of
    it.
    """
    with money_critical_section():
        txn, approval = _load_resolvable(db, transaction_id)

        _resolve(db, txn, approval, ApprovalStatus.REJECTED, decision)

        txn.status = TransactionStatus.CANCELLED
        txn.reason = f"{txn.reason} | rejected by {approval.reviewer}"

        quote = db.get(quote_db, str(txn.quote_id)) if txn.quote_id else None
        if quote is not None and quote.status is QuoteStatus.ACTIVE:
            quote.status = QuoteStatus.CANCELLED

        db.commit()
        db.refresh(txn)

    record_event(
        db,
        AuditEventType.TRANSACTION_CANCELLED,
        buyer_id=txn.buyer_id,
        quote_id=txn.quote_id,
        transaction_id=txn.id,
        approval_id=approval.id,
        status_from=TransactionStatus.AWAITING_APPROVAL,
        status_to=TransactionStatus.CANCELLED,
        reviewer=approval.reviewer,
        quote_status=quote.status if quote is not None else None,
        provider_called=False,
    )

    return ApprovalResolutionResponse(
        approval=ApprovalResponse.model_validate(approval),
        transaction=TransactionResponse.model_validate(txn),
        order_created=False,
    )
