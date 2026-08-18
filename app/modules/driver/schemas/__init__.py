from datetime import date, datetime
from typing import Optional, List
from uuid import UUID
from pydantic import BaseModel, Field


class DriverCreate(BaseModel):
    full_name: str = Field(..., max_length=255)
    mobile: str
    email: Optional[str] = None
    license_number: str = Field(..., max_length=100)
    license_expiry_date: Optional[date] = None
    date_of_birth: Optional[date] = None
    joining_date: Optional[date] = None


class DriverUpdate(BaseModel):
    full_name: Optional[str] = Field(None, max_length=255)
    email: Optional[str] = None
    license_expiry_date: Optional[date] = None


class DriverDocumentUpload(BaseModel):
    document_type: str
    file_url: str
    expiry_date: Optional[date] = None


class DriverAvailabilityUpdate(BaseModel):
    availability_status: str = Field(..., pattern="^(ONLINE|OFFLINE|BREAK)$")


class DriverStatusUpdate(BaseModel):
    status: str = Field(
        ..., pattern="^(PENDING|UNDER_REVIEW|APPROVED|ACTIVE|INACTIVE|SUSPENDED)$"
    )
    remarks: Optional[str] = None


class DriverDocumentResponse(BaseModel):
    id: int
    document_type: str
    file_url: str
    verification_status: Optional[str]
    expiry_date: Optional[date]
    uploaded_at: datetime
    model_config = {"from_attributes": True}


class DriverAvailabilityResponse(BaseModel):
    availability_status: str
    last_online_at: Optional[datetime]
    updated_at: datetime
    model_config = {"from_attributes": True}


class DriverResponse(BaseModel):
    id: int
    uuid: UUID
    driver_code: str
    partner_id: int
    full_name: str
    mobile: str
    email: Optional[str]
    license_number: str
    license_expiry_date: Optional[date]
    date_of_birth: Optional[date]
    joining_date: Optional[date]
    status: str
    approved_at: Optional[datetime]
    created_at: datetime
    documents: List[DriverDocumentResponse] = []
    availability: Optional[DriverAvailabilityResponse] = None
    model_config = {"from_attributes": True}


class DriverListResponse(BaseModel):
    id: int
    uuid: UUID
    driver_code: str
    full_name: str
    mobile: str
    status: str
    partner_id: int
    created_at: datetime
    model_config = {"from_attributes": True}
