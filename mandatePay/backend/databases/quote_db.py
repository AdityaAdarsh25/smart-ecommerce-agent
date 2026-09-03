import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Integer
from sqlalchemy import Enum as SqlEnum

from backend.database import Base, utcnow_naive
from backend.databases.merchant_db import GUID
from backend.enums.QuoteStatus import QuoteStatus
from backend.money import MoneyPaise


class quote_db(Base):
    """An immutable server-side price snapshot with an expiry.

    `quoted_unit_price` and `quoted_total` are written once, from the
    catalogue price the server read at quote time, and are never rewritten
    -- a drifted price invalidates the quote instead of updating it. The
    caller has no way to influence either value.
    """

    __tablename__ = "quotes"

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    buyer_id = Column(GUID(), ForeignKey("buyers.id"), nullable=False)
    product_id = Column(GUID(), ForeignKey("products.product_id"), nullable=False)
    quantity = Column(Integer, nullable=False)

    # Integer paise on disk, Decimal rupees in Python. See backend/money.py.
    quoted_unit_price = Column(MoneyPaise(), nullable=False)
    quoted_total = Column(MoneyPaise(), nullable=False)

    status = Column(SqlEnum(QuoteStatus), nullable=False, default=QuoteStatus.ACTIVE)
    created_at = Column(DateTime, default=utcnow_naive, nullable=False)
    expires_at = Column(DateTime, nullable=False)

    def is_expired(self, now=None) -> bool:
        return self.expires_at <= (now or utcnow_naive())
