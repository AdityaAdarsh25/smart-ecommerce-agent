import uuid
from sqlalchemy import Column,String
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.types import CHAR,TypeDecorator

from backend.database import Base

class GUID(TypeDecorator):
    impl=CHAR
    cache_ok=True
    def load_dialect_impl(self, dialect):
        if dialect.name=="postgresql":
            return dialect.type_descriptor(PGUUID())
        return dialect.type_descriptor(CHAR(36))
    
    def process_bind_param(self, value, dialect):
        if value is None:
            return value
        return str(value)
    def process_result_value(self, value, dialect):
        if value is None:
            return value
        return uuid.UUID(value)

class merchant_db(Base):
    __tablename__="merchants"
    id=Column(GUID(),primary_key=True,default=uuid.uuid4)
    name=Column(String,nullable=False)
    