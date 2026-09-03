"""Catalogue, quoting, order creation and payment verification.

The single most important property of this module is where PAID can be
written. It is written in exactly one place -- the settlement step of
`verify_payment`, after a server-side signature check -- and the buyer's
balance and the product's stock move in that same database transaction
and nowhere else.

Every money action here -- order creation and settlement -- runs its
read-decide-commit section inside the one shared critical section in
`backend.commerce.serialization`, because each of those checks reads
state that the action itself then changes.
"""

import os
from datetime import timedelta
from decimal import Decimal
from typing import Optional

import razorpay
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import extract, func
from sqlalchemy.orm import Session

from backend.audit import events_for_transaction, record_event, record_policy
from backend.commerce.serialization import money_critical_section
from backend.database import get_db, utcnow_naive
from backend.databases.approval_db import approval_db
from backend.databases.audit_db import audit_event_db  # noqa: F401  (registers the table)
from backend.databases.buyer_db import buyer_db
from backend.databases.mandate_db import mandate_db
from backend.databases.product_db import product_db
from backend.databases.quote_db import quote_db
from backend.databases.transaction_db import transaction_db
from backend.enums.ApprovalStatus import (
    UNRESOLVED_APPROVAL_STATUSES,
    ApprovalStatus,
)
from backend.enums.AuditEventType import AuditEventType
from backend.enums.PolicyDecision import PolicyDecision
from backend.enums.QuoteStatus import QuoteStatus
from backend.enums.RejectionReason import RejectionReason
from backend.enums.TransactionStatus import (
    ACTIVE_DUPLICATE_STATUSES,
    SPEND_COUNTING_STATUSES,
    TransactionStatus,
)
from backend.models.PolicyResult import PolicyResult
from backend.models.create_order_request import CreateOrderRequest
from backend.models.order_request import OrderRequest
from backend.models.payment_request import PaymentVerificationRequest
from backend.models.responses import (
    ApprovalResponse,
    AuditEventResponse,
    CreateOrderResponse,
    DemoBuyerResponse,
    MandateSummary,
    PaymentVerificationResponse,
    PendingApprovalsResponse,
    ProductSummary,
    QuoteResponse,
    RazorpayOrderInfo,
    RejectionDetail,
    TransactionAuditResponse,
    TransactionResponse,
)
from backend.money import from_paise, to_paise
from backend.policies.policy_engine import evaluate_transaction

load_dotenv()
key_id = os.getenv("RAZORPAY_KEY_ID")
key_secret = os.getenv("RAZORPAY_KEY_SECRET")
router = APIRouter(prefix="/app/v1")

DUPLICATE_WINDOW = timedelta(minutes=5)

# Short and deterministic on purpose: the whole point of a quote is that the
# price behind it has not had time to move.
QUOTE_TTL = timedelta(minutes=5)


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def _reject(status_code: int, reason: RejectionReason, message: str, **extra) -> None:
    """Raise a machine-readable refusal.

    A refusal is not a policy decision. It means the request could not be
    honoured as presented -- the world moved, or the caller supplied
    something inconsistent.
    """
    detail = RejectionDetail(reason=reason, message=message, **extra)
    raise HTTPException(status_code=status_code, detail=detail.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------


@router.get("/product/{product_id}")
def get_product(product_id: str, db: Session = Depends(get_db)):
    product = db.get(product_db, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product Not Found")
    return product


def search_products(
    db: Session,
    *,
    item: str,
    brand: str | None = None,
    max_price: float | None = None,
) -> list[product_db]:
    """The catalogue query itself, callable without an HTTP request.

    Extracted so the AI buyer agent discovers products through exactly the
    same query the public search endpoint uses. The agent must never have
    a private catalogue, and it must never be able to consider anything
    that is not a real row.
    """
    query = db.query(product_db).filter(product_db.product_name.ilike(f"%{item}%"))
    if brand is not None:
        query = query.filter(product_db.brand.ilike(f"%{brand}%"))
    if max_price is not None:
        query = query.filter(product_db.cost <= max_price)
    return query.all()


@router.get("/search")
def search_catalog(
    item: str | None = None,
    brand: str | None = None,
    max_price: float | None = None,
    db: Session = Depends(get_db),
):
    if item is None:
        raise HTTPException(status_code=400, detail="No item provided,please try again")

    return search_products(db, item=item, brand=brand, max_price=max_price)


# ---------------------------------------------------------------------------
# Policy evaluation
# ---------------------------------------------------------------------------


# The month a PAID transaction is attributed to. `paid_at` is when the
# money actually moved and is the authoritative answer; `timestamp` is
# only when the transaction was created, which is a different question --
# an order created on 31 August and settled on 1 September spends
# September's allowance.
#
# The COALESCE covers PAID rows written before `paid_at` existed, whose
# true settlement time nobody recorded. Falling back to their creation
# timestamp keeps them counting against a cap rather than silently
# vanishing from it, which is the conservative direction: it can only ever
# leave a buyer with less allowance, never more.
_SETTLEMENT_MONTH = func.coalesce(transaction_db.paid_at, transaction_db.timestamp)


def _monthly_spend(db: Session, buyer_id: str, *, at=None) -> float:
    """Spend that has actually left the buyer's allowance in a given month.

    Only verified payments count. An authorized-but-unpaid transaction has
    not consumed anything, so counting it would let a stalled attempt
    permanently eat into the buyer's cap.

    `at` names the month to total, defaulting to now. Settlement passes
    the moment it is about to write, so the cap it checks is the cap for
    the month the money is moving in.
    """
    when = at or utcnow_naive()
    total = (
        db.query(func.sum(transaction_db.amount))
        .filter(
            transaction_db.buyer_id == buyer_id,
            transaction_db.status.in_(tuple(SPEND_COUNTING_STATUSES)),
            extract("year", _SETTLEMENT_MONTH) == when.year,
            extract("month", _SETTLEMENT_MONTH) == when.month,
        )
        .scalar()
    )
    return total or 0.0


def _has_active_duplicate(
    db: Session,
    buyer_id: str,
    product_id: str,
    quantity: int,
    amount: float,
    exclude_transaction_id=None,
) -> bool:
    """Is there already a live commitment for this exact purchase?

    Only statuses representing a live or honoured commitment count.
    BLOCKED / FAILED / CANCELLED attempts are dead and must never stop a
    legitimate retry -- previously a blocked attempt poisoned its own
    five-minute window.

    Matching on quantity as well as amount is also what prevents an
    unbounded pile of identical unresolved approval requests.

    `exclude_transaction_id` exists for re-evaluation: when an
    AWAITING_APPROVAL transaction is re-checked at approval time it would
    otherwise find *itself* in the window and block itself as its own
    duplicate.
    """
    cutoff = utcnow_naive() - DUPLICATE_WINDOW
    query = db.query(transaction_db).filter(
        transaction_db.buyer_id == buyer_id,
        transaction_db.product_id == product_id,
        transaction_db.quantity == quantity,
        transaction_db.amount == amount,
        transaction_db.status.in_(tuple(ACTIVE_DUPLICATE_STATUSES)),
        transaction_db.timestamp >= cutoff,
    )
    if exclude_transaction_id is not None:
        query = query.filter(transaction_db.id != str(exclude_transaction_id))
    return query.first() is not None


def _load_actors(db: Session, buyer_id: str, product_id: str):
    product = db.get(product_db, str(product_id))
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")

    buyer = db.get(buyer_db, str(buyer_id))
    if buyer is None:
        raise HTTPException(status_code=404, detail="Buyer not found")

    mandate = db.query(mandate_db).filter(mandate_db.buyer_id == str(buyer_id)).first()
    if mandate is None:
        raise HTTPException(status_code=404, detail="Mandate not found for this buyer")

    return product, buyer, mandate


def _mandate_permits(mandate, product) -> tuple[bool, bool]:
    """(merchant_allowed, category_allowed) for this product under this mandate.

    An EMPTY allow-list means the mandate places no restriction on that
    dimension. A non-empty one is strict, and strict means strict: a
    product carrying no category cannot satisfy a category allow-list.
    """
    allowed_merchants = {str(row.merchant_id) for row in mandate.allowed_merchants}
    allowed_categories = {row.category for row in mandate.allowed_categories}

    merchant_allowed = (
        not allowed_merchants or str(product.merchant_id) in allowed_merchants
    )
    category_allowed = not allowed_categories or product.category in allowed_categories
    return merchant_allowed, category_allowed


def _evaluate(
    db: Session,
    *,
    buyer,
    mandate,
    product,
    buyer_id: str,
    product_id: str,
    quantity: int,
    amount: float,
    human_approved: bool = False,
    exclude_transaction_id=None,
) -> PolicyResult:
    """Assemble the mandate facts and hand them to the pure engine.

    Everything here is read server-side. The caller supplies no limit, no
    permission and no amount.
    """
    merchant_allowed, category_allowed = _mandate_permits(mandate, product)
    return evaluate_transaction(
        amount=amount,
        balance=buyer.balance,
        autonomous_limit=mandate.autonomous_limit,
        absolute_transaction_limit=mandate.absolute_transaction_limit,
        monthly_cap=mandate.monthly_cap,
        monthly_spent_so_far=_monthly_spend(db, buyer_id),
        is_duplicate=_has_active_duplicate(
            db,
            buyer_id,
            product_id,
            quantity,
            amount,
            exclude_transaction_id=exclude_transaction_id,
        ),
        merchant_allowed=merchant_allowed,
        category_allowed=category_allowed,
        stock_sufficient=product.quantity >= quantity,
        human_approved=human_approved,
    )


# ---------------------------------------------------------------------------
# Quote
# ---------------------------------------------------------------------------


def quote_and_evaluate(
    db: Session,
    *,
    buyer_id: str,
    product_id: str,
    quantity: int,
    agent_selection: Optional[dict] = None,
) -> QuoteResponse:
    """Price, persist and evaluate one purchase. The single quoting path.

    Extracted from the `/quote` route so the AI buyer agent reaches the
    price through exactly this code and no other. Its three inputs are
    who, what and how many -- there is no parameter for a price, a limit
    or a verdict, so no caller (and in particular no model output) can
    contribute one. The unit price is read from the product row here,
    every time.

    `agent_selection` is the one exception, and it is not an input to
    anything: it is a bag of already-established facts about how this
    product came to be chosen, handed straight to `record_event` and read
    by nothing else. It exists because the selection happens before a
    quote exists, so it has nothing to be filed under until this point --
    recording it here is what puts the AI chapter at the front of the
    transaction's trail instead of orphaning it.
    """
    product, buyer, mandate = _load_actors(db, buyer_id, product_id)

    if quantity > product.quantity:
        _reject(
            409,
            RejectionReason.INSUFFICIENT_STOCK,
            f"Only {product.quantity} unit(s) of {product.product_name} in stock, "
            f"{quantity} requested.",
        )

    # Exact integer arithmetic: unit paise x quantity. No float multiply
    # ever touches a quoted amount.
    unit_paise = to_paise(product.cost)
    total_paise = unit_paise * quantity

    now = utcnow_naive()
    quote = quote_db(
        buyer_id=buyer_id,
        product_id=product_id,
        quantity=quantity,
        quoted_unit_price=from_paise(unit_paise),
        quoted_total=from_paise(total_paise),
        status=QuoteStatus.ACTIVE,
        created_at=now,
        expires_at=now + QUOTE_TTL,
    )
    db.add(quote)
    db.commit()
    db.refresh(quote)

    # Recorded after the commit, so the event describes a quote that
    # actually exists. See `record_event` on why the order matters.
    #
    # The selection goes first: it is what caused the quote.
    if agent_selection is not None:
        record_event(
            db,
            AuditEventType.AGENT_PRODUCT_SELECTED,
            buyer_id=buyer_id,
            quote_id=quote.id,
            product_id=product_id,
            quantity=quantity,
            **agent_selection,
        )

    record_event(
        db,
        AuditEventType.QUOTE_CREATED,
        buyer_id=buyer_id,
        quote_id=quote.id,
        product_id=product_id,
        product_name=product.product_name,
        quantity=quantity,
        quoted_unit_price=quote.quoted_unit_price,
        quoted_total=quote.quoted_total,
        expires_at=quote.expires_at,
    )

    result = _evaluate(
        db,
        buyer=buyer,
        mandate=mandate,
        product=product,
        buyer_id=buyer_id,
        product_id=product_id,
        quantity=quantity,
        amount=float(from_paise(total_paise)),
    )

    # The verdict at quote time. Advisory -- `/create-order` re-evaluates
    # from scratch and that pass is the one that governs money -- but it
    # is what the buyer was shown, so it belongs in the record.
    record_policy(
        db,
        result,
        context="quote",
        buyer_id=buyer_id,
        quote_id=quote.id,
        quoted_total=quote.quoted_total,
    )

    return QuoteResponse(
        quote_id=quote.id,
        buyer_id=quote.buyer_id,
        product=ProductSummary(
            product_id=product.product_id,
            product_name=product.product_name,
            brand=product.brand,
            category=product.category,
            merchant_id=product.merchant_id,
        ),
        quantity=quote.quantity,
        quoted_unit_price=quote.quoted_unit_price,
        quoted_total=quote.quoted_total,
        created_at=quote.created_at,
        expires_at=quote.expires_at,
        quote_status=quote.status,
        policy=result,
    )


@router.post("/quote", response_model=QuoteResponse)
def create_quote(order: OrderRequest, db: Session = Depends(get_db)) -> QuoteResponse:
    """Price an order from the CURRENT server-side catalogue price, persist
    that price as an immutable snapshot, and report the policy verdict.

    The caller supplies who, what and how many. It does not -- and cannot --
    supply the price or the mandate values; both are read server-side.

    The response carries a PolicyDecision and deliberately no
    TransactionStatus. Nothing here is a financial event.
    """
    return quote_and_evaluate(
        db,
        buyer_id=order.buyer_id,
        product_id=order.product_id,
        quantity=order.quantity,
    )


# ---------------------------------------------------------------------------
# Order creation
# ---------------------------------------------------------------------------


def _record(
    db: Session,
    *,
    quote: quote_db,
    amount: float,
    status: TransactionStatus,
    result: PolicyResult,
) -> transaction_db:
    txn = transaction_db(
        buyer_id=quote.buyer_id,
        product_id=quote.product_id,
        quote_id=quote.id,
        quantity=quote.quantity,
        amount=amount,
        status=status,
        reason=result.summary,
    )
    db.add(txn)
    db.commit()
    db.refresh(txn)
    return txn


def _invalidate(db: Session, quote: quote_db) -> None:
    """A quote whose world has changed is dead, not repriced.

    Silently substituting the new price is the failure mode this exists to
    prevent: the buyer would be charged an amount they never agreed to.
    """
    quote.status = QuoteStatus.EXPIRED
    db.commit()


def _order_response(
    txn: transaction_db,
    result: PolicyResult,
    razorpay_info: Optional[RazorpayOrderInfo] = None,
    approval: Optional[approval_db] = None,
) -> CreateOrderResponse:
    return CreateOrderResponse(
        transaction=TransactionResponse.model_validate(txn),
        policy=result,
        razorpay=razorpay_info,
        approval=(
            ApprovalResponse.model_validate(approval) if approval is not None else None
        ),
    )


# ---------------------------------------------------------------------------
# Human approval plumbing
# ---------------------------------------------------------------------------


def unresolved_approval(db: Session, transaction_id) -> Optional[approval_db]:
    """The single outstanding approval for this transaction, if any."""
    return (
        db.query(approval_db)
        .filter(
            approval_db.transaction_id == str(transaction_id),
            approval_db.status.in_(tuple(UNRESOLVED_APPROVAL_STATUSES)),
        )
        .first()
    )


def _open_approval(
    db: Session, txn: transaction_db, result: PolicyResult
) -> approval_db:
    """Ensure exactly ONE unresolved approval exists for this transaction.

    Re-asking is idempotent. Between this and duplicate detection, a client
    that retries an identical over-threshold purchase cannot accumulate a
    queue of identical requests for a human to wade through.
    """
    existing = unresolved_approval(db, txn.id)
    if existing is not None:
        return existing

    approval = approval_db(
        transaction_id=txn.id,
        status=ApprovalStatus.PENDING,
        reason=result.summary,
    )
    db.add(approval)
    db.commit()
    db.refresh(approval)

    record_event(
        db,
        AuditEventType.APPROVAL_CREATED,
        buyer_id=txn.buyer_id,
        quote_id=txn.quote_id,
        transaction_id=txn.id,
        approval_id=approval.id,
        approval_codes=list(result.approval_codes),
        amount=txn.amount,
        reason=result.summary,
    )
    return approval


# ---------------------------------------------------------------------------
# Provider contact -- the only place an order is created
# ---------------------------------------------------------------------------


def create_razorpay_order(
    db: Session,
    *,
    txn: transaction_db,
    quote: quote_db,
    total_paise: int,
    summary: str,
) -> RazorpayOrderInfo:
    """Create the Razorpay Test Mode order and move the transaction to
    ORDER_CREATED.

    Shared by the autonomous path and the post-approval path, so both reach
    the provider through the same door -- and so neither can reach it
    without having passed policy first.

    ORDER_CREATED is as far as this goes. An order is an intent to collect,
    never a collection, so nothing here touches balance, stock or PAID.
    """
    record_event(
        db,
        AuditEventType.ORDER_CREATION_ATTEMPTED,
        buyer_id=txn.buyer_id,
        quote_id=quote.id,
        transaction_id=txn.id,
        amount_in_paise=total_paise,
        status_before=txn.status,
    )

    try:
        client = razorpay.Client(auth=(key_id, key_secret))
        neworder = client.order.create(
            {
                # Straight from the frozen quote, in the integer unit
                # Razorpay expects. No caller input reaches this number.
                "amount": total_paise,
                "currency": "INR",
                "notes": {
                    "buyer_id": str(quote.buyer_id),
                    "transaction_id": str(txn.id),
                    "quote_id": str(quote.id),
                },
            }
        )
    except Exception as exc:  # provider unreachable / rejected the order
        txn.status = TransactionStatus.FAILED
        txn.reason = f"{summary} | Razorpay order creation failed: {exc}"
        db.commit()

        # No order id, no ORDER_CREATED, no PAID, and balance and stock
        # were never in scope here to begin with. The record says so.
        record_event(
            db,
            AuditEventType.ORDER_CREATION_FAILED,
            buyer_id=txn.buyer_id,
            quote_id=quote.id,
            transaction_id=txn.id,
            error=type(exc).__name__,
            error_message=str(exc)[:300],
            status_after=TransactionStatus.FAILED,
            razorpay_order_id=None,
            balance_or_stock_mutated=False,
        )
        record_event(
            db,
            AuditEventType.TRANSACTION_FAILED,
            buyer_id=txn.buyer_id,
            quote_id=quote.id,
            transaction_id=txn.id,
            status_from=TransactionStatus.AUTHORIZED,
            status_to=TransactionStatus.FAILED,
            reason="provider order creation failed",
        )

        # The refusal carries where the transaction actually ended up,
        # read after FAILED was committed -- the same contract payment
        # verification already keeps. A caller holding AWAITING_APPROVAL
        # or AUTHORIZED must not be left rendering it, and there is no
        # order id to report here because none was ever returned.
        _reject(
            502,
            RejectionReason.PROVIDER_ERROR,
            "Payment provider order creation failed. Nothing was charged.",
            transaction_id=txn.id,
            transaction_status=txn.status,
        )

    status_before = txn.status
    txn.razorpay_order_id = neworder["id"]
    txn.status = TransactionStatus.ORDER_CREATED
    db.commit()
    db.refresh(txn)

    record_event(
        db,
        AuditEventType.ORDER_CREATED,
        buyer_id=txn.buyer_id,
        quote_id=quote.id,
        transaction_id=txn.id,
        razorpay_order_id=txn.razorpay_order_id,
        amount_in_paise=total_paise,
        status_from=status_before,
        status_to=TransactionStatus.ORDER_CREATED,
        paid=False,
    )

    return RazorpayOrderInfo(
        razorpay_order_id=txn.razorpay_order_id, amount_in_paise=total_paise
    )


# ---------------------------------------------------------------------------
# Quote re-validation, shared by order creation and post-approval execution
# ---------------------------------------------------------------------------


# The revalidation reason -> the event that names it. Anything not listed
# is recorded as the generic QUOTE_REVALIDATION_FAILED; drift and expiry
# get their own names because they are the two an operator actually looks
# for.
_REVALIDATION_EVENTS = {
    RejectionReason.PRICE_DRIFT: AuditEventType.PRICE_DRIFT_DETECTED,
    RejectionReason.QUOTE_EXPIRED: AuditEventType.QUOTE_EXPIRED,
}


def _record_revalidation_failure(
    db: Session,
    quote: quote_db,
    detail: RejectionDetail,
    transaction_id=None,
) -> None:
    """Note that a quote did not survive re-checking, and that the provider
    was therefore never contacted."""
    record_event(
        db,
        _REVALIDATION_EVENTS.get(
            detail.reason, AuditEventType.QUOTE_REVALIDATION_FAILED
        ),
        buyer_id=quote.buyer_id,
        quote_id=quote.id,
        transaction_id=transaction_id,
        reason=detail.reason,
        message=detail.message,
        quoted_unit_price=detail.quoted_unit_price,
        current_unit_price=detail.current_unit_price,
        quoted_total=quote.quoted_total,
        quote_status=quote.status,
        provider_called=False,
    )


def quote_revalidation_failure(
    db: Session, quote: quote_db, product, transaction_id=None
) -> Optional[tuple[int, RejectionDetail]]:
    """Re-check everything a quote asserts about a moment already passed.

    Returns `(status_code, detail)` for the first thing that no longer
    holds, or None if the quote is still good. Its only side effect is
    invalidating a quote the world has moved past -- and recording that it
    did, which is the one place the audit trail can prove the provider was
    never reached.

    Returning rather than raising is deliberate: order creation turns this
    into a 4xx refusal, while post-approval execution turns it into a safe
    terminal transaction state. Same checks, two honest outcomes.

    `transaction_id` is present only on the post-approval path, where the
    transaction already exists.
    """
    failure = _quote_revalidation_failure(db, quote, product)
    if failure is not None:
        _record_revalidation_failure(db, quote, failure[1], transaction_id)
    return failure


def _quote_revalidation_failure(
    db: Session, quote: quote_db, product
) -> Optional[tuple[int, RejectionDetail]]:
    """The checks themselves, with no audit involvement whatsoever.

    Split out so the decision and the recording of it are visibly separate
    functions: this one reads state and returns a verdict, and no branch
    of it can see whether the event above was written.
    """
    if quote.status is not QuoteStatus.ACTIVE:
        return 409, RejectionDetail(
            reason=RejectionReason.QUOTE_NOT_ACTIVE,
            message=f"Quote is {quote.status.value} and can no longer be used.",
            requote_required=True,
        )

    if quote.is_expired():
        _invalidate(db, quote)
        return 409, RejectionDetail(
            reason=RejectionReason.QUOTE_EXPIRED,
            message="Quote has expired. Request a new quote.",
            requote_required=True,
        )

    if product is None:
        _invalidate(db, quote)
        return 404, RejectionDetail(
            reason=RejectionReason.PRODUCT_NOT_FOUND,
            message="The quoted product no longer exists.",
            requote_required=True,
        )

    if product.quantity < quote.quantity:
        return 409, RejectionDetail(
            reason=RejectionReason.INSUFFICIENT_STOCK,
            message=(
                f"Only {product.quantity} unit(s) left, quote is for "
                f"{quote.quantity}."
            ),
            requote_required=True,
        )

    # Price drift. For v1 ANY change invalidates the quote -- both a rise
    # and a fall, because either means the buyer agreed to a price that is
    # no longer the price. Compared in integer paise, so 100.0 vs 100.00
    # is not a drift and 100.00 vs 100.01 is.
    current_unit_paise = to_paise(product.cost)
    quoted_unit_paise = to_paise(quote.quoted_unit_price)
    if current_unit_paise != quoted_unit_paise:
        _invalidate(db, quote)
        return 409, RejectionDetail(
            reason=RejectionReason.PRICE_DRIFT,
            message=(
                f"Price changed from {from_paise(quoted_unit_paise)} to "
                f"{from_paise(current_unit_paise)} since the quote was issued. "
                "No order was created."
            ),
            requote_required=True,
            quoted_unit_price=from_paise(quoted_unit_paise),
            current_unit_price=from_paise(current_unit_paise),
        )

    return None


@router.post("/create-order", response_model=CreateOrderResponse)
def create_order(
    request: CreateOrderRequest, db: Session = Depends(get_db)
) -> CreateOrderResponse:
    """Turn a live quote into a payable Razorpay order.

    Everything is re-read immediately beforehand -- quote, current price,
    buyer, mandate, permissions and transaction history -- because a quote
    is a claim about a moment that has already passed.

    Payment-state truth, in order:

      BLOCK            -> BLOCKED,           provider never contacted
      REQUIRE_APPROVAL -> AWAITING_APPROVAL, provider never contacted,
                          exactly one unresolved approval opened
      ALLOW            -> AUTHORIZED, then a Razorpay order -> ORDER_CREATED

    ORDER_CREATED is the terminal state of this endpoint. A Razorpay order
    is an intent to collect, not a collection, so nothing here may set PAID
    -- which is also why buyer balance and product stock are untouched.

    The read-decide-commit part of this runs inside the shared money
    critical section. Duplicate detection is a read-then-write check, and
    without serialization two simultaneous callers both read "no
    duplicate" before either has written the transaction that would have
    told the other one. The provider call is deliberately made AFTER the
    section is released: the AUTHORIZED row that makes this attempt
    visible to a concurrent one is already committed by then, so nothing
    is gained by holding a process-wide lock across a network round trip.
    """
    with money_critical_section():
        txn, result, quote, total_paise, response = _authorize_order(db, request)
    if response is not None:
        return response

    razorpay_info = create_razorpay_order(
        db,
        txn=txn,
        quote=quote,
        total_paise=total_paise,
        summary=result.summary,
    )

    return _order_response(txn, result, razorpay_info)


def _authorize_order(db: Session, request: CreateOrderRequest):
    """Re-validate, evaluate and commit the live transaction state.

    MUST be called with the money critical section held. Returns
    `(txn, result, quote, total_paise, response)`; a non-None `response`
    is a terminal outcome that never reaches the provider, and the caller
    returns it unchanged.
    """
    quote = db.get(quote_db, str(request.quote_id))
    if quote is None:
        _reject(404, RejectionReason.QUOTE_NOT_FOUND, "Quote not found.")

    product = db.get(product_db, str(quote.product_id))

    failure = quote_revalidation_failure(db, quote, product)
    if failure is not None:
        status_code, detail = failure
        raise HTTPException(
            status_code=status_code, detail=detail.model_dump(mode="json")
        )

    buyer = db.get(buyer_db, str(quote.buyer_id))
    if buyer is None:
        raise HTTPException(status_code=404, detail="Buyer not found")

    mandate = (
        db.query(mandate_db).filter(mandate_db.buyer_id == str(quote.buyer_id)).first()
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
        buyer_id=str(quote.buyer_id),
        product_id=str(quote.product_id),
        quantity=quote.quantity,
        amount=amount,
    )

    if result.decision is PolicyDecision.BLOCK:
        txn = _record(
            db,
            quote=quote,
            amount=amount,
            status=TransactionStatus.BLOCKED,
            result=result,
        )
        record_policy(
            db,
            result,
            context="create_order",
            buyer_id=txn.buyer_id,
            quote_id=quote.id,
            transaction_id=txn.id,
            amount=amount,
        )
        record_event(
            db,
            AuditEventType.TRANSACTION_BLOCKED,
            buyer_id=txn.buyer_id,
            quote_id=quote.id,
            transaction_id=txn.id,
            status_to=TransactionStatus.BLOCKED,
            violation_codes=list(result.violation_codes),
            provider_called=False,
        )
        return None, result, quote, total_paise, _order_response(txn, result)

    if result.decision is PolicyDecision.REQUIRE_APPROVAL:
        txn = _record(
            db,
            quote=quote,
            amount=amount,
            status=TransactionStatus.AWAITING_APPROVAL,
            result=result,
        )
        record_policy(
            db,
            result,
            context="create_order",
            buyer_id=txn.buyer_id,
            quote_id=quote.id,
            transaction_id=txn.id,
            amount=amount,
        )
        # A human is now the gate. The provider is not contacted, and the
        # request is recorded exactly once.
        approval = _open_approval(db, txn, result)
        return None, result, quote, total_paise, _order_response(
            txn, result, approval=approval
        )

    # ALLOW. Commit AUTHORIZED *before* the critical section is released,
    # so that a concurrent identical attempt -- which cannot enter the
    # section until this commit has happened -- sees it as a duplicate.
    txn = _record(
        db,
        quote=quote,
        amount=amount,
        status=TransactionStatus.AUTHORIZED,
        result=result,
    )
    record_policy(
        db,
        result,
        context="create_order",
        buyer_id=txn.buyer_id,
        quote_id=quote.id,
        transaction_id=txn.id,
        amount=amount,
        human_approval_required=False,
    )

    return txn, result, quote, total_paise, None


# ---------------------------------------------------------------------------
# Demo identity and approval queue
# ---------------------------------------------------------------------------


@router.get("/buyers", response_model=list[DemoBuyerResponse])
def list_demo_buyers(db: Session = Depends(get_db)) -> list[DemoBuyerResponse]:
    """The seeded demo identities, each with the mandate governing it.

    There is no authentication in v1 and none is planned for it: picking a
    demo buyer IS the identity step, and `buyer_id` is the only thing
    carried forward into a quote. Production auth is explicitly out of
    scope for the Buildathon build.
    """
    responses: list[DemoBuyerResponse] = []
    for buyer in db.query(buyer_db).all():
        mandate = (
            db.query(mandate_db).filter(mandate_db.buyer_id == str(buyer.id)).first()
        )
        summary = None
        if mandate is not None:
            summary = MandateSummary(
                mandate_id=mandate.id,
                autonomous_limit=Decimal(str(mandate.autonomous_limit)),
                absolute_transaction_limit=Decimal(
                    str(mandate.absolute_transaction_limit)
                ),
                monthly_cap=Decimal(str(mandate.monthly_cap)),
                monthly_spent=Decimal(str(_monthly_spend(db, str(buyer.id)))),
                allowed_merchants=[row.merchant_id for row in mandate.allowed_merchants],
                allowed_categories=[row.category for row in mandate.allowed_categories],
            )
        responses.append(
            DemoBuyerResponse(
                buyer_id=buyer.id,
                name=buyer.name,
                balance=Decimal(str(buyer.balance)),
                mandate=summary,
            )
        )
    return responses


@router.get("/approvals", response_model=PendingApprovalsResponse)
def list_pending_approvals(
    buyer_id: Optional[str] = None, db: Session = Depends(get_db)
) -> PendingApprovalsResponse:
    """Everything currently waiting on a human."""
    query = db.query(approval_db).filter(
        approval_db.status.in_(tuple(UNRESOLVED_APPROVAL_STATUSES))
    )
    if buyer_id is not None:
        query = query.join(
            transaction_db, transaction_db.id == approval_db.transaction_id
        ).filter(transaction_db.buyer_id == str(buyer_id))

    approvals = [
        ApprovalResponse.model_validate(row)
        for row in query.order_by(approval_db.created_at).all()
    ]
    return PendingApprovalsResponse(count=len(approvals), approvals=approvals)


# ---------------------------------------------------------------------------
# Audit trail -- read only, and read by nothing else
# ---------------------------------------------------------------------------


@router.get(
    "/transactions/{transaction_id}/audit",
    response_model=TransactionAuditResponse,
    tags=["audit"],
)
def transaction_audit(
    transaction_id: str, db: Session = Depends(get_db)
) -> TransactionAuditResponse:
    """The recorded story of one transaction, oldest event first.

    A read of history. It evaluates no policy, contacts no provider and
    writes nothing -- calling it a thousand times leaves every balance,
    stock level and payment status exactly where it found them.

    Events recorded against the transaction's quote are included, because
    the quote and the AI request behind it happened before the transaction
    row existed and are the first chapters of the same story.
    """
    txn = db.get(transaction_db, str(transaction_id))
    if txn is None:
        raise HTTPException(status_code=404, detail="Transaction not found")

    events = events_for_transaction(db, txn)
    return TransactionAuditResponse(
        transaction_id=txn.id,
        status=txn.status,
        quote_id=txn.quote_id,
        event_count=len(events),
        events=[AuditEventResponse.model_validate(event) for event in events],
    )


# ---------------------------------------------------------------------------
# Payment verification -- the only place PAID is written
# ---------------------------------------------------------------------------


def _verification_response(
    *,
    txn: transaction_db,
    quote: Optional[quote_db],
    buyer,
    product,
    already_verified: bool,
) -> PaymentVerificationResponse:
    return PaymentVerificationResponse(
        verified=True,
        already_verified=already_verified,
        transaction=TransactionResponse.model_validate(txn),
        quote_status=quote.status,
        buyer_balance=Decimal(str(buyer.balance)),
        product_stock_remaining=product.quantity,
    )


def _fail(db: Session, txn: transaction_db, note: str) -> None:
    """Record a failed verification attempt.

    Only the transaction is touched. No balance, no stock, no quote --
    an unverified payment has moved nothing and must consume nothing.
    """
    status_from = txn.status
    txn.status = TransactionStatus.FAILED
    txn.reason = f"{txn.reason} | {note}"
    db.commit()

    record_event(
        db,
        AuditEventType.TRANSACTION_FAILED,
        buyer_id=txn.buyer_id,
        quote_id=txn.quote_id,
        transaction_id=txn.id,
        status_from=status_from,
        status_to=TransactionStatus.FAILED,
        reason=note,
        balance_or_stock_mutated=False,
    )


def _verification_refused(
    db: Session,
    txn: Optional[transaction_db],
    status_code: int,
    reason: RejectionReason,
    message: str,
    **extra,
) -> None:
    """Note why a payment was not settled, then refuse it.

    Every exit from `verify_payment` that is not a settlement comes
    through here, so "nothing was settled" is a claim the trail can
    substantiate rather than one the prose merely asserts.

    The transaction's status is read here, after any `_fail` has already
    written it, and reported both to the trail and to the caller. A caller
    that has just been refused should not have to guess whether the
    transaction it was holding is still payable.
    """
    status = txn.status if txn is not None else None

    record_event(
        db,
        AuditEventType.PAYMENT_VERIFICATION_FAILED,
        buyer_id=txn.buyer_id if txn is not None else None,
        quote_id=txn.quote_id if txn is not None else None,
        transaction_id=txn.id if txn is not None else None,
        reason=reason,
        message=message,
        transaction_status=status,
        paid=False,
        balance_or_stock_mutated=False,
        **extra,
    )
    _reject(status_code, reason, message, transaction_status=status)


@router.post("/payment/verify", response_model=PaymentVerificationResponse)
def verify_payment(
    request: PaymentVerificationRequest, db: Session = Depends(get_db)
) -> PaymentVerificationResponse:
    """Verify a Razorpay payment server-side and, only then, settle it.

    This is the single writer of PAID in the system, and the single place
    the buyer's balance and the product's stock are decremented -- both in
    the same database transaction as the status change, so the three can
    never disagree.

    The caller's claims about amount or payment status are ignored
    entirely; the only thing taken from it is the signature, which is
    checked against our own secret and then discarded rather than stored.

    The whole of it runs inside the shared money critical section. Every
    settlement check -- stock, balance, the monthly cap -- is a read of
    state that the settlement itself then changes, so two simultaneous
    verifications would otherwise read the same monthly spend and both
    conclude there was room. Nothing in here contacts the network: the
    Razorpay signature check is a local HMAC, so holding the section
    across it costs nothing.
    """
    with money_critical_section():
        return _settle_payment(db, request)


def _settle_payment(
    db: Session, request: PaymentVerificationRequest
) -> PaymentVerificationResponse:
    """The verification and settlement itself.

    MUST be called with the money critical section held: it reads the
    monthly spend, stock and balance, and then writes all three of PAID,
    the decremented balance and the decremented stock on the strength of
    those reads.
    """
    txn = db.get(transaction_db, str(request.transaction_id))
    if txn is None:
        _verification_refused(
            db,
            None,
            404,
            RejectionReason.TRANSACTION_NOT_FOUND,
            "Transaction not found.",
        )

    quote = db.get(quote_db, str(txn.quote_id)) if txn.quote_id else None

    record_event(
        db,
        AuditEventType.PAYMENT_VERIFICATION_ATTEMPTED,
        buyer_id=txn.buyer_id,
        quote_id=txn.quote_id,
        transaction_id=txn.id,
        transaction_status=txn.status,
        # The presented ids, so a mismatch is legible afterwards. The
        # signature is deliberately not passed in, and could not be stored
        # if it were -- see `backend.audit.SENSITIVE_KEY_FRAGMENTS`.
        presented_order_id=request.razorpay_order_id,
        presented_payment_id=request.razorpay_payment_id,
    )

    # --- Idempotency ------------------------------------------------------
    # Checked before anything else, because the honest answer to a replayed
    # callback is "already done", not "wrong state".
    if txn.status is TransactionStatus.PAID:
        same_payment = (
            txn.razorpay_payment_id == request.razorpay_payment_id
            and txn.razorpay_order_id == request.razorpay_order_id
        )
        if not same_payment:
            # A different payment presented against a settled transaction.
            # Refuse loudly; do not mutate anything a second time.
            _verification_refused(
                db,
                txn,
                409,
                RejectionReason.PAYMENT_ALREADY_SETTLED,
                "This transaction is already settled by a different payment. "
                "No further charge was applied.",
                settled_payment_id=txn.razorpay_payment_id,
            )

        # A replay of the same payment. Recorded as a verification carrying
        # `replay`, so the trail shows the callback arrived twice and that
        # the second one moved nothing.
        record_event(
            db,
            AuditEventType.PAYMENT_VERIFIED,
            buyer_id=txn.buyer_id,
            quote_id=txn.quote_id,
            transaction_id=txn.id,
            razorpay_order_id=txn.razorpay_order_id,
            razorpay_payment_id=txn.razorpay_payment_id,
            replay=True,
            balance_or_stock_mutated=False,
        )
        return _verification_response(
            txn=txn,
            quote=quote,
            buyer=db.get(buyer_db, str(txn.buyer_id)),
            product=db.get(product_db, str(txn.product_id)),
            already_verified=True,
        )

    if txn.status is not TransactionStatus.ORDER_CREATED:
        _verification_refused(
            db,
            txn,
            409,
            RejectionReason.TRANSACTION_NOT_AWAITING_PAYMENT,
            f"Transaction is {txn.status.value}; only an ORDER_CREATED "
            "transaction can be settled.",
        )

    if not txn.razorpay_order_id or txn.razorpay_order_id != request.razorpay_order_id:
        # The callback does not belong to this transaction. Refuse without
        # marking it failed -- our transaction is still perfectly payable.
        _verification_refused(
            db,
            txn,
            409,
            RejectionReason.ORDER_ID_MISMATCH,
            "Razorpay order id does not match the order stored for this transaction.",
            stored_order_id=txn.razorpay_order_id,
        )

    if quote is None:
        _verification_refused(
            db, txn, 409, RejectionReason.QUOTE_NOT_FOUND, "Transaction has no quote."
        )
    if quote.status is not QuoteStatus.ACTIVE:
        # Deliberately no clock check here: expiry guards order creation.
        # Once the buyer has actually paid, refusing on a stopwatch would
        # strand real money.
        _verification_refused(
            db,
            txn,
            409,
            RejectionReason.QUOTE_NOT_ACTIVE,
            f"Quote is {quote.status.value} and cannot be settled again.",
        )

    product = db.get(product_db, str(txn.product_id))
    buyer = db.get(buyer_db, str(txn.buyer_id))
    if product is None or buyer is None:
        _verification_refused(
            db,
            txn,
            404,
            RejectionReason.PRODUCT_NOT_FOUND,
            "Buyer or product missing.",
        )

    total_paise = to_paise(quote.quoted_total)

    if product.quantity < quote.quantity:
        _fail(db, txn, "Stock no longer sufficient at settlement.")
        _verification_refused(
            db,
            txn,
            409,
            RejectionReason.INSUFFICIENT_STOCK,
            "Stock is no longer sufficient; the payment was not settled.",
        )
    if to_paise(buyer.balance) < total_paise:
        _fail(db, txn, "Balance no longer sufficient at settlement.")
        _verification_refused(
            db,
            txn,
            409,
            RejectionReason.INSUFFICIENT_BALANCE,
            "Buyer balance is no longer sufficient; the payment was not settled.",
        )

    # --- The monthly cap, re-checked at the moment money would move -------
    #
    # Order creation checks the cap against the spend as it stood THEN,
    # and only PAID spend counts. Two orders of 4,000 against a 5,000 cap
    # are therefore both legitimately creatable, because when each was
    # created neither had settled. Without this second check the buyer
    # ends the month having actually paid 8,000 against a 5,000 mandate.
    #
    # `settled_at` is captured once and used for both the check and the
    # `paid_at` written below, so the month this is checked against is by
    # construction the month it is recorded in.
    settled_at = utcnow_naive()
    mandate = (
        db.query(mandate_db).filter(mandate_db.buyer_id == str(txn.buyer_id)).first()
    )
    if mandate is None:
        # The authority that permitted this purchase is gone, so there is
        # nothing left to settle it against. Refused without marking the
        # transaction FAILED -- the fault is in the mandate, not here.
        _verification_refused(
            db,
            txn,
            409,
            RejectionReason.MANDATE_NOT_FOUND,
            "No mandate governs this buyer, so the payment was not settled.",
        )

    spent_paise = to_paise(_monthly_spend(db, str(txn.buyer_id), at=settled_at))
    cap_paise = to_paise(mandate.monthly_cap)
    if spent_paise + total_paise > cap_paise:
        _fail(db, txn, "Monthly cap would be exceeded at settlement.")
        _verification_refused(
            db,
            txn,
            409,
            RejectionReason.MONTHLY_CAP_EXCEEDED,
            f"Settling {from_paise(total_paise)} would take verified spend for "
            f"{settled_at.strftime('%B %Y')} to "
            f"{from_paise(spent_paise + total_paise)}, over the monthly cap of "
            f"{from_paise(cap_paise)}. The payment was not settled.",
            monthly_spent=float(from_paise(spent_paise)),
            monthly_cap=float(from_paise(cap_paise)),
        )

    # --- Server-side signature verification -------------------------------
    try:
        client = razorpay.Client(auth=(key_id, key_secret))
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail="Payment provider unavailable"
        ) from exc

    try:
        client.utility.verify_payment_signature(
            {
                "razorpay_order_id": request.razorpay_order_id,
                "razorpay_payment_id": request.razorpay_payment_id,
                "razorpay_signature": request.razorpay_signature,
            }
        )
    except Exception:
        # The signature did not come from Razorpay. Nothing is settled and
        # nothing is decremented. The signature itself is never stored --
        # not on the transaction row, and not in the audit event.
        _fail(db, txn, "Razorpay signature verification failed.")
        _verification_refused(
            db,
            txn,
            400,
            RejectionReason.SIGNATURE_VERIFICATION_FAILED,
            "Payment signature verification failed. Nothing was settled.",
        )

    # --- Settlement: one database transaction -----------------------------
    try:
        txn.status = TransactionStatus.PAID
        txn.razorpay_payment_id = request.razorpay_payment_id
        # The authoritative settlement time, written exactly once and in
        # the same commit as PAID. This -- not the creation timestamp --
        # is what the monthly cap is counted from.
        txn.paid_at = settled_at
        buyer.balance = float(from_paise(to_paise(buyer.balance) - total_paise))
        product.quantity = product.quantity - quote.quantity
        quote.status = QuoteStatus.CONSUMED
        db.commit()
    except Exception:
        db.rollback()
        raise

    db.refresh(txn)

    # Two events, because they are two different claims: the signature was
    # ours, and money then actually moved.
    record_event(
        db,
        AuditEventType.PAYMENT_VERIFIED,
        buyer_id=txn.buyer_id,
        quote_id=quote.id,
        transaction_id=txn.id,
        razorpay_order_id=txn.razorpay_order_id,
        razorpay_payment_id=txn.razorpay_payment_id,
        replay=False,
        verified_server_side=True,
    )
    record_event(
        db,
        AuditEventType.TRANSACTION_PAID,
        buyer_id=txn.buyer_id,
        quote_id=quote.id,
        transaction_id=txn.id,
        status_from=TransactionStatus.ORDER_CREATED,
        status_to=TransactionStatus.PAID,
        amount=txn.amount,
        amount_in_paise=total_paise,
        paid_at=txn.paid_at,
        buyer_balance_after=buyer.balance,
        product_stock_after=product.quantity,
        quote_status=QuoteStatus.CONSUMED,
    )

    return _verification_response(
        txn=txn, quote=quote, buyer=buyer, product=product, already_verified=False
    )
