from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from backend.database import utcnow_naive
from backend.enums.QuoteStatus import QuoteStatus


class Quote(BaseModel):
    """Domain mirror of `quote_db`.

    The price fields are Decimal, never float: a quote is the one place a
    price is frozen, so it must be frozen exactly.
    """

    id: UUID = Field(default_factory=uuid4)
    buyer_id: UUID
    product_id: UUID
    quantity: int = Field(..., ge=1)
    quoted_unit_price: Decimal
    quoted_total: Decimal
    status: QuoteStatus = QuoteStatus.ACTIVE
    created_at: datetime = Field(default_factory=utcnow_naive)
    expires_at: datetime
