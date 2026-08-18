# ============================================================
# WAY TERO — VEHICLE SCHEMAS
# File: app/modules/vehicle/schemas/__init__.py
# Doc Ref: DB Schema Part 3, Sections 10-20
# Phase: 2 — Vehicle Module
# ============================================================

from datetime import date, datetime
from decimal import Decimal
from typing import Optional, List
from uuid import UUID
from pydantic import BaseModel, Field


# ---- Vehicle Category ----


class VehicleCategoryResponse(BaseModel):
    id: int
    category_name: str
    seating_capacity: Optional[int]
    luggage_capacity: Optional[int]
    is_active: bool
    model_config = {"from_attributes": True}


# ---- Vehicle Document ----


class VehicleDocumentUpload(BaseModel):
    document_type: str = Field(
        ..., description="RC / INSURANCE / FITNESS / PERMIT / PUC"
    )
    file_url: str = Field(..., max_length=1000)
    expiry_date: Optional[date] = None


class VehicleDocumentResponse(BaseModel):
    id: int
    document_type: Optional[str]
    file_url: Optional[str]
    expiry_date: Optional[date]
    verification_status: str
    uploaded_at: datetime
    verified_at: Optional[datetime]
    model_config = {"from_attributes": True}


# ---- Vehicle Photo Upload ----


class VehiclePhotoUpload(BaseModel):
    photo_type: str = Field(
        ..., description="FRONT|BACK|LEFT|RIGHT|INTERIOR|ODOMETER|ENGINE|OTHER"
    )
    file_url: str = Field(..., max_length=1000)
    caption: Optional[str] = Field(None, max_length=255)


class VehiclePhotoResponse(BaseModel):
    id: int
    photo_type: str
    file_url: str
    caption: Optional[str]
    verification_status: str
    uploaded_at: datetime
    verified_at: Optional[datetime]
    remarks: Optional[str]
    model_config = {"from_attributes": True}


# ---- Vehicle Availability ----


class VehicleAvailabilityResponse(BaseModel):
    availability_status: str
    updated_at: datetime
    model_config = {"from_attributes": True}


# ---- Vehicle Register / Update ----


class VehicleRegister(BaseModel):
    registration_number: str = Field(
        ..., max_length=50, description="Vehicle number plate"
    )
    vehicle_category_id: int
    vehicle_brand: Optional[str] = Field(None, max_length=100)
    vehicle_model: Optional[str] = Field(None, max_length=100)
    manufacturing_year: Optional[int] = Field(None, ge=1990, le=2030)
    fuel_type: Optional[str] = Field(
        None, pattern="^(PETROL|DIESEL|CNG|ELECTRIC|HYBRID)$"
    )
    seating_capacity: Optional[int] = Field(None, ge=1, le=60)
    city_id: int


class VehicleUpdate(BaseModel):
    vehicle_brand: Optional[str] = Field(None, max_length=100)
    vehicle_model: Optional[str] = Field(None, max_length=100)
    fuel_type: Optional[str] = Field(
        None, pattern="^(PETROL|DIESEL|CNG|ELECTRIC|HYBRID)$"
    )
    seating_capacity: Optional[int] = Field(None, ge=1, le=60)
    city_id: Optional[int] = None


class VehicleStatusUpdate(BaseModel):
    status: str = Field(
        ..., description="PENDING/UNDER_REVIEW/APPROVED/ACTIVE/MAINTENANCE/SUSPENDED"
    )
    remarks: Optional[str] = None


# ---- Vehicle Pricing Rule ----


class PricingRuleCreate(BaseModel):
    city_id: int
    vehicle_category_id: int
    trip_type: str = Field(
        ..., pattern="^(LOCAL|AIRPORT|OUTSTATION_ONE_WAY|OUTSTATION_ROUND_TRIP)$"
    )
    base_fare: Decimal = Field(..., ge=0)
    minimum_km: Optional[int] = Field(None, ge=0)
    per_km_rate: Decimal = Field(..., ge=0)
    driver_allowance: Optional[Decimal] = Field(None, ge=0)
    night_charge: Optional[Decimal] = Field(None, ge=0)
    effective_from: Optional[date] = None
    effective_to: Optional[date] = None


class PricingRuleResponse(BaseModel):
    id: int
    city_id: int
    vehicle_category_id: int
    trip_type: Optional[str]
    base_fare: Optional[Decimal]
    minimum_km: Optional[int]
    per_km_rate: Optional[Decimal]
    driver_allowance: Optional[Decimal]
    night_charge: Optional[Decimal]
    effective_from: Optional[date]
    effective_to: Optional[date]
    model_config = {"from_attributes": True}


# ---- Vehicle Response ----


class VehicleResponse(BaseModel):
    id: int
    uuid: UUID
    partner_id: int
    vehicle_code: Optional[str]
    registration_number: str
    vehicle_category_id: int
    vehicle_brand: Optional[str]
    vehicle_model: Optional[str]
    manufacturing_year: Optional[int]
    fuel_type: Optional[str]
    seating_capacity: Optional[int]
    city_id: int
    status: str
    created_at: datetime
    updated_at: datetime
    documents: List[VehicleDocumentResponse] = []
    photos: List[VehiclePhotoResponse] = []
    availability: Optional[VehicleAvailabilityResponse] = None
    model_config = {"from_attributes": True}


class VehicleListResponse(BaseModel):
    id: int
    uuid: UUID
    vehicle_code: Optional[str]
    registration_number: str
    vehicle_category_id: int
    vehicle_brand: Optional[str]
    vehicle_model: Optional[str]
    manufacturing_year: Optional[int]
    fuel_type: Optional[str]
    seating_capacity: Optional[int]
    city_id: int
    status: str
    created_at: datetime
    doc_count: int = (
        0  # Number of required docs (RC/INSURANCE/FITNESS/PERMIT/PUC) uploaded
    )
    model_config = {"from_attributes": True}
