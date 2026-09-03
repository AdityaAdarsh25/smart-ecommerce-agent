from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from backend.database import utcnow_naive
from backend.enums.TransactionStatus import TransactionStatus


class Transaction(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    buyer_id: UUID
    product_id: UUID
    quantity: int
    amount: float
    status: TransactionStatus
    reason: str
    quote_id: Optional[UUID] = None
    razorpay_order_id: Optional[str] = None
    # Set only by verified payment. Its presence is the record that money moved.
    razorpay_payment_id: Optional[str] = None
    timestamp: datetime = Field(default_factory=utcnow_naive)
    # Authoritative settlement time. NULL until a verified payment sets it.
    paid_at: Optional[datetime] = None
