import uuid

from sqlalchemy import Column, Float, ForeignKey, String
from sqlalchemy.orm import relationship

from backend.database import Base
from backend.databases.merchant_db import GUID


class mandate_db(Base):
    """The delegated authority a buyer has granted their agent.

    Two thresholds, not one:

      * `autonomous_limit`           -- act alone at or below this
      * `absolute_transaction_limit` -- the ceiling; above it the purchase
                                        is blocked and no human may approve

    Permissions are child rows rather than a serialized blob so that
    "which merchants may this mandate use" is a query, not a parse. An
    EMPTY permission list means unrestricted for that dimension; a
    non-empty list is a strict allow-list.
    """

    __tablename__ = "mandates"

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    buyer_id = Column(GUID(), ForeignKey("buyers.id"), nullable=False)
    autonomous_limit = Column(Float, nullable=False)
    absolute_transaction_limit = Column(Float, nullable=False)
    monthly_cap = Column(Float, nullable=False)

    allowed_merchants = relationship(
        "mandate_allowed_merchant_db",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    allowed_categories = relationship(
        "mandate_allowed_category_db",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class mandate_allowed_merchant_db(Base):
    """One merchant this mandate permits."""

    __tablename__ = "mandate_allowed_merchants"

    mandate_id = Column(GUID(), ForeignKey("mandates.id"), primary_key=True)
    merchant_id = Column(GUID(), ForeignKey("merchants.id"), primary_key=True)


class mandate_allowed_category_db(Base):
    """One product category this mandate permits.

    Category is a plain string rather than its own table: the catalogue
    has no category entity, and inventing one buys nothing here.
    """

    __tablename__ = "mandate_allowed_categories"

    mandate_id = Column(GUID(), ForeignKey("mandates.id"), primary_key=True)
    category = Column(String, primary_key=True)
