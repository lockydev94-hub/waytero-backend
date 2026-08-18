# ============================================================
# WAY TERO — PARTNER SCHEMAS
# File: app/modules/partner/schemas/__init__.py
# Phase: 2 — Partner Module
# ============================================================

from datetime import date, datetime
from typing import Optional, List
from uuid import UUID

from pydantic import BaseModel, Field, field_validator
import re


class PartnerRegisterRequest(BaseModel):
    partner_type: str = Field(..., pattern="^(INDIVIDUAL|COMPANY)$")
    owner_name: str = Field(..., min_length=2, max_length=255)
    business_name: Optional[str] = Field(None, max_length=255)
    mobile: str
    email: Optional[str] = None
    city_id: int
    onboarding_source: Optional[str] = None
    # Office address — Doc Ref: BRD Part 2 §20 | Migration 0016
    # office_city_id must match city_id — enforced at application layer
    office_address_line_1: Optional[str] = Field(None, max_length=255)
    office_address_line_2: Optional[str] = Field(None, max_length=255)
    office_city_id: Optional[int] = None
    office_state_id: Optional[int] = None
    office_postal_code: Optional[str] = Field(None, max_length=20)

    @field_validator("mobile")
    @classmethod
    def validate_mobile(cls, v: str) -> str:
        v = v.strip().replace(" ", "").replace("-", "")
        if not re.match(r"^[6-9]\d{9}$", v):
            raise ValueError("Enter a valid 10-digit Indian mobile number")
        return v


class PartnerProfileUpdate(BaseModel):
    business_name: Optional[str] = Field(None, max_length=255)
    owner_name: Optional[str] = Field(None, max_length=255)
    email: Optional[str] = None
    city_id: Optional[int] = None
    logo_url: Optional[str] = None
    # Office address — Doc Ref: BRD Part 2 §20 | Migration 0016
    office_address_line_1: Optional[str] = Field(None, max_length=255)
    office_address_line_2: Optional[str] = Field(None, max_length=255)
    office_city_id: Optional[int] = None
    office_state_id: Optional[int] = None
    office_postal_code: Optional[str] = Field(None, max_length=20)


class PartnerServiceUpdate(BaseModel):
    service_type: str = Field(
        ..., min_length=2, max_length=50
    )  # validated against DB at service layer
    is_active: bool = True


class PartnerDocumentUpload(BaseModel):
    document_type: str
    document_number: Optional[str] = None
    file_url: str
    expiry_date: Optional[date] = None


class BankAccountCreate(BaseModel):
    account_holder_name: str = Field(..., max_length=255)
    account_number_encrypted: str  # Must be encrypted before sending
    ifsc_code: str = Field(..., max_length=20)
    bank_name: str = Field(..., max_length=255)
    branch_name: Optional[str] = None
    account_type: str = Field("SAVINGS", pattern="^(SAVINGS|CURRENT)$")
    is_primary: bool = False


class GSTDetailsCreate(BaseModel):
    gst_number: str = Field(..., max_length=20)
    pan_number: Optional[str] = Field(
        None, max_length=20
    )  # BRD §18: PAN required for company
    legal_name: Optional[str] = None
    trade_name: Optional[str] = None
    registration_date: Optional[date] = None
    gst_status: Optional[str] = None


class DocumentResponse(BaseModel):
    id: int
    document_type: str
    document_number: Optional[str]
    file_url: str
    verification_status: Optional[str]
    remarks: Optional[str]
    expiry_date: Optional[date]
    uploaded_at: datetime
    verified_at: Optional[datetime]

    model_config = {"from_attributes": True}


class BankAccountResponse(BaseModel):
    id: int
    account_holder_name: Optional[str]
    ifsc_code: Optional[str]
    bank_name: Optional[str]
    branch_name: Optional[str]
    account_type: Optional[str]
    is_primary: bool
    verification_status: Optional[str]
    created_at: datetime

    model_config = {"from_attributes": True}


class PartnerServiceResponse(BaseModel):
    id: int
    service_type: str
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class GSTDetailsResponse(BaseModel):
    id: int
    gst_number: Optional[str]
    pan_number: Optional[str]
    legal_name: Optional[str]
    trade_name: Optional[str]
    registration_date: Optional[date]
    gst_status: Optional[str]
    verified_at: Optional[datetime]

    model_config = {"from_attributes": True}


class PartnerResponse(BaseModel):
    id: int
    uuid: UUID
    partner_code: str
    partner_type: str
    business_name: Optional[str]
    owner_name: str
    mobile: str
    email: Optional[str]
    city_id: int
    logo_url: Optional[str]
    # Office address — Doc Ref: BRD Part 2 §20 | Migration 0016
    office_address_line_1: Optional[str]
    office_address_line_2: Optional[str]
    office_city_id: Optional[int]
    office_state_id: Optional[int]
    office_postal_code: Optional[str]
    status: str
    onboarding_source: Optional[str]
    approved_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime
    services: List[PartnerServiceResponse] = []
    documents: List[DocumentResponse] = []
    bank_accounts: List[BankAccountResponse] = []
    gst_details: Optional[GSTDetailsResponse] = None

    model_config = {"from_attributes": True}


class PartnerListResponse(BaseModel):
    id: int
    uuid: UUID
    partner_code: str
    partner_type: str
    owner_name: str
    business_name: Optional[str]
    city_id: int
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}


class PartnerStatusUpdate(BaseModel):
    status: str = Field(
        ...,
        pattern="^(PENDING|UNDER_REVIEW|DOCUMENT_PENDING|APPROVED|ACTIVE|SUSPENDED|BLOCKED)$",
    )
    remarks: Optional[str] = None
