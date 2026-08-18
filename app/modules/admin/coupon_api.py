# ============================================================
# WAYTERO — COUPON SYSTEM API
# File: app/modules/admin/coupon_api.py
# Prefix: /admin/coupons  (registered in api/router.py)
# Doc Ref: Migration 0020_coupon_system
# ============================================================

from typing import List, Optional
from datetime import date, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, delete
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.modules.admin.coupon_models import (
    Coupon,
    CouponServiceRule,
    CouponCityRule,
    CouponCustomerRule,
    CouponUsage,
)

router = APIRouter()


# ── Pydantic Schemas ─────────────────────────────────────────


class CouponServiceRuleIn(BaseModel):
    service_type: str  # CAB | HOTEL | TOUR
    vehicle_category_id: Optional[int] = None


class CouponCityRuleIn(BaseModel):
    city_id: int
    max_discount_override: Optional[Decimal] = None


class CouponCreate(BaseModel):
    coupon_code: str = Field(..., min_length=3, max_length=50)
    title: str
    description: Optional[str] = None
    apply_to: str = "ALL"  # ALL | CAB | HOTEL | TOUR
    discount_type: str  # PERCENTAGE | FLAT
    discount_value: Decimal
    max_discount_amount: Optional[Decimal] = None
    min_booking_amount: Decimal = Decimal("0")
    max_usage_total: Optional[int] = None
    max_usage_per_customer: int = 1
    valid_from: date
    valid_to: date
    is_customer_specific: bool = False
    is_city_specific: bool = False
    # Relations
    service_rules: List[CouponServiceRuleIn] = []
    city_rules: List[CouponCityRuleIn] = []
    customer_mobiles: List[str] = []  # Mobile numbers for customer-specific


class CouponUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    discount_value: Optional[Decimal] = None
    max_discount_amount: Optional[Decimal] = None
    min_booking_amount: Optional[Decimal] = None
    max_usage_total: Optional[int] = None
    max_usage_per_customer: Optional[int] = None
    valid_from: Optional[date] = None
    valid_to: Optional[date] = None
    is_active: Optional[bool] = None
    service_rules: Optional[List[CouponServiceRuleIn]] = None
    city_rules: Optional[List[CouponCityRuleIn]] = None
    customer_mobiles: Optional[List[str]] = None


class CouponServiceRuleOut(BaseModel):
    id: int
    service_type: str
    vehicle_category_id: Optional[int]

    class Config:
        from_attributes = True


class CouponCityRuleOut(BaseModel):
    id: int
    city_id: int
    max_discount_override: Optional[Decimal]

    class Config:
        from_attributes = True


class CouponCustomerRuleOut(BaseModel):
    id: int
    mobile_number: str
    customer_id: Optional[int]

    class Config:
        from_attributes = True


class CouponOut(BaseModel):
    id: int
    coupon_code: str
    title: str
    description: Optional[str]
    apply_to: str
    discount_type: str
    discount_value: Decimal
    max_discount_amount: Optional[Decimal]
    min_booking_amount: Decimal
    max_usage_total: Optional[int]
    max_usage_per_customer: int
    current_usage_count: int
    valid_from: date
    valid_to: date
    is_customer_specific: bool
    is_city_specific: bool
    is_active: bool
    created_at: datetime
    updated_at: datetime
    service_rules: List[CouponServiceRuleOut] = []
    city_rules: List[CouponCityRuleOut] = []
    customer_rules: List[CouponCustomerRuleOut] = []

    class Config:
        from_attributes = True


class ValidateRequest(BaseModel):
    coupon_code: str
    service_type: str  # CAB | HOTEL | TOUR
    vehicle_category_id: Optional[int] = None
    city_id: Optional[int] = None
    booking_amount: Decimal
    customer_id: int
    customer_mobile: Optional[str] = None


class ValidateResponse(BaseModel):
    valid: bool
    discount_amount: Decimal = Decimal("0")
    final_amount: Decimal = Decimal("0")
    message: str


# ── Helpers ───────────────────────────────────────────────────


async def _load_coupon(db: AsyncSession, coupon_id: int) -> Coupon:
    q = (
        select(Coupon)
        .options(
            selectinload(Coupon.service_rules),
            selectinload(Coupon.city_rules),
            selectinload(Coupon.customer_rules),
        )
        .where(Coupon.id == coupon_id)
    )
    result = await db.execute(q)
    coupon = result.scalar_one_or_none()
    if not coupon:
        raise HTTPException(status_code=404, detail="Coupon not found")
    return coupon


async def _save_relations(
    db: AsyncSession,
    coupon: Coupon,
    service_rules: List[CouponServiceRuleIn],
    city_rules: List[CouponCityRuleIn],
    customer_mobiles: List[str],
):
    # Service rules
    await db.execute(
        delete(CouponServiceRule).where(CouponServiceRule.coupon_id == coupon.id)
    )
    for sr in service_rules:
        db.add(
            CouponServiceRule(
                coupon_id=coupon.id,
                service_type=sr.service_type,
                vehicle_category_id=sr.vehicle_category_id,
            )
        )

    # City rules
    await db.execute(
        delete(CouponCityRule).where(CouponCityRule.coupon_id == coupon.id)
    )
    for cr in city_rules:
        db.add(
            CouponCityRule(
                coupon_id=coupon.id,
                city_id=cr.city_id,
                max_discount_override=cr.max_discount_override,
            )
        )

    # Customer rules
    await db.execute(
        delete(CouponCustomerRule).where(CouponCustomerRule.coupon_id == coupon.id)
    )
    for mobile in customer_mobiles:
        db.add(CouponCustomerRule(coupon_id=coupon.id, mobile_number=mobile.strip()))


# ── Routes ────────────────────────────────────────────────────


@router.get("", response_model=List[CouponOut], tags=["Coupons"])
async def list_coupons(
    is_active: Optional[bool] = Query(None),
    apply_to: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    q = (
        select(Coupon)
        .options(
            selectinload(Coupon.service_rules),
            selectinload(Coupon.city_rules),
            selectinload(Coupon.customer_rules),
        )
        .order_by(Coupon.created_at.desc())
    )
    if is_active is not None:
        q = q.where(Coupon.is_active == is_active)
    if apply_to:
        q = q.where(Coupon.apply_to == apply_to.upper())
    result = await db.execute(q)
    return result.scalars().all()


@router.post("", response_model=CouponOut, status_code=201, tags=["Coupons"])
async def create_coupon(payload: CouponCreate, db: AsyncSession = Depends(get_db)):
    # Duplicate code guard
    exists = await db.execute(
        select(Coupon).where(Coupon.coupon_code == payload.coupon_code.upper())
    )
    if exists.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Coupon code already exists")

    coupon = Coupon(
        coupon_code=payload.coupon_code.upper().strip(),
        title=payload.title,
        description=payload.description,
        apply_to=payload.apply_to.upper(),
        discount_type=payload.discount_type.upper(),
        discount_value=payload.discount_value,
        max_discount_amount=payload.max_discount_amount,
        min_booking_amount=payload.min_booking_amount,
        max_usage_total=payload.max_usage_total,
        max_usage_per_customer=payload.max_usage_per_customer,
        valid_from=payload.valid_from,
        valid_to=payload.valid_to,
        is_customer_specific=payload.is_customer_specific,
        is_city_specific=payload.is_city_specific,
    )
    db.add(coupon)
    await db.flush()  # get coupon.id

    await _save_relations(
        db, coupon, payload.service_rules, payload.city_rules, payload.customer_mobiles
    )
    await db.commit()
    return await _load_coupon(db, coupon.id)


@router.get("/{coupon_id}", response_model=CouponOut, tags=["Coupons"])
async def get_coupon(coupon_id: int, db: AsyncSession = Depends(get_db)):
    return await _load_coupon(db, coupon_id)


@router.put("/{coupon_id}", response_model=CouponOut, tags=["Coupons"])
async def update_coupon(
    coupon_id: int, payload: CouponUpdate, db: AsyncSession = Depends(get_db)
):
    coupon = await _load_coupon(db, coupon_id)

    for field, val in payload.model_dump(
        exclude_none=True, exclude={"service_rules", "city_rules", "customer_mobiles"}
    ).items():
        setattr(coupon, field, val)

    if (
        payload.service_rules is not None
        or payload.city_rules is not None
        or payload.customer_mobiles is not None
    ):
        await _save_relations(
            db,
            coupon,
            payload.service_rules or [],
            payload.city_rules or [],
            payload.customer_mobiles or [],
        )

    await db.commit()
    return await _load_coupon(db, coupon.id)


@router.delete("/{coupon_id}", status_code=204, tags=["Coupons"])
async def delete_coupon(coupon_id: int, db: AsyncSession = Depends(get_db)):
    coupon = await _load_coupon(db, coupon_id)
    # Check if used
    usage = await db.execute(
        select(CouponUsage).where(CouponUsage.coupon_id == coupon_id).limit(1)
    )
    if usage.scalar_one_or_none():
        raise HTTPException(
            status_code=400,
            detail="Cannot delete coupon with existing usages. Deactivate it instead.",
        )
    await db.delete(coupon)
    await db.commit()


@router.post("/validate", response_model=ValidateResponse, tags=["Coupons"])
async def validate_coupon(payload: ValidateRequest, db: AsyncSession = Depends(get_db)):
    """
    Validates coupon eligibility and returns the discount amount.
    Enforces:
      - Active + within validity window
      - Service type match
      - Cab vehicle category match (if specified)
      - City restriction
      - Customer whitelist
      - Usage limits (global + per customer)
      - Discount does not exceed booking_amount (flat protection)
      - Discount does not exceed city base_fare (city-price guard)
    """
    today = date.today()

    # Load coupon
    q = (
        select(Coupon)
        .options(
            selectinload(Coupon.service_rules),
            selectinload(Coupon.city_rules),
            selectinload(Coupon.customer_rules),
        )
        .where(Coupon.coupon_code == payload.coupon_code.upper())
    )
    result = await db.execute(q)
    coupon = result.scalar_one_or_none()

    def fail(msg: str) -> ValidateResponse:
        return ValidateResponse(
            valid=False, message=msg, final_amount=payload.booking_amount
        )

    if not coupon:
        return fail("Invalid coupon code")
    if not coupon.is_active:
        return fail("This coupon is inactive")
    if today < coupon.valid_from or today > coupon.valid_to:
        return fail("Coupon has expired or is not yet valid")
    if payload.booking_amount < coupon.min_booking_amount:
        return fail(f"Minimum booking amount ₹{coupon.min_booking_amount} required")

    # Global usage limit
    if (
        coupon.max_usage_total is not None
        and coupon.current_usage_count >= coupon.max_usage_total
    ):
        return fail("Coupon usage limit reached")

    # Per-customer usage
    cust_usage = await db.execute(
        select(func.count(CouponUsage.id)).where(
            and_(
                CouponUsage.coupon_id == coupon.id,
                CouponUsage.customer_id == payload.customer_id,
            )
        )
    )
    if cust_usage.scalar() >= coupon.max_usage_per_customer:
        return fail("You have already used this coupon the maximum number of times")

    # Customer whitelist check
    if coupon.is_customer_specific:
        mobiles = {r.mobile_number for r in coupon.customer_rules}
        if not payload.customer_mobile or payload.customer_mobile not in mobiles:
            return fail("This coupon is not applicable to your account")

    # Service type check
    svc_upper = payload.service_type.upper()
    if coupon.apply_to != "ALL" and coupon.apply_to != svc_upper:
        return fail(f"Coupon not valid for {payload.service_type} service")

    if coupon.service_rules:
        # Must match at least one service rule
        matching = [
            r
            for r in coupon.service_rules
            if r.service_type == svc_upper
            and (
                r.vehicle_category_id is None
                or r.vehicle_category_id == payload.vehicle_category_id
            )
        ]
        if not matching:
            return fail("Coupon not valid for the selected vehicle category")

    # City restriction check + city-level discount cap
    city_max_discount: Optional[Decimal] = None
    if coupon.is_city_specific:
        if not payload.city_id:
            return fail("City is required for this coupon")
        city_match = next(
            (r for r in coupon.city_rules if r.city_id == payload.city_id), None
        )
        if not city_match:
            return fail("Coupon not valid in your city")
        city_max_discount = city_match.max_discount_override

    # Calculate discount
    if coupon.discount_type == "FLAT":
        raw_discount = coupon.discount_value
    else:  # PERCENTAGE
        raw_discount = (
            payload.booking_amount * coupon.discount_value / Decimal("100")
        ).quantize(Decimal("0.01"))
        # Apply percentage cap
        if coupon.max_discount_amount:
            raw_discount = min(raw_discount, coupon.max_discount_amount)

    # Global cap
    if coupon.max_discount_amount:
        raw_discount = min(raw_discount, coupon.max_discount_amount)

    # City-level cap
    if city_max_discount is not None:
        raw_discount = min(raw_discount, city_max_discount)

    # Cannot exceed booking amount (protect against negative final amount)
    raw_discount = min(raw_discount, payload.booking_amount)

    final = payload.booking_amount - raw_discount
    return ValidateResponse(
        valid=True,
        discount_amount=raw_discount,
        final_amount=final,
        message=f"Coupon applied! You save ₹{raw_discount}",
    )


@router.get("/{coupon_id}/usages", tags=["Coupons"])
async def coupon_usages(
    coupon_id: int,
    page: int = Query(1, ge=1, description="Page number (1-based)"),
    page_size: int = Query(20, ge=1, le=100, description="Records per page"),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns paginated usage records for a coupon.
    Response includes: items, total, page, page_size, total_pages.
    """
    # Verify coupon exists
    exists = await db.execute(select(Coupon.id).where(Coupon.id == coupon_id))
    if not exists.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Coupon not found")

    # Total count
    count_q = select(func.count(CouponUsage.id)).where(
        CouponUsage.coupon_id == coupon_id
    )
    total: int = (await db.execute(count_q)).scalar() or 0

    # Paginated records
    offset = (page - 1) * page_size
    result = await db.execute(
        select(CouponUsage)
        .where(CouponUsage.coupon_id == coupon_id)
        .order_by(CouponUsage.used_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    usages = result.scalars().all()

    import math

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": math.ceil(total / page_size) if total > 0 else 1,
        "items": [
            {
                "id": u.id,
                "customer_id": u.customer_id,
                "master_booking_id": u.master_booking_id,
                "discount_applied": float(u.discount_applied),
                "used_at": u.used_at.isoformat(),
            }
            for u in usages
        ],
    }
