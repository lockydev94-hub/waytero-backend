"""Coupon management and redemption. Doc Ref: BRD Part 6 §129, §147."""

from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal
import math

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, func, delete, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.core.dependencies import require_roles
from app.core.exceptions import ResourceNotFoundException, ValidationException
from app.modules.admin.coupon_models import (
    Coupon,
    CouponServiceRule,
    CouponCityRule,
    CouponCustomerRule,
    CouponPartnerRule,
    CouponUsage,
)
from app.modules.booking.services.coupon_availability import available_cab_counts

router = APIRouter()
CouponType = Literal["PLATFORM", "PARTNER_ONLY", "PLATFORM_PARTNER"]
ServiceType = Literal["CAB", "HOTEL", "TOUR"]
PARTNER_FUNDED_TYPES = {"PARTNER_ONLY", "PLATFORM_PARTNER"}
COUPON_TYPES = {"PLATFORM", *PARTNER_FUNDED_TYPES}
CENT = Decimal("0.01")


class CouponServiceRuleIn(BaseModel):
    service_type: ServiceType
    vehicle_category_id: int | None = Field(None, gt=0)


class CouponCityRuleIn(BaseModel):
    city_id: int = Field(gt=0)
    max_discount_override: Decimal | None = Field(None, ge=0, decimal_places=2)


class CouponCreate(BaseModel):
    coupon_code: str = Field(min_length=3, max_length=50)
    title: str = Field(min_length=1, max_length=255)
    description: str | None = None
    apply_to: Literal["ALL", "CAB", "HOTEL", "TOUR"] = "ALL"
    discount_type: Literal["PERCENTAGE", "FLAT"]
    discount_value: Decimal = Field(gt=0, decimal_places=2, max_digits=12)
    max_discount_amount: Decimal | None = Field(None, ge=0, decimal_places=2)
    min_booking_amount: Decimal = Field(Decimal("0"), ge=0, decimal_places=2)
    max_usage_total: int | None = Field(None, gt=0)
    max_usage_per_customer: int = Field(1, gt=0)
    valid_from: date
    valid_to: date
    is_customer_specific: bool = False
    is_city_specific: bool = False
    coupon_type: CouponType = "PLATFORM"
    platform_share_percent: int | None = Field(None, ge=1, le=99)
    service_rules: list[CouponServiceRuleIn] = Field(default_factory=list)
    city_rules: list[CouponCityRuleIn] = Field(default_factory=list)
    customer_mobiles: list[str] = Field(default_factory=list)
    partner_ids: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_definition(self):
        self.coupon_code = self.coupon_code.strip().upper()
        if len(self.coupon_code) < 3 or not self.title.strip():
            raise ValueError("Coupon code and title cannot be blank")
        if self.valid_to < self.valid_from:
            raise ValueError("End date cannot precede start date")
        if self.discount_type == "PERCENTAGE" and self.discount_value > 100:
            raise ValueError("Percentage discount cannot exceed 100")
        return self


class CouponUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = None
    discount_value: Decimal | None = Field(None, gt=0, decimal_places=2)
    max_discount_amount: Decimal | None = Field(None, ge=0, decimal_places=2)
    min_booking_amount: Decimal | None = Field(None, ge=0, decimal_places=2)
    max_usage_total: int | None = Field(None, gt=0)
    max_usage_per_customer: int | None = Field(None, gt=0)
    valid_from: date | None = None
    valid_to: date | None = None
    is_active: bool | None = None
    is_customer_specific: bool | None = None
    is_city_specific: bool | None = None
    coupon_type: CouponType | None = None
    platform_share_percent: int | None = Field(None, ge=1, le=99)
    service_rules: list[CouponServiceRuleIn] | None = None
    city_rules: list[CouponCityRuleIn] | None = None
    customer_mobiles: list[str] | None = None
    partner_ids: list[int] | None = None


class CouponServiceRuleOut(CouponServiceRuleIn):
    model_config = ConfigDict(from_attributes=True)
    id: int


class CouponCityRuleOut(CouponCityRuleIn):
    model_config = ConfigDict(from_attributes=True)
    id: int


class CouponCustomerRuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    mobile_number: str
    customer_id: int | None


class CouponPartnerRuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    partner_id: int
    joined_at: datetime
    is_active: bool


class CouponOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    coupon_code: str
    title: str
    description: str | None
    apply_to: str
    discount_type: str
    discount_value: Decimal
    max_discount_amount: Decimal | None
    min_booking_amount: Decimal
    max_usage_total: int | None
    max_usage_per_customer: int
    current_usage_count: int
    valid_from: date
    valid_to: date
    is_customer_specific: bool
    is_city_specific: bool
    is_active: bool
    coupon_type: CouponType
    platform_share_percent: int | None
    created_at: datetime
    updated_at: datetime
    service_rules: list[CouponServiceRuleOut]
    city_rules: list[CouponCityRuleOut]
    customer_rules: list[CouponCustomerRuleOut]
    partner_rules: list[CouponPartnerRuleOut]


class ValidateRequest(BaseModel):
    coupon_code: str = Field(min_length=1, max_length=50)
    service_type: ServiceType
    vehicle_category_id: int | None = Field(None, gt=0)
    city_id: int | None = Field(None, gt=0)
    booking_amount: Decimal = Field(ge=0, decimal_places=2, max_digits=12)
    customer_id: int = Field(ge=0)
    customer_mobile: str | None = None
    partner_id: int | None = Field(None, gt=0)
    pickup_datetime: datetime | None = None
    return_datetime: datetime | None = None


class ValidateResponse(BaseModel):
    valid: bool
    discount_amount: Decimal = Decimal("0")
    final_amount: Decimal = Decimal("0")
    message: str
    coupon_id: int | None = None
    coupon_type: CouponType = "PLATFORM"
    platform_share: Decimal = Decimal("0")
    partner_share: Decimal = Decimal("0")
    eligible_partner_ids: list[int] = Field(default_factory=list)


def _validate_type_fields(
    coupon_type, platform_share_percent, partner_ids, *, is_active=True
):
    if coupon_type not in COUPON_TYPES:
        raise ValidationException("Invalid coupon funding type")
    if coupon_type == "PLATFORM":
        if partner_ids:
            raise ValidationException("Platform coupons do not have joined partners")
        return None
    if is_active and not partner_ids:
        raise ValidationException(
            "Join at least one partner before activating this coupon"
        )
    if coupon_type == "PARTNER_ONLY":
        return None
    if platform_share_percent is None or not 1 <= platform_share_percent <= 99:
        raise ValidationException("Platform share must be between 1 and 99 percent")
    return platform_share_percent


def _split_shares(coupon_type, discount: Decimal, platform_share_percent):
    discount = discount.quantize(CENT, rounding=ROUND_HALF_UP)
    if coupon_type == "PLATFORM":
        return discount, Decimal("0.00")
    if coupon_type == "PARTNER_ONLY":
        return Decimal("0.00"), discount
    if coupon_type != "PLATFORM_PARTNER" or platform_share_percent is None:
        raise ValidationException("Coupon funding configuration is incomplete")
    platform = (discount * Decimal(platform_share_percent) / 100).quantize(
        CENT, rounding=ROUND_HALF_UP
    )
    return platform, discount - platform


def _coupon_query():
    return (
        select(Coupon)
        .options(
            selectinload(Coupon.service_rules),
            selectinload(Coupon.city_rules),
            selectinload(Coupon.customer_rules),
            selectinload(Coupon.partner_rules),
        )
        .execution_options(populate_existing=True)
    )


async def _load_coupon(db, coupon_id, *, lock=False):
    query = _coupon_query().where(Coupon.id == coupon_id)
    if lock:
        query = query.with_for_update()
    coupon = (await db.execute(query)).scalar_one_or_none()
    if coupon is None:
        raise ResourceNotFoundException("Coupon", coupon_id)
    return coupon


async def _save_relations(
    db, coupon, service_rules=None, city_rules=None, customer_mobiles=None
):
    if service_rules is not None:
        await db.execute(
            delete(CouponServiceRule).where(CouponServiceRule.coupon_id == coupon.id)
        )
        for rule in service_rules:
            db.add(CouponServiceRule(coupon_id=coupon.id, **rule.model_dump()))
    if city_rules is not None:
        await db.execute(
            delete(CouponCityRule).where(CouponCityRule.coupon_id == coupon.id)
        )
        for rule in city_rules:
            db.add(CouponCityRule(coupon_id=coupon.id, **rule.model_dump()))
    if customer_mobiles is not None:
        await db.execute(
            delete(CouponCustomerRule).where(CouponCustomerRule.coupon_id == coupon.id)
        )
        for mobile in sorted({m.strip() for m in customer_mobiles if m.strip()}):
            db.add(CouponCustomerRule(coupon_id=coupon.id, mobile_number=mobile))


async def _save_partner_rules(db, coupon, partner_ids, created_by):
    ids = set(partner_ids)
    existing = {r.partner_id: r for r in coupon.partner_rules}
    if {pid for pid, r in existing.items() if r.is_active} - ids:
        raise ValidationException(
            "Use the audited partner removal action; editing cannot remove participants"
        )
    if not ids:
        return
    eligible = set(
        (
            await db.execute(
                text(
                    """
        SELECT p.id FROM partners p WHERE p.id = ANY(CAST(:ids AS BIGINT[]))
          AND p.status IN ('APPROVED','ACTIVE') AND EXISTS (
            SELECT 1 FROM partner_services ps WHERE ps.partner_id = p.id AND ps.is_active
            AND (:scope = 'ALL' OR ps.service_type = :scope)
            AND ps.service_type IN ('CAB','HOTEL','TOUR'))
    """
                ),
                {"ids": sorted(ids), "scope": coupon.apply_to},
            )
        )
        .scalars()
        .all()
    )
    if ids != eligible:
        raise ValidationException(
            "Joined partners must be active and offer the selected service"
        )
    for pid in sorted(ids):
        if pid in existing:
            if not existing[pid].is_active:
                raise ValidationException(
                    "A removed participant cannot be silently re-enrolled"
                )
        else:
            db.add(
                CouponPartnerRule(
                    coupon_id=coupon.id, partner_id=pid, created_by=created_by
                )
            )


@router.get("", response_model=list[CouponOut], tags=["Coupons"])
async def list_coupons(
    is_active: bool | None = None,
    apply_to: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    q = _coupon_query().order_by(Coupon.created_at.desc())
    if is_active is not None:
        q = q.where(Coupon.is_active == is_active)
    if apply_to:
        q = q.where(Coupon.apply_to == apply_to.upper())
    return (await db.execute(q)).scalars().all()


@router.post("", response_model=CouponOut, status_code=201, tags=["Coupons"])
async def create_coupon(
    payload: CouponCreate,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_roles("ADMIN", "SUPER_ADMIN")),
):
    if (
        await db.execute(
            select(Coupon.id).where(Coupon.coupon_code == payload.coupon_code)
        )
    ).scalar_one_or_none():
        raise ValidationException("Coupon code already exists")
    pct = _validate_type_fields(
        payload.coupon_type, payload.platform_share_percent, payload.partner_ids
    )
    data = payload.model_dump(
        exclude={"service_rules", "city_rules", "customer_mobiles", "partner_ids"}
    )
    data["platform_share_percent"] = pct
    coupon = Coupon(**data, created_by=str(current_user["id"]), partner_rules=[])
    db.add(coupon)
    await db.flush()
    await _save_relations(
        db, coupon, payload.service_rules, payload.city_rules, payload.customer_mobiles
    )
    await _save_partner_rules(db, coupon, payload.partner_ids, str(current_user["id"]))
    await db.flush()
    return await _load_coupon(db, coupon.id)


@router.get("/{coupon_id}", response_model=CouponOut, tags=["Coupons"])
async def get_coupon(coupon_id: int, db: AsyncSession = Depends(get_db)):
    return await _load_coupon(db, coupon_id)


@router.put("/{coupon_id}", response_model=CouponOut, tags=["Coupons"])
async def update_coupon(
    coupon_id: int,
    payload: CouponUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_roles("ADMIN", "SUPER_ADMIN")),
):
    coupon = await _load_coupon(db, coupon_id, lock=True)
    ctype = payload.coupon_type or coupon.coupon_type
    ids = (
        payload.partner_ids
        if payload.partner_ids is not None
        else [r.partner_id for r in coupon.partner_rules if r.is_active]
    )
    pct = (
        payload.platform_share_percent
        if "platform_share_percent" in payload.model_fields_set
        else coupon.platform_share_percent
    )
    active = payload.is_active if payload.is_active is not None else coupon.is_active
    pct = _validate_type_fields(ctype, pct, ids, is_active=active)
    changes = payload.model_dump(
        exclude_unset=True,
        exclude={
            "coupon_type",
            "platform_share_percent",
            "partner_ids",
            "service_rules",
            "city_rules",
            "customer_mobiles",
        },
    )
    nullable = {"description", "max_discount_amount", "max_usage_total"}
    if any(value is None and key not in nullable for key, value in changes.items()):
        raise ValidationException("Required coupon fields cannot be null")
    if changes.get("valid_to", coupon.valid_to) < changes.get(
        "valid_from", coupon.valid_from
    ):
        raise ValidationException("End date cannot precede start date")
    if (
        coupon.discount_type == "PERCENTAGE"
        and changes.get("discount_value", coupon.discount_value) > 100
    ):
        raise ValidationException("Percentage discount cannot exceed 100")
    for field, value in changes.items():
        setattr(coupon, field, value)
    coupon.coupon_type, coupon.platform_share_percent = ctype, pct
    await _save_relations(
        db, coupon, payload.service_rules, payload.city_rules, payload.customer_mobiles
    )
    if payload.partner_ids is not None:
        await _save_partner_rules(db, coupon, ids, str(current_user["id"]))
    await db.flush()
    return await _load_coupon(db, coupon.id)


@router.delete("/{coupon_id}", status_code=204, tags=["Coupons"])
async def delete_coupon(coupon_id: int, db: AsyncSession = Depends(get_db)):
    coupon = await _load_coupon(db, coupon_id, lock=True)
    if (
        await db.execute(
            select(CouponUsage.id).where(CouponUsage.coupon_id == coupon_id).limit(1)
        )
    ).first():
        raise ValidationException(
            "Deactivate coupons with redemption history instead of deleting them"
        )
    await db.delete(coupon)


async def validate_coupon(
    payload: ValidateRequest, db: AsyncSession, *, reserve: bool = False
):
    q = _coupon_query().where(Coupon.coupon_code == payload.coupon_code.strip().upper())
    if reserve:
        q = q.with_for_update()
    coupon = (await db.execute(q)).scalar_one_or_none()

    def fail(message):
        return ValidateResponse(
            valid=False, message=message, final_amount=payload.booking_amount
        )

    if coupon is None:
        return fail("Invalid coupon code")
    if not coupon.is_active:
        return fail("This coupon is inactive")
    if not coupon.valid_from <= date.today() <= coupon.valid_to:
        return fail("Coupon has expired or is not yet valid")
    if payload.booking_amount < coupon.min_booking_amount:
        return fail(f"Minimum booking amount ₹{coupon.min_booking_amount} required")
    if (
        coupon.max_usage_total is not None
        and coupon.current_usage_count >= coupon.max_usage_total
    ):
        return fail("Coupon usage limit reached")
    if payload.customer_id:
        used = (
            await db.execute(
                select(func.count(CouponUsage.id)).where(
                    CouponUsage.coupon_id == coupon.id,
                    CouponUsage.customer_id == payload.customer_id,
                )
            )
        ).scalar_one()
        if used >= coupon.max_usage_per_customer:
            return fail("You have already used this coupon the maximum number of times")
    if coupon.is_customer_specific and payload.customer_mobile not in {
        r.mobile_number for r in coupon.customer_rules
    }:
        return fail("This coupon is not applicable to your account")
    if coupon.apply_to not in ("ALL", payload.service_type):
        return fail("Coupon not valid for this service")
    if coupon.service_rules and not any(
        r.service_type == payload.service_type
        and (
            r.vehicle_category_id is None
            or r.vehicle_category_id == payload.vehicle_category_id
        )
        for r in coupon.service_rules
    ):
        return fail("Coupon not valid for the selected service/category")
    cap = None
    if coupon.is_city_specific:
        city = next(
            (r for r in coupon.city_rules if r.city_id == payload.city_id), None
        )
        if city is None:
            return fail("Coupon not valid in your city")
        cap = city.max_discount_override
    cohort = []
    if coupon.coupon_type in PARTNER_FUNDED_TYPES:
        joined = sorted(r.partner_id for r in coupon.partner_rules if r.is_active)
        if not joined:
            return fail("This coupon has no active participants")
        if payload.service_type == "CAB":
            if (
                not payload.city_id
                or not payload.vehicle_category_id
                or payload.pickup_datetime is None
            ):
                return fail(
                    "Select city, cab category and pickup time before applying this coupon"
                )
            if reserve:
                await db.execute(
                    text(
                        "SELECT id FROM partners WHERE id = ANY(CAST(:ids AS BIGINT[])) ORDER BY id FOR UPDATE"
                    ),
                    {"ids": joined},
                )
            counts = await available_cab_counts(
                db,
                joined,
                payload.city_id,
                payload.vehicle_category_id,
                payload.pickup_datetime,
                payload.return_datetime,
            )
            cohort = [pid for pid in joined if counts.get(pid, 0) > 0]
            if not cohort:
                return fail("No participating partner has cab capacity for this trip")
        else:
            if payload.partner_id not in joined:
                return fail("This coupon is not available for this listing's partner")
            eligible = (
                await db.execute(
                    text(
                        """
                SELECT p.id FROM partners p JOIN partner_services ps ON ps.partner_id = p.id
                WHERE p.id = :pid AND p.status IN ('APPROVED','ACTIVE')
                  AND ps.service_type = :svc AND ps.is_active
            """
                    ),
                    {"pid": payload.partner_id, "svc": payload.service_type},
                )
            ).first()
            if not eligible:
                return fail(
                    "The participating partner is not available for this service"
                )
    discount = (
        coupon.discount_value
        if coupon.discount_type == "FLAT"
        else (payload.booking_amount * coupon.discount_value / 100).quantize(
            CENT, rounding=ROUND_HALF_UP
        )
    )
    if coupon.max_discount_amount is not None:
        discount = min(discount, coupon.max_discount_amount)
    if cap is not None:
        discount = min(discount, cap)
    discount = min(discount, payload.booking_amount).quantize(
        CENT, rounding=ROUND_HALF_UP
    )
    platform, partner = _split_shares(
        coupon.coupon_type, discount, coupon.platform_share_percent
    )
    return ValidateResponse(
        valid=True,
        coupon_id=coupon.id,
        discount_amount=discount,
        final_amount=payload.booking_amount - discount,
        message=f"Coupon applied! You save ₹{discount}",
        coupon_type=coupon.coupon_type,
        platform_share=platform,
        partner_share=partner,
        eligible_partner_ids=cohort,
    )


@router.post("/validate", response_model=ValidateResponse, tags=["Coupons"])
async def validate_coupon_endpoint(
    payload: ValidateRequest, db: AsyncSession = Depends(get_db)
):
    return await validate_coupon(payload, db)


async def record_coupon_usage(
    db,
    *,
    validation: ValidateResponse,
    customer_id: int,
    master_booking_id: int,
    cab_booking_id=None,
    hotel_reservation_id=None,
    tour_booking_id=None,
):
    if not validation.valid or validation.coupon_id is None:
        raise ValidationException("A valid coupon redemption is required")
    if (
        sum(
            v is not None
            for v in (cab_booking_id, hotel_reservation_id, tour_booking_id)
        )
        != 1
    ):
        raise ValidationException(
            "Coupon redemption requires exactly one service booking"
        )
    db.add(
        CouponUsage(
            coupon_id=validation.coupon_id,
            customer_id=customer_id,
            master_booking_id=master_booking_id,
            cab_booking_id=cab_booking_id,
            hotel_reservation_id=hotel_reservation_id,
            tour_booking_id=tour_booking_id,
            discount_applied=validation.discount_amount,
            coupon_type=validation.coupon_type,
            platform_share=validation.platform_share,
            partner_share=validation.partner_share,
            eligible_partner_ids=validation.eligible_partner_ids,
        )
    )
    await db.execute(
        text(
            "UPDATE coupons SET current_usage_count = current_usage_count + 1, updated_at = NOW() WHERE id = :id"
        ),
        {"id": validation.coupon_id},
    )
    await db.flush()


@router.get("/{coupon_id}/usages", tags=["Coupons"])
async def coupon_usages(
    coupon_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    await _load_coupon(db, coupon_id)
    total = (
        await db.execute(
            select(func.count(CouponUsage.id)).where(CouponUsage.coupon_id == coupon_id)
        )
    ).scalar_one()
    rows = (
        (
            await db.execute(
                select(CouponUsage)
                .where(CouponUsage.coupon_id == coupon_id)
                .order_by(CouponUsage.used_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": math.ceil(total / page_size) or 1,
        "items": [
            {
                "id": u.id,
                "customer_id": u.customer_id,
                "master_booking_id": u.master_booking_id,
                "discount_applied": u.discount_applied,
                "coupon_type": u.coupon_type,
                "platform_share": u.platform_share,
                "partner_share": u.partner_share,
                "used_at": u.used_at,
            }
            for u in rows
        ],
    }


async def _partner_response_rows(db, coupon_id):
    return (
        (
            await db.execute(
                text(
                    """
        SELECT a.partner_id, a.id, a.accepted_at, a.rejected_at, a.rejection_reason_code,
               a.available_cabs_at_decline, cb.booking_number
        FROM cab_booking_assignments a JOIN coupon_usages cu ON cu.cab_booking_id = a.cab_booking_id
        JOIN cab_bookings cb ON cb.id = a.cab_booking_id
        WHERE cu.coupon_id = :cid AND cu.coupon_type IN ('PARTNER_ONLY','PLATFORM_PARTNER')
        ORDER BY a.assigned_at DESC
    """
                ),
                {"cid": coupon_id},
            )
        )
        .mappings()
        .all()
    )


def _is_proven_refusal(row):
    return (
        row["rejected_at"] is not None
        and row["rejection_reason_code"] not in (None, "TIMEOUT", "ADMIN_REASSIGN")
        and (row["available_cabs_at_decline"] or 0) > 0
    )


@router.get("/{coupon_id}/partners", tags=["Coupons"])
async def coupon_partners(coupon_id: int, db: AsyncSession = Depends(get_db)):
    coupon = await _load_coupon(db, coupon_id)
    names = (
        (
            await db.execute(
                text(
                    """
        SELECT p.id, p.business_name, p.status FROM partners p JOIN coupon_partner_rules cpr
        ON cpr.partner_id = p.id WHERE cpr.coupon_id = :cid
    """
                ),
                {"cid": coupon_id},
            )
        )
        .mappings()
        .all()
    )
    names = {row["id"]: row for row in names}
    responses = await _partner_response_rows(db, coupon_id)
    items = []
    for rule in coupon.partner_rules:
        history = [r for r in responses if r["partner_id"] == rule.partner_id]
        declines = [
            r
            for r in history
            if r["rejected_at"] is not None
            and r["rejection_reason_code"] != "ADMIN_REASSIGN"
        ]
        proven = sum(_is_proven_refusal(r) for r in history)
        partner = names[rule.partner_id]
        items.append(
            {
                "partner_id": rule.partner_id,
                "business_name": partner["business_name"],
                "partner_status": partner["status"],
                "joined_at": rule.joined_at,
                "is_active": rule.is_active,
                "offered_count": len(history),
                "accepted_count": sum(r["accepted_at"] is not None for r in history),
                "declined_count": len(declines),
                "declined_with_cabs": proven,
                "can_remove": rule.is_active and proven > 0,
                "declines": [
                    {
                        "booking_number": r["booking_number"],
                        "rejected_at": r["rejected_at"],
                        "reason_code": r["rejection_reason_code"],
                        "available_cabs_at_decline": r["available_cabs_at_decline"],
                    }
                    for r in declines
                ],
            }
        )
    return {"coupon_id": coupon_id, "coupon_type": coupon.coupon_type, "items": items}


@router.delete("/{coupon_id}/partners/{partner_id}", tags=["Coupons"])
async def remove_coupon_partner(
    coupon_id: int,
    partner_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_roles("ADMIN", "SUPER_ADMIN")),
):
    coupon = await _load_coupon(db, coupon_id, lock=True)
    rule = next(
        (r for r in coupon.partner_rules if r.partner_id == partner_id and r.is_active),
        None,
    )
    if rule is None:
        raise ResourceNotFoundException("Active coupon participant", partner_id)
    rows = await _partner_response_rows(db, coupon_id)
    if not any(r["partner_id"] == partner_id and _is_proven_refusal(r) for r in rows):
        raise ValidationException(
            "Removal requires a recorded refusal with available cabs; unavailability and timeouts are not grounds"
        )
    rule.is_active = False
    rule.removed_at = datetime.now(timezone.utc)
    rule.removed_by = str(current_user["id"])
    if not any(r.is_active for r in coupon.partner_rules):
        coupon.is_active = False
    await db.flush()
    return {"success": True, "coupon_id": coupon_id, "partner_id": partner_id}
