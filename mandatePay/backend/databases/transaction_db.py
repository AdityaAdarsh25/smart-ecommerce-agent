import uuid

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy import Enum as SqlEnum

from backend.database import Base, utcnow_naive
from backend.databases.merchant_db import GUID

# Imported for its side effect: the `quotes` table must be registered on the
# shared metadata before `transactions.quote_id` can resolve its foreign key.
from backend.databases.quote_db import quote_db  # noqa: F401
from backend.enums.TransactionStatus import TransactionStatus


class transaction_db(Base):
    __tablename__ = "transactions"
    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    buyer_id = Column(GUID(), ForeignKey("buyers.id"), nullable=False)
    product_id = Column(GUID(), ForeignKey("products.product_id"), nullable=False)
    # Duplicate detection keys on quantity as well as amount: without it,
    # 2 x 100 and 1 x 200 are indistinguishable. Nullable so pre-Package-1
    # rows, which never recorded it, stay honest rather than being backfilled
    # with an invented value.
    quantity = Column(Integer, nullable=True)
    amount = Column(Float, nullable=False)
    status = Column(SqlEnum(TransactionStatus), nullable=False)
    reason = Column(String, nullable=False)
    # The priced offer this transaction was created from. Nullable so that
    # pre-Package-2 rows, which never had a quote, stay honest.
    quote_id = Column(GUID(), ForeignKey("quotes.id"), nullable=True)
    razorpay_order_id = Column(String, nullable=True)
    # Written only by successful server-side payment verification. Its
    # presence alongside PAID is the record that money actually moved.
    razorpay_payment_id = Column(String, nullable=True)
    timestamp = Column(DateTime, default=utcnow_naive, nullable=False)
    # When the money actually moved. `timestamp` is when this transaction
    # was CREATED and never changes, so it cannot answer "was this paid in
    # September?" -- an order created on 31 August and settled on 1
    # September belongs to September's allowance. Written exactly once, in
    # the same commit that writes PAID, and NULL until then. Nullable also
    # keeps pre-migration PAID rows honest: their true settlement time was
    # never recorded.
    paid_at = Column(DateTime, nullable=True)
