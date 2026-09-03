from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class Mandate(BaseModel):
    """Domain mirror of `mandate_db`: the buyer's delegated authority.

    `autonomous_limit` is where the agent must stop and ask a human.
    `absolute_transaction_limit` is where the mandate itself stops -- no
    human approval reaches past it.

    An empty allow-list means unrestricted for that dimension; a non-empty
    one is a strict allow-list.
    """

    id: UUID = Field(default_factory=uuid4)
    buyer_id: UUID
    autonomous_limit: float
    absolute_transaction_limit: float
    monthly_cap: float
    allowed_merchants: list[UUID] = Field(default_factory=list)
    allowed_categories: list[str] = Field(default_factory=list)
