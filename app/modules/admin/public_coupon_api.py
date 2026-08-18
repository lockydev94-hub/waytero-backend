# ============================================================
# WAYTERO — PUBLIC COUPON VALIDATION (no auth)
# File: app/modules/admin/public_coupon_api.py
# Doc Ref: Migration 0020_coupon_system, public_hotel_api.py
#
# The customer-web booking review modal needs a live discount preview
# before the booking is created. The admin coupon router lives at
# /admin/coupons and requires auth; this thin wrapper reuses the exact
# same validate_coupon logic so the preview and the server-side
# re-validation at booking creation can never disagree.
#
#   POST /public/coupon/validate
# ============================================================

from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.modules.admin.coupon_api import (
    ValidateRequest,
    ValidateResponse,
    validate_coupon,
)

router = APIRouter()


class PublicCouponValidateIn(BaseModel):
    coupon_code: str
    service_type: str = "HOTEL"  # CAB | HOTEL | TOUR
    vehicle_category_id: Optional[int] = None
    city_id: Optional[int] = None
    booking_amount: Decimal
    customer_id: int = 0  # 0 = not-yet-known; per-customer usage cap is skipped
    customer_mobile: Optional[str] = None


@router.post(
    "/validate",
    response_model=ValidateResponse,
    summary="Preview a coupon discount before booking (no auth)",
)
async def public_validate_coupon(
    payload: PublicCouponValidateIn,
    db: AsyncSession = Depends(get_db),
):
    """Same eligibility rules as the admin validate endpoint, callable from
    the public site. customer_id 0 skips the per-customer usage check (the
    customer row may not exist yet); booking creation re-validates with the
    real id and persists the discount."""
    return await validate_coupon(
        ValidateRequest(
            coupon_code=payload.coupon_code,
            service_type=payload.service_type,
            vehicle_category_id=payload.vehicle_category_id,
            city_id=payload.city_id,
            booking_amount=payload.booking_amount,
            customer_id=payload.customer_id,
            customer_mobile=payload.customer_mobile,
        ),
        db,
    )
