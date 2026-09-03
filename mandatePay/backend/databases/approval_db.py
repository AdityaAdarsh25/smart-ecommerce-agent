import uuid

from sqlalchemy import Column, DateTime, ForeignKey, String
from sqlalchemy import Enum as SqlEnum

from backend.database import Base, utcnow_naive
from backend.databases.merchant_db import GUID

# Imported for its side effect: `transactions` must be registered on the
# shared metadata before `approvals.transaction_id` can resolve its key.
from backend.databases.transaction_db import transaction_db  # noqa: F401
from backend.enums.ApprovalStatus import ApprovalStatus


class approval_db(Base):
    """A request for a human to authorize one transaction.

    Deliberately thin. There is no user or role system behind `reviewer` --
    Buildathon v1 has a single demo approver, and inventing an auth
    subsystem to name them would be scope no one asked for.

    At most one PENDING approval may exist per transaction; the route
    enforces that, which is what stops a pile of identical unresolved
    requests accumulating.
    """

    __tablename__ = "approvals"

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    transaction_id = Column(GUID(), ForeignKey("transactions.id"), nullable=False)
    status = Column(SqlEnum(ApprovalStatus), nullable=False, default=ApprovalStatus.PENDING)
    reason = Column(String, nullable=False)
    created_at = Column(DateTime, default=utcnow_naive, nullable=False)

    # Set exactly once, when a human resolves the request.
    resolved_at = Column(DateTime, nullable=True)
    reviewer = Column(String, nullable=True)
    reviewer_note = Column(String, nullable=True)
