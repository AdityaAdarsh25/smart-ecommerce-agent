from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from backend.database import utcnow_naive
from backend.enums.ApprovalStatus import ApprovalStatus


class Approval(BaseModel):
    """Domain mirror of `approval_db`.

    An APPROVED approval means a human granted the autonomous-threshold
    exception. It is not authorization to pay: every hard mandate rule is
    re-evaluated afterwards, and any of them can still block the purchase.
    """

    id: UUID = Field(default_factory=uuid4)
    transaction_id: UUID
    status: ApprovalStatus = ApprovalStatus.PENDING
    reason: str
    created_at: datetime = Field(default_factory=utcnow_naive)
    resolved_at: Optional[datetime] = None
    reviewer: Optional[str] = None
    reviewer_note: Optional[str] = None
