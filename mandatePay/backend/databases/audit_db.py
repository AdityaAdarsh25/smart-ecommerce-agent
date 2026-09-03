"""The append-only audit log.

One row per thing that happened. A plain SQL table is the whole design --
there is no event bus, no projection, no replay, and nothing downstream
reads these rows to make a decision. That is the point: the log observes
the system, and a system whose behaviour depended on its own log would no
longer be auditable by it.

Two structural properties:

  * INSERT-ONLY. `_forbid_audit_mutation` below turns any attempt to
    update or delete a recorded event into an error, so "append-only in
    normal application use" is enforced rather than promised.
  * ORDERED. `sequence` is a plain autoincrementing integer, so the
    chronological order of two events written in the same clock tick is
    still unambiguous -- `timestamp` alone is not, on a coarse clock.
"""

import json
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from uuid import UUID

from sqlalchemy import Column, DateTime, ForeignKey, Integer, Text, event
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.orm import Session
from sqlalchemy.types import TypeDecorator

from backend.database import Base, utcnow_naive
from backend.databases.merchant_db import GUID

# Imported for their side effect: these tables must be registered on the
# shared metadata before the foreign keys below can resolve.
from backend.databases.approval_db import approval_db  # noqa: F401
from backend.databases.quote_db import quote_db  # noqa: F401
from backend.databases.transaction_db import transaction_db  # noqa: F401
from backend.enums.AuditEventType import AuditEventType


class AuditImmutableError(RuntimeError):
    """An audit event was updated or deleted. History is not editable."""


def jsonable(value):
    """Coerce a detail value into something `json.dumps` accepts.

    Deliberately total: an unknown type becomes its `repr` rather than an
    exception, because a detail we cannot serialize must never be able to
    fail the operation being audited.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, Decimal):
        # str() rather than float(): these are money, and the whole point
        # of Decimal here is not to hand them back to binary floating point.
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or timezone.utc).isoformat()
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(item) for item in value]
    return repr(value)


class JSONText(TypeDecorator):
    """A JSON object stored as text.

    SQLite has a JSON1 extension but no JSON column type worth depending
    on here, and the details payload is only ever read back whole.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return json.dumps(jsonable(value), sort_keys=True)

    def process_result_value(self, value, dialect):
        if value is None:
            return {}
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}


class audit_event_db(Base):
    """One recorded fact about one moment.

    Every foreign key is nullable because events legitimately arrive
    before the entity they will eventually belong to exists: a quote is
    created before the transaction that spends it, and an AI request that
    ends in a clarification never produces either.
    """

    __tablename__ = "audit_events"

    # An integer primary key rather than the project's usual GUID, on
    # purpose: it is the tiebreaker that makes chronological ordering
    # total. Nothing references an audit event, so it needs no opaque id.
    sequence = Column(Integer, primary_key=True, autoincrement=True)

    event_type = Column(SqlEnum(AuditEventType), nullable=False, index=True)
    timestamp = Column(DateTime, default=utcnow_naive, nullable=False, index=True)

    buyer_id = Column(GUID(), ForeignKey("buyers.id"), nullable=True, index=True)
    quote_id = Column(GUID(), ForeignKey("quotes.id"), nullable=True, index=True)
    transaction_id = Column(
        GUID(), ForeignKey("transactions.id"), nullable=True, index=True
    )
    approval_id = Column(GUID(), ForeignKey("approvals.id"), nullable=True)

    # Concise structured facts: decisions, reason codes, amounts, ids.
    # Never a secret, never a signature, never a prompt. See
    # `backend.audit.SENSITIVE_KEY_FRAGMENTS`.
    details = Column(JSONText(), nullable=False, default=dict)


@event.listens_for(Session, "before_flush")
def _forbid_audit_mutation(session, flush_context, instances):
    """Append-only, enforced at the ORM boundary.

    Registered on the Session class so it covers every session in the
    process, including the ones the test suite and the evaluation harness
    build themselves.
    """
    for obj in session.deleted:
        if isinstance(obj, audit_event_db):
            raise AuditImmutableError(
                "Audit events are append-only and cannot be deleted."
            )
    for obj in session.dirty:
        if isinstance(obj, audit_event_db) and session.is_modified(
            obj, include_collections=False
        ):
            raise AuditImmutableError(
                "Audit events are append-only and cannot be modified."
            )
