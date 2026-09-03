"""Recording what happened, without ever deciding what happens.

This module is the single writer of `audit_events`. Everything about it
is shaped by one rule:

    The audit trail OBSERVES financial decisions. It never authorizes one.

Two concrete consequences:

  * `record_event` cannot raise. A failure to write history is a
    degradation of the record, never a change to the outcome -- deleting
    every call to it would leave every ALLOW/BLOCK/PAID decision in this
    codebase byte-for-byte identical.
  * Nothing in `backend/` reads these rows back except the read endpoint.
    There is no query from the policy engine, the quote path, the
    approval path or payment verification into this table.

It also decides what is NOT written. Signatures, API secrets, prompts and
model replies are dropped before the row is built, because an audit log
that leaks the thing it audits is worse than none.
"""

from typing import Any, Optional

from sqlalchemy.orm import Session

from backend.database import utcnow_naive
from backend.databases.audit_db import audit_event_db
from backend.enums.AuditEventType import AuditEventType

# Key fragments whose values never enter the log. Matched case-insensitively
# against the whole key, at every depth of the details payload.
#
# `prompt` and the model-reply fragments are here for the same reason as
# the credentials: an audit trail is a demo artefact and a debugging aid,
# and neither job needs the buyer's full request text or a model's
# reasoning to be sitting in a database.
SENSITIVE_KEY_FRAGMENTS = (
    "signature",
    "secret",
    "password",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "private_key",
    "key_id",
    "prompt",
    "raw_response",
    "chain_of_thought",
    "reasoning",
)


def _is_sensitive(key: str) -> bool:
    lowered = str(key).lower()
    return any(fragment in lowered for fragment in SENSITIVE_KEY_FRAGMENTS)


def scrub(details: dict[str, Any]) -> dict[str, Any]:
    """Drop sensitive entries, recursively.

    Dropped rather than masked: a masked key still advertises that the
    value existed here, and the honest record is that this log does not
    hold it at all. The names that were dropped are kept under
    `_redacted`, so the omission itself is visible.
    """
    clean: dict[str, Any] = {}
    redacted: list[str] = []

    for key, value in details.items():
        if _is_sensitive(key):
            redacted.append(str(key))
            continue
        if isinstance(value, dict):
            nested = scrub(value)
            clean[key] = nested
        else:
            clean[key] = value

    if redacted:
        clean["_redacted"] = sorted(redacted)
    return clean


def record_event(
    db: Session,
    event_type: AuditEventType,
    *,
    buyer_id=None,
    quote_id=None,
    transaction_id=None,
    approval_id=None,
    **details: Any,
) -> Optional[audit_event_db]:
    """Append one event. Returns the row, or None if it could not be written.

    CALL ORDER MATTERS. Call this *after* the financial state it describes
    has been committed. This function commits, and on failure it rolls
    back -- so a caller that leaves uncommitted financial work in the
    session and then records an event would be trusting the log with money.
    Every call site in this project commits first.

    Never raises. The `except` is not defensive padding: it is the
    mechanism by which a broken audit table cannot block a payment.
    """
    try:
        entry = audit_event_db(
            event_type=event_type,
            timestamp=utcnow_naive(),
            buyer_id=str(buyer_id) if buyer_id is not None else None,
            quote_id=str(quote_id) if quote_id is not None else None,
            transaction_id=str(transaction_id) if transaction_id is not None else None,
            approval_id=str(approval_id) if approval_id is not None else None,
            details=scrub(details),
        )
        db.add(entry)
        db.commit()
        return entry
    except Exception:  # pragma: no cover - the point is that it changes nothing
        try:
            db.rollback()
        except Exception:
            pass
        return None


def record_policy(
    db: Session,
    result,
    *,
    context: str,
    buyer_id=None,
    quote_id=None,
    transaction_id=None,
    **extra: Any,
) -> Optional[audit_event_db]:
    """Record a deterministic verdict as the event named after it.

    `context` says where the evaluation happened -- "quote", "create_order"
    or "post_approval" -- because the same purchase is legitimately
    evaluated more than once and an audit reader needs to tell those
    passes apart.
    """
    from backend.enums.PolicyDecision import PolicyDecision

    event_type = {
        PolicyDecision.ALLOW: AuditEventType.POLICY_ALLOWED,
        PolicyDecision.REQUIRE_APPROVAL: AuditEventType.POLICY_APPROVAL_REQUIRED,
        PolicyDecision.BLOCK: AuditEventType.POLICY_BLOCKED,
    }[result.decision]

    return record_event(
        db,
        event_type,
        buyer_id=buyer_id,
        quote_id=quote_id,
        transaction_id=transaction_id,
        context=context,
        decision=result.decision,
        violation_codes=list(result.violation_codes),
        approval_codes=list(result.approval_codes),
        evaluated_rules=list(result.evaluated_rules),
        summary=result.summary,
        **extra,
    )


def events_for_transaction(db: Session, txn) -> list[audit_event_db]:
    """Every event belonging to one transaction, oldest first.

    Correlation uses the lifecycle relations the system already has, and
    nothing else -- there is no correlation id, no trace id and no join
    table. Two rules, and the second is the one that matters:

      * anything explicitly tagged with this transaction id, and
      * anything tagged with this transaction's quote but with NO
        transaction id of its own.

    That second clause is what pulls in the chapters that happened before
    the transaction row existed -- the AI selection and the quote itself.
    Restricting it to events carrying no transaction id is deliberate: one
    quote can legitimately produce more than one transaction (a retry, or
    a duplicate attempt that gets BLOCKED), and a sibling transaction's
    events must never surface in this one's story. An event that names a
    transaction belongs to that transaction alone.

    Ordered by `(timestamp, sequence)`. The sequence tiebreak is what
    makes the order total rather than merely usually-right.
    """
    from sqlalchemy import and_, or_

    conditions = [audit_event_db.transaction_id == str(txn.id)]
    if txn.quote_id is not None:
        conditions.append(
            and_(
                audit_event_db.quote_id == str(txn.quote_id),
                audit_event_db.transaction_id.is_(None),
            )
        )

    return (
        db.query(audit_event_db)
        .filter(or_(*conditions))
        .order_by(audit_event_db.timestamp, audit_event_db.sequence)
        .all()
    )
