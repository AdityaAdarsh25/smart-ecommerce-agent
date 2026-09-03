from uuid import UUID

from pydantic import BaseModel, Field


class PaymentVerificationRequest(BaseModel):
    """Exactly what Razorpay Checkout hands back, plus our own binding.

    Deliberately absent: amount, currency and status. Those are ours to
    know, not the caller's to assert -- accepting them would let a caller
    talk us into settling a transaction for a different sum.

    The signature is used for verification and then discarded; it is never
    persisted.
    """

    # Binds the provider callback to one of our transactions.
    transaction_id: UUID

    razorpay_order_id: str = Field(..., min_length=1)
    razorpay_payment_id: str = Field(..., min_length=1)
    razorpay_signature: str = Field(..., min_length=1)
