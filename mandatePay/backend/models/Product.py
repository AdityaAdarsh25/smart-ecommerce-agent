from typing import Optional
from uuid import UUID,uuid4

from pydantic import BaseModel,Field

class Product(BaseModel):
    product_id:UUID=Field(default_factory=uuid4)
    product_name:str
    brand:str
    cost:float
    category:Optional[str]=None
    quantity:int
    merchant_id:UUID

