import uuid
from sqlalchemy import Column,String,Float

from backend.database import Base
from backend.databases.merchant_db import GUID

class buyer_db(Base):
    __tablename__="buyers"
    id=Column(GUID(),primary_key=True,default=uuid.uuid4)
    name=Column(String,nullable=False)
    balance=Column(Float,nullable=False)



