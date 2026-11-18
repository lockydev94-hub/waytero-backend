from typing import Literal, Optional

from pydantic import BaseModel, Field


class CreatePaymentOrderIn(BaseModel):
    booking_number: str = Field(..., min_length=4, max_length=100)
    payment_type: Literal["FULL", "ADVANCE"] = "FULL"


class VerifyPaymentIn(BaseModel):
    razorpay_order_id: str = Field(..., min_length=4, max_length=120)
    razorpay_payment_id: str = Field(..., min_length=4, max_length=120)
    razorpay_signature: str = Field(..., min_length=10, max_length=256)


class PaymentOrderOut(BaseModel):
    booking_number: str
    payment_type: str
    amount: float
    amount_paise: int
    currency: str
    key_id: str
    order_id: str


class PaymentVerificationOut(BaseModel):
    booking_number: str
    payment_type: str
    amount: float
    currency: str
    payment_status: str
    booking_payment_status: Optional[str] = None
    balance_due: Optional[float] = None
    receipt_number: Optional[str] = None
    gateway_payment_id: str
