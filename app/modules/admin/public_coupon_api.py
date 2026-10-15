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
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

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
    hotel_id: Optional[int] = None
    # For HOTEL previews of partner-funded coupons (migration 0075): the
    # fulfilling partner is resolved from this hotel server-side — a client
    # supplied partner_id would be forgeable.
    tour_package_id: Optional[int] = None
    # For TOUR previews: same anti-forgery rule — the fulfilling partner and
    # city come from the package row, matching what booking creation validates.
    pickup_datetime: Optional[datetime] = None
    return_datetime: Optional[datetime] = None
    # For CAB previews of partner-funded coupons: joined-partner availability
    # is trip-window aware, so the client must send the same window it will
    # book with. Booking creation re-validates server-side regardless.


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
    hotel_partner_id = None
    if payload.hotel_id:
        hotel_partner_id = (
            await db.execute(
                text(
                    "SELECT partner_id FROM hotels WHERE id = :hid AND deleted_at IS NULL"
                ),
                {"hid": payload.hotel_id},
            )
        ).scalar()

    tour_partner_id = None
    tour_city_id = None
    if payload.tour_package_id:
        tour_row = (
            (
                await db.execute(
                    text(
                        "SELECT partner_id, city_id FROM tour_packages WHERE id = :tid"
                    ),
                    {"tid": payload.tour_package_id},
                )
            )
            .mappings()
            .first()
        )
        if tour_row:
            tour_partner_id = tour_row["partner_id"]
            tour_city_id = tour_row["city_id"]

    return await validate_coupon(
        ValidateRequest(
            coupon_code=payload.coupon_code,
            service_type=payload.service_type,
            vehicle_category_id=payload.vehicle_category_id,
            city_id=payload.city_id or tour_city_id,
            booking_amount=payload.booking_amount,
            customer_id=payload.customer_id,
            customer_mobile=payload.customer_mobile,
            partner_id=hotel_partner_id if payload.hotel_id else tour_partner_id,
            pickup_datetime=payload.pickup_datetime,
            return_datetime=payload.return_datetime,
        ),
        db,
    )
