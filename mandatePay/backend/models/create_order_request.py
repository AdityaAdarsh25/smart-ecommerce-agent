from uuid import UUID

from pydantic import BaseModel


class CreateOrderRequest(BaseModel):
    """Order creation takes a quote, and nothing else.

    Quantity and price are not inputs here: they were fixed server-side
    when the quote was issued. This is what makes "no caller controls the
    financial amount" structural rather than a convention.
    """

    quote_id: UUID
