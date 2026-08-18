# ============================================================
# WAY TERO — CUSTOMER SCHEMAS
# File: app/modules/customer/schemas/__init__.py
# Phase: 2 — Customer Module
# ============================================================

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Optional, List
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


# ---- Address Schemas ----


class AddressCreate(BaseModel):
    address_type: Optional[str] = Field(None, pattern="^(HOME|WORK|OTHER)$")
    address_line_1: Optional[str] = Field(None, max_length=255)
    address_line_2: Optional[str] = Field(None, max_length=255)
    city_id: Optional[int] = None
    state_id: Optional[int] = None
    postal_code: Optional[str] = Field(None, max_length=20)
    latitude: Optional[Decimal] = None
    longitude: Optional[Decimal] = None
    is_default: bool = False


class AddressResponse(BaseModel):
    id: int
    address_type: Optional[str]
    address_line_1: Optional[str]
    address_line_2: Optional[str]
    city_id: Optional[int]
    state_id: Optional[int]
    postal_code: Optional[str]
    latitude: Optional[Decimal]
    longitude: Optional[Decimal]
    is_default: bool
    created_at: datetime

    model_config = {"from_attributes": True}


# ---- Customer Schemas ----


class CustomerProfileUpdate(BaseModel):
    first_name: Optional[str] = Field(None, max_length=150)
    last_name: Optional[str] = Field(None, max_length=150)
    gender: Optional[str] = Field(None, pattern="^(MALE|FEMALE|OTHER)$")
    date_of_birth: Optional[date] = None
    city_id: Optional[int] = None


class MobileAttachRequest(BaseModel):
    """Used by the booking flow to attach a verified mobile to a Firebase email
    account that doesn't yet have one on file. The caller must have just
    completed /auth/verify-otp with this same mobile.
    """

    mobile_number: str = Field(
        ..., min_length=10, max_length=15, pattern=r"^\d{10,15}$"
    )


class MobileOtpVerifyRequest(BaseModel):
    """Verify an OTP sent for a NEW mobile (purpose=MOBILE_ATTACH) and attach
    it to the currently authenticated account. Unlike /auth/verify-otp this
    never creates or switches to another customer account — it only persists
    the verified mobile on the existing user row.
    """

    mobile_number: str = Field(
        ..., min_length=10, max_length=15, pattern=r"^\d{10,15}$"
    )
    otp: str = Field(..., min_length=4, max_length=8)

    @field_validator("mobile_number")
    @classmethod
    def validate_mobile(cls, v: str) -> str:
        return v.strip()

    @field_validator("otp")
    @classmethod
    def validate_otp(cls, v: str) -> str:
        return v.strip()


class EmailUpdateRequest(BaseModel):
    """Attach / update the email on the current customer account. Used by the
    mobile-OTP login flow when the customer has no email on file yet — the
    web flow offers it as an optional step and continues either way.
    """

    email: str = Field(..., max_length=255)

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        v = v.strip().lower()
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v):
            raise ValueError("Enter a valid email address")
        return v


class CustomerResponse(BaseModel):
    id: int
    uuid: UUID
    customer_code: str
    first_name: Optional[str]
    last_name: Optional[str]
    gender: Optional[str]
    date_of_birth: Optional[date]
    city_id: Optional[int]
    referral_code: Optional[str]
    is_active: bool
    created_at: datetime
    updated_at: datetime
    addresses: List[AddressResponse] = []

    # Joined from users — included so the booking flow can tell whether the
    # signed-in customer already has a mobile on file (Firebase email users
    # don't, OTP users do) and can skip prompting for one. Optional so the
    # admin-facing list endpoints stay slim.
    mobile_number: Optional[str] = None
    email: Optional[str] = None

    model_config = {"from_attributes": True}


class CustomerListResponse(BaseModel):
    id: int
    uuid: UUID
    customer_code: str
    first_name: Optional[str]
    last_name: Optional[str]
    city_id: Optional[int]
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}
