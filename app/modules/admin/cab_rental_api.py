# ============================================================
# WAYTERO — ADMIN CAB RATE CARD & CUSTOMER PRICE OVERRIDES
# File: app/modules/admin/cab_rental_api.py
# Prefix: /admin/customer-care  (registered in api/router.py)
# Doc Ref: Docs/01_Business_Requirements/SUPPLIER_CAB_PRICE_MASTER_V1.md §1, §3
#          BRD Part 3 §36 — city-based pricing; migration 0098
#
# Three things the desk needs that did not exist before:
#
#   GET  /cab-booking/rate-card                what a class costs, and what this
#                                              customer has been promised instead
#   POST /cab-booking/rental-quote             the package price, both sides
#   CRUD /cab-booking/custom-quotes            the promise itself
#   PATCH /cab-booking/rate-card/{rate_id}     correct a card line
#
# The quote endpoint returns the card price and the customer price side by side.
# Nothing here computes money in the client: the rate card is the one source,
# and an override is an explicit delta an admin can see and revoke.
# ============================================================

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.branch_scope import BranchScope, get_branch_scope
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.exceptions import ResourceNotFoundException, ValidationException
from app.modules.admin.cab_route_service import assert_city_active
from app.modules.admin.customer_care_api import _desk_customer, _desk_pricing_city
from app.modules.auth.models.user import User
from app.modules.booking.services.rental_booking import (
    RENTAL_TYPE_ORDER,
    RentalPackageQuote,
    load_custom_quote,
    load_rate_card,
    quote_rental_package,
)

router = APIRouter()


# ── Schemas ──────────────────────────────────────────────────


class RentalQuoteRequest(BaseModel):
    """Everything the desk knows about the trip before it has a price."""

    city_id: Optional[int] = None
    customer_id: Optional[int] = None
    vehicle_class: str = Field(..., min_length=1, max_length=40)
    rental_type: str = Field(..., description="LOCAL | OUTSTATION | DAILY")
    # The duty the customer described. LOCAL/OUTSTATION read both; DAILY reads
    # days and treats hours/km as overflow beyond the per-day allowance.
    hours: float = Field(default=0, ge=0, le=24 * 30)
    km: float = Field(default=0, ge=0, le=20000)
    days: int = Field(default=1, ge=1, le=30)
    returned_after_midnight: bool = False
    pickup_datetime: Optional[datetime] = None


class CustomQuoteCreate(BaseModel):
    """A negotiated price for one customer, one package.

    All three money fields are optional but at least one is required: an
    override that overlaps nothing would tell the desk a customer has a special
    price while quoting them the card.
    """

    customer_id: int = Field(..., gt=0)
    city_id: Optional[int] = None
    vehicle_class: str = Field(..., min_length=1, max_length=40)
    rental_type: str = Field(..., description="LOCAL | OUTSTATION | DAILY")
    included_hours: int = Field(..., ge=1, le=720)
    package_price: Optional[Decimal] = Field(None, ge=0)
    extra_hour_rate: Optional[Decimal] = Field(None, ge=0)
    extra_km_rate: Optional[Decimal] = Field(None, ge=0)
    valid_from: Optional[date] = None
    valid_to: Optional[date] = None
    reason: Optional[str] = Field(None, max_length=500)


class CustomQuoteUpdate(BaseModel):
    package_price: Optional[Decimal] = Field(None, ge=0)
    extra_hour_rate: Optional[Decimal] = Field(None, ge=0)
    extra_km_rate: Optional[Decimal] = Field(None, ge=0)
    valid_from: Optional[date] = None
    valid_to: Optional[date] = None
    reason: Optional[str] = Field(None, max_length=500)
    is_active: Optional[bool] = None


class RateCardUpdate(BaseModel):
    """Correct one line of the card. Only the columns an admin actually edits."""

    price: Optional[Decimal] = Field(None, ge=0)
    extra_hour_rate: Optional[Decimal] = Field(None, ge=0)
    extra_km_rate: Optional[Decimal] = Field(None, ge=0)
    driver_allowance: Optional[Decimal] = Field(None, ge=0)
    midnight_return_charge: Optional[Decimal] = Field(None, ge=0)
    vehicle_category_id: Optional[int] = Field(None, gt=0)
    is_active: Optional[bool] = None


# ── Serialisation ────────────────────────────────────────────


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def _side(q) -> dict:
    return {
        "package_price": str(_money(q.package_price)),
        "extra_hours": str(q.extra_hours),
        "extra_hour_rate": str(_money(q.extra_hour_rate)),
        "extra_hour_charge": str(_money(q.extra_hour_charge)),
        "extra_km": str(q.extra_km),
        "extra_km_rate": str(_money(q.extra_km_rate)),
        "extra_km_charge": str(_money(q.extra_km_charge)),
        "driver_allowance": str(_money(q.driver_allowance)),
        "midnight_charge": str(_money(q.midnight_charge)),
        "total": str(_money(q.total)),
        "toll_included": q.toll_included,
        "parking_included": q.parking_included,
        "night_halt_included": q.night_halt_included,
    }


def _lines(quote: RentalPackageQuote) -> list[dict]:
    """The money panel, card and customer side by side.

    Every line is returned even at zero for the card side, so the agent can see
    which lines the override touched rather than guessing from a shorter list.
    """
    std, app = quote.standard, quote.applied
    rows = [
        (
            "Package price",
            _money(std.package_price),
            _money(app.package_price),
        ),
        (
            f"Extra hours ({app.extra_hours:g}h)",
            _money(std.extra_hour_charge),
            _money(app.extra_hour_charge),
        ),
        (
            f"Extra km ({app.extra_km:g}km)",
            _money(std.extra_km_charge),
            _money(app.extra_km_charge),
        ),
        (
            "Driver allowance",
            _money(std.driver_allowance),
            _money(app.driver_allowance),
        ),
        (
            "Midnight return",
            _money(std.midnight_charge),
            _money(app.midnight_charge),
        ),
        ("Total", _money(std.total), _money(app.total)),
    ]
    return [
        {
            "label": label,
            "standard": str(card),
            "applied": str(applied),
            "changed": card != applied,
        }
        for label, card, applied in rows
    ]


def _quote_out(quote: RentalPackageQuote) -> dict:
    return {
        "city_id": quote.city_id,
        "vehicle_class": quote.vehicle_class,
        "vehicle_label": quote.vehicle_label,
        "vehicle_category_id": quote.vehicle_category_id,
        "seating_capacity": quote.seating_capacity,
        "rental_type": quote.rental_type,
        "rate_source": quote.rate_source,
        "hours": str(quote.hours),
        "km": str(quote.km),
        "days": quote.days,
        "returned_after_midnight": quote.returned_after_midnight,
        "package": {
            "included_hours": quote.applied.package_hours,
            "included_km": quote.applied.package_km,
            "days": quote.applied.days,
        },
        "standard": _side(quote.standard),
        "applied": _side(quote.applied),
        "lines": _lines(quote),
        "difference": str(_money(quote.difference)),
        "custom_quote": quote.custom_quote.as_dict() if quote.custom_quote else None,
        "custom_applied": quote.custom_applied,
        "package_options": quote.package_options,
        "validations": [v.as_dict() for v in quote.validations],
        "can_book": not quote.errors,
        "total": str(_money(quote.total)),
    }


# ── Rate card ────────────────────────────────────────────────


@router.get("/cab-booking/rate-card", tags=["Customer Care"])
async def cab_rate_card(
    city_id: Optional[int] = Query(None),
    customer_id: Optional[int] = Query(None),
    for_date: Optional[date] = Query(None),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
    scope: BranchScope = Depends(get_branch_scope),
):
    """The card the desk quotes from, per class and rental type.

    When `customer_id` is supplied, each package also says whether this customer
    has a negotiated price for it — with the override attached, in force or not,
    so the screen can show "your price" beside the card and grey out a lapsed one.
    """
    effective_city_id = scope.city_id if scope.restricted else city_id
    if scope.restricted or effective_city_id:
        await assert_city_active(db, effective_city_id, "Rate card city")

    by_class, source = await load_rate_card(db, effective_city_id)
    on_date = for_date or date.today()

    custom_rows: dict[tuple, dict] = {}
    if customer_id:
        await _desk_customer(db, customer_id, scope)
        result = await db.execute(
            text(
                """
                SELECT id, customer_id, city_id, vehicle_class, rental_type,
                       included_hours, package_price, extra_hour_rate, extra_km_rate,
                       valid_from, valid_to, reason, is_active
                FROM cab_custom_quotes
                WHERE customer_id = :customer_id AND is_active = TRUE
                  AND (city_id IS NULL OR city_id = :city_id)
                ORDER BY city_id NULLS LAST
                """
            ),
            {"customer_id": customer_id, "city_id": effective_city_id},
        )
        for row in result.mappings().all():
            data = dict(row)
            # City-specific row wins: a later all-city row must not overwrite it.
            key = (
                data["vehicle_class"],
                data["rental_type"],
                data["included_hours"],
            )
            custom_rows.setdefault(key, data)

    classes = []
    for vehicle_class, rows in sorted(by_class.items()):
        sample = rows[0]
        packages = []
        for row in sorted(rows, key=lambda r: (r["rental_type"], r["included_hours"])):
            override = custom_rows.get(
                (vehicle_class, row["rental_type"], row["included_hours"])
            )
            applies = False
            if override is not None:
                applies = (
                    override["valid_from"] is None or override["valid_from"] <= on_date
                ) and (override["valid_to"] is None or override["valid_to"] >= on_date)
            packages.append(
                {
                    "rate_id": row["id"],
                    "rental_type": row["rental_type"],
                    "included_hours": row["included_hours"],
                    "included_km": row["included_km"],
                    "price": str(_money(row["price"])),
                    "extra_hour_rate": str(_money(row["extra_hour_rate"])),
                    "extra_km_rate": str(_money(row["extra_km_rate"])),
                    "driver_allowance": str(_money(row["driver_allowance"])),
                    "midnight_return_charge": str(
                        _money(row["midnight_return_charge"])
                    ),
                    "toll_included": row["toll_included"],
                    "parking_included": row["parking_included"],
                    "night_halt_included": row["night_halt_included"],
                    "effective_from": (
                        row["effective_from"].isoformat()
                        if row["effective_from"]
                        else None
                    ),
                    "custom": (
                        {
                            **{
                                k: (
                                    str(v)
                                    if isinstance(v, Decimal)
                                    else v.isoformat() if isinstance(v, date) else v
                                )
                                for k, v in override.items()
                            },
                            "applies": applies,
                        }
                        if override
                        else None
                    ),
                }
            )
        classes.append(
            {
                "vehicle_class": vehicle_class,
                "label": sample["vehicle_label"],
                "models": sample["vehicle_models"],
                "seating_capacity": sample["seating_capacity"],
                "vehicle_category_id": sample["vehicle_category_id"],
                "rate_source": source.get(vehicle_class, "PLATFORM"),
                "packages": packages,
            }
        )

    return {
        "city_id": effective_city_id,
        "effective_date": on_date.isoformat(),
        "rental_types": list(RENTAL_TYPE_ORDER),
        "classes": classes,
    }


@router.patch("/cab-booking/rate-card/{rate_id}", tags=["Customer Care"])
async def update_rate_card_line(
    rate_id: int,
    payload: RateCardUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
    scope: BranchScope = Depends(get_branch_scope),
):
    """Correct a card line in place.

    A branch edits its own city's sheet; the platform default is only editable
    by an admin whose scope is not a single city, because changing it reprices
    every city that has not set its own. Nothing is written for a line outside
    the branch's scope — it reads as absent.
    """
    row = (
        (
            await db.execute(
                text(
                    """
                SELECT id, city_id, vehicle_class, rental_type, included_hours,
                       price, extra_hour_rate, extra_km_rate, driver_allowance,
                       midnight_return_charge, vehicle_category_id, is_active
                FROM cab_rental_rates WHERE id = :id
                """
                ),
                {"id": rate_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise ResourceNotFoundException("Rate card line not found")

    if scope.restricted:
        if row["city_id"] != scope.city_id:
            raise ResourceNotFoundException("Rate card line not found")
    # Unrestricted admins may edit the platform default as well as any city.

    if payload.vehicle_category_id is not None:
        exists = (
            await db.execute(
                text("SELECT 1 FROM vehicle_categories WHERE id = :id AND is_active"),
                {"id": payload.vehicle_category_id},
            )
        ).scalar()
        if not exists:
            raise ValidationException("That vehicle category is not active.")

    updates = payload.model_dump(exclude_none=True)
    if not updates:
        raise ValidationException("Nothing to update.")

    assignments = ", ".join(f"{column} = :{column}" for column in updates)
    await db.execute(
        text(
            f"UPDATE cab_rental_rates SET {assignments}, updated_at = NOW() "
            "WHERE id = :id"
        ),
        {**updates, "id": rate_id},
    )

    return {
        "success": True,
        "message": (
            f"{row['vehicle_class']} {row['rental_type']} "
            f"{row['included_hours']}h updated."
        ),
        "data": {"rate_id": rate_id, **{k: str(v) for k, v in updates.items()}},
    }


# ── Package quote ────────────────────────────────────────────


@router.post("/cab-booking/rental-quote", tags=["Customer Care"])
async def quote_rental_booking(
    payload: RentalQuoteRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
    scope: BranchScope = Depends(get_branch_scope),
):
    """Price a package trip, and say what the customer's own price would be.

    The same call the create endpoint makes. Nothing is charged or reserved here,
    so a stale quote cannot be committed by posting it back.
    """
    city_id = await _desk_pricing_city(payload, db, scope)
    if payload.customer_id:
        await _desk_customer(db, payload.customer_id, scope)

    quote = await quote_rental_package(
        db,
        city_id=city_id,
        vehicle_class=payload.vehicle_class,
        rental_type=payload.rental_type,
        hours=payload.hours,
        km=payload.km,
        days=payload.days,
        returned_after_midnight=payload.returned_after_midnight,
        customer_id=payload.customer_id,
        pickup_datetime=payload.pickup_datetime,
    )
    return _quote_out(quote)


# ── Customer price overrides ─────────────────────────────────


def _custom_quote_out(row: dict) -> dict:
    return {
        key: (
            str(value)
            if isinstance(value, Decimal)
            else value.isoformat() if isinstance(value, (date, datetime)) else value
        )
        for key, value in row.items()
    }


@router.get("/cab-booking/custom-quotes", tags=["Customer Care"])
async def list_custom_quotes(
    customer_id: Optional[int] = Query(None),
    city_id: Optional[int] = Query(None),
    include_inactive: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
    scope: BranchScope = Depends(get_branch_scope),
):
    """Negotiated prices, newest first.

    Without `customer_id` this is the manager's view: everything in the branch's
    cities plus the all-city rows, so an override cannot be invisible to the
    branch that honours it.
    """
    clauses = []
    params: dict[str, Any] = {}
    # Every column is qualified: `customers` has a city_id and an is_active of
    # its own, so a bare name here reads the customer's home city instead of the
    # override's and the query silently answers a different question.
    if customer_id:
        await _desk_customer(db, customer_id, scope)
        clauses.append("q.customer_id = :customer_id")
        params["customer_id"] = customer_id
    if scope.restricted:
        clauses.append("(q.city_id IS NULL OR q.city_id = :scope_city)")
        params["scope_city"] = scope.city_id
    if city_id:
        clauses.append("(q.city_id IS NULL OR q.city_id = :city_id)")
        params["city_id"] = city_id
    if not include_inactive:
        clauses.append("q.is_active = TRUE")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    result = await db.execute(
        text(
            f"""
            SELECT q.id, q.customer_id, q.city_id, q.vehicle_class, q.rental_type,
                   q.included_hours, q.package_price, q.extra_hour_rate,
                   q.extra_km_rate, q.valid_from, q.valid_to, q.reason,
                   q.is_active, q.created_at,
                   u.first_name || ' ' || COALESCE(u.last_name, '') AS customer_name,
                   u.mobile_number,
                   c.name AS city_name
            FROM cab_custom_quotes q
            JOIN customers cu ON cu.id = q.customer_id
            JOIN users u ON u.id = cu.user_id
            LEFT JOIN cities c ON c.id = q.city_id
            {where}
            ORDER BY q.created_at DESC, q.id DESC
            LIMIT 500
            """
        ),
        params,
    )
    return {"quotes": [_custom_quote_out(dict(r)) for r in result.mappings().all()]}


@router.post("/cab-booking/custom-quotes", tags=["Customer Care"])
async def create_custom_quote(
    payload: CustomQuoteCreate,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
    scope: BranchScope = Depends(get_branch_scope),
):
    """Agree a price for one customer's package.

    The class/package is checked against the card, because an override for a
    package nobody sells is a promise nobody can honour. A branch can only set a
    price in its own city — an all-city override from one branch would reprice
    trips another branch is accountable for.
    """
    if payload.rental_type.upper() not in RENTAL_TYPE_ORDER:
        raise ValidationException(
            f"Rental type must be one of {', '.join(RENTAL_TYPE_ORDER)}."
        )
    if (
        payload.package_price is None
        and payload.extra_hour_rate is None
        and payload.extra_km_rate is None
    ):
        raise ValidationException(
            "Set at least one price — the package, the extra hour rate or the "
            "extra km rate."
        )
    if (
        payload.valid_from
        and payload.valid_to
        and payload.valid_from > payload.valid_to
    ):
        raise ValidationException("The valid-from date is after the valid-to date.")

    await _desk_customer(db, payload.customer_id, scope)

    if scope.restricted:
        city_id = scope.city_id
    else:
        city_id = payload.city_id
        if city_id is not None:
            await assert_city_active(db, city_id, "Override city")

    rental_type = payload.rental_type.upper()
    card_exists = (
        await db.execute(
            text(
                """
                SELECT 1 FROM cab_rental_rates
                WHERE vehicle_class = :vehicle_class
                  AND rental_type = :rental_type
                  AND included_hours = :included_hours
                  AND is_active = TRUE
                  AND (city_id = :city_id OR city_id IS NULL)
                LIMIT 1
                """
            ),
            {
                "vehicle_class": payload.vehicle_class,
                "rental_type": rental_type,
                "included_hours": payload.included_hours,
                "city_id": city_id,
            },
        )
    ).scalar()
    if not card_exists:
        raise ValidationException(
            "That package is not on the rate card for this city, so there is no "
            "standard price for the agreed one to replace."
        )

    created_by = None
    try:
        import uuid as _uuid

        created_by = _uuid.UUID(current_user["sub"])
    except (KeyError, TypeError, ValueError):
        created_by = None

    row = (
        await db.execute(
            text(
                """
                INSERT INTO cab_custom_quotes (
                    customer_id, city_id, vehicle_class, rental_type,
                    included_hours, package_price, extra_hour_rate, extra_km_rate,
                    valid_from, valid_to, reason, created_by
                ) VALUES (
                    :customer_id, :city_id, :vehicle_class, :rental_type,
                    :included_hours, :package_price, :extra_hour_rate,
                    :extra_km_rate, :valid_from, :valid_to, :reason, :created_by
                )
                -- The partial unique index on the active override is the
                -- conflict target; a second active row for the same package
                -- is a mistake, not a second opinion.
                ON CONFLICT DO NOTHING
                RETURNING id
                """
            ),
            {
                "customer_id": payload.customer_id,
                "city_id": city_id,
                "vehicle_class": payload.vehicle_class,
                "rental_type": rental_type,
                "included_hours": payload.included_hours,
                "package_price": payload.package_price,
                "extra_hour_rate": payload.extra_hour_rate,
                "extra_km_rate": payload.extra_km_rate,
                "valid_from": payload.valid_from,
                "valid_to": payload.valid_to,
                "reason": payload.reason,
                "created_by": created_by,
            },
        )
    ).scalar_one_or_none()

    if row is None:
        raise ValidationException(
            "This customer already has an active agreed price for that package "
            "and city — edit that one instead of creating a second."
        )

    return {
        "success": True,
        "message": "Customer price saved.",
        "data": {"id": row},
    }


@router.patch("/cab-booking/custom-quotes/{quote_id}", tags=["Customer Care"])
async def update_custom_quote(
    quote_id: int,
    payload: CustomQuoteUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
    scope: BranchScope = Depends(get_branch_scope),
):
    """Change or revoke an agreed price.

    Revoking (`is_active=false`) keeps the row: the trips priced under it still
    have to be explainable, and an override that vanishes cannot be audited.
    """
    row = (
        (
            await db.execute(
                text(
                    """
                SELECT id, customer_id, city_id, package_price, extra_hour_rate,
                       extra_km_rate
                FROM cab_custom_quotes WHERE id = :id
                """
                ),
                {"id": quote_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise ResourceNotFoundException("Agreed price not found")
    if scope.restricted and row["city_id"] not in (None, scope.city_id):
        raise ResourceNotFoundException("Agreed price not found")

    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise ValidationException("Nothing to update.")

    merged = {**dict(row), **updates}
    if (
        merged.get("package_price") is None
        and merged.get("extra_hour_rate") is None
        and merged.get("extra_km_rate") is None
    ):
        raise ValidationException(
            "An agreed price needs at least one of the package, extra hour or "
            "extra km rate."
        )
    if (
        merged.get("valid_from")
        and merged.get("valid_to")
        and merged["valid_from"] > merged["valid_to"]
    ):
        raise ValidationException("The valid-from date is after the valid-to date.")

    assignments = ", ".join(f"{column} = :{column}" for column in updates)
    await db.execute(
        text(
            f"UPDATE cab_custom_quotes SET {assignments}, updated_at = NOW() "
            "WHERE id = :id"
        ),
        {**updates, "id": quote_id},
    )
    return {
        "success": True,
        "message": (
            "Agreed price updated."
            if updates.get("is_active", True)
            else "Agreed price revoked."
        ),
        "data": {"id": quote_id},
    }


@router.get("/cab-booking/custom-quote", tags=["Customer Care"])
async def effective_custom_quote(
    customer_id: int,
    city_id: Optional[int] = Query(None),
    vehicle_class: str = Query(...),
    rental_type: str = Query(...),
    included_hours: int = Query(...),
    on_date: Optional[date] = Query(None),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
    scope: BranchScope = Depends(get_branch_scope),
):
    """The single override a quote would use, if any. Read-only lookup."""
    await _desk_customer(db, customer_id, scope)
    effective_city_id = scope.city_id if scope.restricted else city_id
    custom = await load_custom_quote(
        db,
        customer_id=customer_id,
        city_id=effective_city_id,
        vehicle_class=vehicle_class,
        rental_type=rental_type.upper(),
        included_hours=included_hours,
        on_date=on_date,
    )
    if custom is None:
        raise ResourceNotFoundException("No agreed price for that package")
    return {
        "custom_quote": custom.as_dict(),
        "applies": custom.covers(on_date or date.today()),
    }
