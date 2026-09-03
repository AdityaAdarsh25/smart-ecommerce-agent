from pydantic import BaseModel, Field


class OrderRequest(BaseModel):
    buyer_id: str
    product_id: str
    # Rejected at the schema boundary: a zero or negative quantity produced a
    # zero or negative amount that passed every downstream check.
    quantity: int = Field(..., ge=1)
