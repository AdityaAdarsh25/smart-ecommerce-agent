import uuid
from sqlalchemy import Column, Float, ForeignKey, Integer, String

from backend.database import Base
from backend.databases.merchant_db import GUID

class product_db(Base):
    __tablename__="products"
    product_id=Column(GUID(),primary_key=True,default=uuid.uuid4)
    product_name=Column(String,nullable=False)
    brand=Column(String,nullable=False)
    cost=Column(Float,nullable=False)
    # Nullable so pre-Package-3 rows stay honest rather than being
    # backfilled with an invented category. A product with no category
    # cannot satisfy a mandate that restricts categories.
    category=Column(String,nullable=True)
    quantity=Column(Integer,nullable=False)
    merchant_id=Column(GUID(),ForeignKey("merchants.id"),nullable=False)