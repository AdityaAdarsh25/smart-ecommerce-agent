from pydantic import BaseModel, Field

from backend.models.Product import Product


class RankedCandidate(BaseModel):
    product: Product
    score: float = 0.0
    reasons: list[str] = Field(default_factory=list)
