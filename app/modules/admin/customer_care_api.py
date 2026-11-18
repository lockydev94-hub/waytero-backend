# ============================================================
# WAYTERO — ADMIN CUSTOMER CARE API
# File: app/modules/admin/customer_care_api.py
# Prefix: /admin/customer-care  (registered in api/router.py)
# Doc Ref:
#   DB Schema Part 2 — Customer (customers, customer_addresses)
#   DB Schema Part 4 — Booking Engine (master_bookings, cab_bookings)
#   Auth Models — users table
#   Vehicle Models — vehicle_categories, vehicle_pricing_rules
#   Master Models — cities
# ============================================================

import asyncio as _asyncio
import json
import math
import uuid as _uuid
from datetime import datetime, timezone, date, timedelta
from decimal import Decimal
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.branch_scope import (
    BranchScope,
    city_scope_sql,
    get_branch_scope,
    require_row_scope,
)
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.exceptions import ResourceNotFoundException, ValidationException
from app.modules.admin.cab_route_service import assert_city_active
from app.modules.auth.models.user import User
from app.modules.booking.models import (
    MasterBooking,
    CabBooking,
    BookingService,
    BookingTimeline,
)
from app.modules.booking.services.cab_availability import (
    CAB_TRIP_TYPES,
    cab_booking_disabled_message,
    enabled_cab_trip_types,
    is_cab_trip_type_enabled,
)

# One definition of "is this the same phone number", shared with the booking
# guards: the desk matches what an agent types, the booking path checks what is
# on file, and both have to mean the same ten digits.
from app.shared.utilities.mobile import mobile_match_sql, normalise_mobile

# Hotel pricing engine — reused so an admin-created hotel booking is priced by
# exactly the same rate/tax/commission rules as a customer-side booking.
from app.modules.hotel.models import Hotel

router = APIRouter()

# ── Pydantic Schemas ─────────────────────────────────────────


class CustomerCareLogOut(BaseModel):
    id: int
    log_number: str
    customer_id: Optional[int] = None
    customer_name: Optional[str] = None
    customer_mobile: Optional[str] = None
    issue_type: str
    subject: str
    description: str
    status: str
    priority: str
    booking_id: Optional[int] = None
    booking_number: Optional[str] = None
    resolved_at: Optional[datetime] = None
    created_by_name: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class CustomerCareLogCreate(BaseModel):
    customer_id: int
    issue_type: str = Field(
        ..., description="BOOKING_ISSUE | PAYMENT_ISSUE | INQUIRY | COMPLAINT | OTHER"
    )
    subject: str = Field(..., max_length=255)
    description: str = Field(..., max_length=2000)
    priority: str = Field(default="MEDIUM", description="LOW | MEDIUM | HIGH | URGENT")
    booking_id: Optional[int] = None


class CustomerCareLogUpdate(BaseModel):
    status: Optional[str] = None
    priority: Optional[str] = None
    description: Optional[str] = None
    resolution_notes: Optional[str] = None


class CustomerLookupResult(BaseModel):
    found: bool
    customer_id: Optional[int] = None
    customer_code: Optional[str] = None
    user_id: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    full_name: Optional[str] = None
    mobile_number: Optional[str] = None
    email: Optional[str] = None
    is_active: Optional[bool] = None
    created_at: Optional[datetime] = None
    total_bookings: Optional[int] = None
    total_issues: Optional[int] = None

    model_config = {"from_attributes": True}


class CustomerRegisterByAdmin(BaseModel):
    first_name: str = Field(..., max_length=100)
    last_name: Optional[str] = Field(None, max_length=100)
    mobile_number: str = Field(..., min_length=10, max_length=15)
    email: Optional[str] = None
    city_id: Optional[int] = None


class CabStopIn(BaseModel):
    """A place between pickup and drop, as the agent read it down the phone."""

    name: str = Field(..., max_length=500)
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    city_id: Optional[int] = None


class CabBookingQuoteRequest(BaseModel):
    """What the desk needs to quote a trip before it commits to booking one.

    No amount and no distance here — those are the server's answers, which is the
    whole point. The agent gives places, a cab type, a trip type and a time.
    """

    city_id: Optional[int] = None
    destination_city_id: Optional[int] = None
    vehicle_category_id: int = Field(..., gt=0)
    trip_type: str = Field(
        ..., description="LOCAL | AIRPORT | OUTSTATION | ONE_WAY | ROUND_TRIP"
    )
    pickup_location: str = Field(..., max_length=500)
    drop_location: str = Field(..., max_length=500)
    # Stops are measured as extra legs, so a detour the agent promised is in the
    # distance the customer is quoted.
    stops: List[CabStopIn] = Field(default_factory=list)
    pickup_datetime: datetime
    return_datetime: Optional[datetime] = None
    # Minutes the customer asked the cab to wait on this trip. A property of the
    # trip, not of the city's fare sheet — which is why the desk can say it here
    # and nowhere else.
    waiting_minutes: Optional[int] = Field(None, ge=0, le=1440)
    # Only used to say which coupons this customer could actually redeem.
    customer_id: Optional[int] = None


class CabBookingCreateByAdmin(BaseModel):
    """Create-on-behalf payload.

    `estimated_distance` and any hand-typed amount are gone: the booking is
    priced by the same server call the quote screen made, from the places the
    agent named. `customer_id` is required because the fare a customer is told
    and the coupon they may use both depend on who they are.
    """

    customer_id: int
    city_id: Optional[int] = None
    destination_city_id: Optional[int] = None
    # PACKAGE prices from the rate card (migration 0097/0098); PER_KM is the
    # older per-kilometre engine. The desk chooses, and both are priced by the
    # server — the mode only decides which sheet answers.
    pricing_mode: str = Field(default="PER_KM", description="PER_KM | PACKAGE")
    vehicle_class: Optional[str] = Field(None, max_length=40)
    hours: Optional[float] = Field(None, ge=0, le=24 * 30)
    km: Optional[float] = Field(None, ge=0, le=20000)
    days: Optional[int] = Field(None, ge=1, le=30)
    returned_after_midnight: bool = False
    trip_type: str = Field(
        ..., description="LOCAL | AIRPORT | OUTSTATION | ONE_WAY | ROUND_TRIP"
    )
    # Required for PER_KM. In PACKAGE mode it is derived from the rate class's
    # card row, because the class is what was priced.
    vehicle_category_id: Optional[int] = Field(None, gt=0)
    pickup_location: str = Field(..., max_length=500)
    pickup_latitude: Optional[float] = None
    pickup_longitude: Optional[float] = None
    drop_location: str = Field(..., max_length=500)
    drop_latitude: Optional[float] = None
    drop_longitude: Optional[float] = None
    stops: List[CabStopIn] = Field(default_factory=list)
    pickup_datetime: datetime
    # Return datetime for ROUND_TRIP — used by the fare engine to compute
    # ``trip_days`` when the matched pricing rule has
    # ``driver_allowance_type = PER_DAY``. Added migration 0049.
    return_datetime: Optional[datetime] = None
    waiting_minutes: Optional[int] = Field(None, ge=0, le=1440)
    coupon_code: Optional[str] = Field(None, max_length=50)
    remarks: Optional[str] = None


class CabCouponOptionOut(BaseModel):
    """One coupon the desk can honestly offer this booking.

    Produced by the same validator that decides whether a redemption is accepted,
    so a code recommended here cannot fail at create for a reason the screen
    could have said out loud.
    """

    coupon_id: int
    coupon_code: str
    title: str
    discount_amount: Decimal
    final_amount: Decimal
    coupon_type: str
    partner_funded: bool


class CabQuoteOut(BaseModel):
    """The desk's price panel. Money leaves as Decimal so the JSON cannot round
    a figure the customer is about to be told."""

    city_id: int
    pricing_city_id: Optional[int] = None
    vehicle_category_id: int
    trip_type: str
    distance_km: Decimal
    # GOOGLE | CONFIGURED_ROUTE | HAVERSINE — the agent says which one it was.
    distance_source: str
    billable_km: Decimal
    route_id: Optional[int] = None
    toll_configured: bool
    base_fare: Decimal
    distance_charge: Decimal
    driver_allowance: Decimal
    night_charge: Decimal
    toll: Decimal
    waiting: Decimal
    total: Decimal
    notes: List[str] = Field(default_factory=list)
    coupons: List[CabCouponOptionOut] = Field(default_factory=list)


class CabBookingCreateOut(BaseModel):
    """What the desk gets back, in the same Decimal form it was quoted in.

    `estimated_amount` is the fare, `payable_amount` is what the customer pays
    after the coupon. Returning both stops a screen from subtracting the
    discount a second time.
    """

    success: bool
    master_booking_id: int
    booking_number: str
    cab_booking_id: int
    cab_booking_number: str
    distance_km: Decimal
    distance_source: str
    billable_km: Decimal
    estimated_amount: Decimal
    coupon_discount: Decimal
    payable_amount: Decimal
    notes: List[str] = Field(default_factory=list)
    message: str


class CabBookingEditByAdmin(BaseModel):
    """Change a booking that already exists to the customer's new requirement.

    The same fields a create takes, minus the customer (a booking is whose it is
    and does not move) and minus the coupon: the redemption is already recorded,
    with the funding split that goes with it, and re-valuing it here would be
    adjusting booked money. And as at create, there is no distance and no amount —
    the amended fare is the fare engine's answer for the amended trip.
    """

    city_id: Optional[int] = None
    destination_city_id: Optional[int] = None
    trip_type: str = Field(
        ..., description="LOCAL | AIRPORT | OUTSTATION | ONE_WAY | ROUND_TRIP"
    )
    vehicle_category_id: int = Field(..., gt=0)
    pickup_location: str = Field(..., max_length=500)
    pickup_latitude: Optional[float] = None
    pickup_longitude: Optional[float] = None
    drop_location: str = Field(..., max_length=500)
    drop_latitude: Optional[float] = None
    drop_longitude: Optional[float] = None
    stops: List[CabStopIn] = Field(default_factory=list)
    pickup_datetime: datetime
    return_datetime: Optional[datetime] = None
    waiting_minutes: Optional[int] = Field(None, ge=0, le=1440)
    # Why the trip changed. Free text from the agent, written to the audit trail
    # next to the figures — the numbers say what the fare became, this says why.
    reason: Optional[str] = Field(None, max_length=500)


class CabBookingEditOut(BaseModel):
    """The amended booking, in the same Decimal form it was quoted in.

    `old_amount` is here because the agent's next sentence to the customer is
    usually "so it was ₹X and now it is ₹Y", and `re_offered` because closing the
    outstanding offers is a decision the desk has to be able to see.
    """

    success: bool
    master_booking_id: int
    booking_number: str
    cab_booking_id: int
    cab_booking_number: str
    distance_km: Decimal
    distance_source: str
    billable_km: Decimal
    old_amount: Decimal
    estimated_amount: Decimal
    coupon_discount: Decimal
    payable_amount: Decimal
    re_offered: bool
    notes: List[str] = Field(default_factory=list)
    message: str


# ── Hotel booking (by admin) ─────────────────────────────────


class HotelReservationGuestInput(BaseModel):
    guest_name: str = Field(..., max_length=255)
    mobile: Optional[str] = Field(None, max_length=15)
    gender: Optional[str] = Field(None, max_length=20)
    age: Optional[int] = None
    id_type: Optional[str] = Field(None, max_length=50)
    id_number: Optional[str] = Field(None, max_length=100)
    is_primary: bool = False


class HotelBookingQuoteRequest(BaseModel):
    """Ask the server to price a stay. The client never computes hotel money —
    per-night rate plans + GST slabs are resolved backend-side."""

    hotel_id: int
    room_category_id: int
    check_in_date: date
    check_out_date: date
    rooms_count: int = Field(default=1, ge=1, le=30)
    adults_count: int = Field(default=1, ge=1)
    children_count: int = Field(default=0, ge=0)
    extra_beds: int = Field(default=0, ge=0)


class HotelBookingCreateByAdmin(BaseModel):
    customer_id: int
    hotel_id: int
    room_category_id: int
    check_in_date: date
    check_out_date: date
    rooms_count: int = Field(default=1, ge=1, le=30)
    adults_count: int = Field(default=1, ge=1)
    children_count: int = Field(default=0, ge=0)
    extra_beds: int = Field(default=0, ge=0)
    guests: List[HotelReservationGuestInput] = Field(default_factory=list)
    special_requests: Optional[str] = None
    remarks: Optional[str] = None


class HotelBookingEditByAdmin(BaseModel):
    """Amend a still-CONFIRMED reservation to the customer's new requirement.
    Every priced field is re-quoted server-side; only the fields the admin
    changes need to be sent, but the form sends the full current state."""

    room_category_id: int
    check_in_date: date
    check_out_date: date
    rooms_count: int = Field(default=1, ge=1, le=30)
    adults_count: int = Field(default=1, ge=1)
    children_count: int = Field(default=0, ge=0)
    extra_beds: int = Field(default=0, ge=0)
    special_requests: Optional[str] = None


class PaginatedCareLogsOut(BaseModel):
    total: int
    page: int
    page_size: int
    total_pages: int
    items: List[CustomerCareLogOut]


class CustomerBookingHistoryOut(BaseModel):
    id: int
    booking_number: str
    booking_status: str
    payment_status: str
    total_amount: Optional[float] = None
    journey_start_date: Optional[str] = None
    city_name: Optional[str] = None
    created_at: str


class CustomerPreviousRecordsOut(BaseModel):
    bookings: List[CustomerBookingHistoryOut]
    booking_total: int
    booking_page: int
    booking_pages: int
    issues: List[CustomerCareLogOut]
    issue_total: int
    issue_page: int
    issue_pages: int


# ── Helpers ──────────────────────────────────────────────────

_care_table_ensured = False  # module-level flag — DDL runs once per process
_care_table_lock = _asyncio.Lock()  # prevents concurrent DDL on startup

# Booking numbers come from the centralised numbering service so admin
# and customer-side creation cannot collide on the same sequence.
# Doc Ref: BRD Part 3 §20, app/modules/booking/services/numbering.py


def _legacy_booking_number(prefix: str) -> str:
    """
    Legacy fallback for non-cab paths (e.g. hotel bookings). The cab path
    uses the centralised, MAX-based sequence — see numbering.py. Hotel
    bookings still hit raw-SQL INSERTs and a timestamp+uuid suffix is
    good enough for them; the prefix disambiguates the formats.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    uid = str(_uuid.uuid4()).replace("-", "")[:6].upper()
    return f"{prefix}-{ts}-{uid}"


# Aliases kept so existing hotel-flow callers compile unchanged.
def _booking_number() -> str:
    return _legacy_booking_number("MT")


def _cab_number() -> str:
    return _legacy_booking_number("CB")


def _log_number() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    uid = str(_uuid.uuid4()).replace("-", "")[:4].upper()
    return f"CC-{ts}-{uid}"


def _reservation_number() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    uid = str(_uuid.uuid4()).replace("-", "")[:6].upper()
    return f"HR-{ts}-{uid}"


async def _ensure_care_log_table(db: AsyncSession):
    """
    Create customer_care_logs table + indexes if they don't exist.
    - Runs ONCE per process (module-level flag guards re-entry).
    - Asyncio lock prevents concurrent DDL from parallel startup requests.
    - Uses raw asyncpg connection with execute() (not prepare()) to avoid
      the pg_type unique-violation that asyncpg's prepared-statement cache
      causes when CREATE TABLE IF NOT EXISTS is called while the table
      already exists in a concurrent session.

    Migration 0085 owns this table's shape and creates it with the rest of the
    schema; this runtime DDL only keeps a database that has not been upgraded
    readable. city_id is declared without an inline FK so 0085's
    fk_customer_care_logs_city is the only constraint on the column.
    """
    global _care_table_ensured
    if _care_table_ensured:
        return

    async with _care_table_lock:
        # Double-checked locking — another coroutine may have finished while we waited
        if _care_table_ensured:
            return

        # Use raw asyncpg connection to bypass prepared-statement caching.
        # SQLAlchemy asyncpg uses PREPARE for text() queries — this caches the
        # statement by SQL text in pg_type, causing a UniqueViolationError on
        # "pg_type_typname_nsp_index" when CREATE TABLE IF NOT EXISTS is called
        # concurrently (table already exists in pg_catalog).
        # Fix: get the real asyncpg connection and call .execute() directly,
        # which uses the simple query protocol (not prepared) — safe for DDL.
        #
        # Chain: AsyncSession -> AsyncConnection -> get_raw_connection()
        #        -> AsyncAdapt_asyncpg_connection -> .driver_connection (asyncpg conn)
        async_conn = await db.connection()  # SQLAlchemy AsyncConnection
        adapt_conn = (
            await async_conn.get_raw_connection()
        )  # AsyncAdapt_asyncpg_connection
        asyncpg_conn = adapt_conn.driver_connection  # real asyncpg Connection

        ddl_statements = [
            """
            CREATE TABLE IF NOT EXISTS customer_care_logs (
                id              BIGSERIAL PRIMARY KEY,
                log_number      VARCHAR(60)  UNIQUE NOT NULL,
                customer_id     BIGINT       REFERENCES customers(id) ON DELETE SET NULL,
                city_id         BIGINT,
                issue_type      VARCHAR(60)  NOT NULL DEFAULT 'INQUIRY',
                subject         VARCHAR(255) NOT NULL,
                description     TEXT         NOT NULL,
                status          VARCHAR(50)  NOT NULL DEFAULT 'OPEN',
                priority        VARCHAR(30)  NOT NULL DEFAULT 'MEDIUM',
                booking_id      BIGINT       REFERENCES master_bookings(id) ON DELETE SET NULL,
                resolution_notes TEXT,
                created_by      UUID         REFERENCES users(id) ON DELETE SET NULL,
                resolved_by     UUID         REFERENCES users(id) ON DELETE SET NULL,
                resolved_at     TIMESTAMP WITH TIME ZONE,
                created_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                updated_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_ccl_customer ON customer_care_logs(customer_id)",
            "CREATE INDEX IF NOT EXISTS idx_ccl_status ON customer_care_logs(status)",
            "CREATE INDEX IF NOT EXISTS idx_ccl_booking ON customer_care_logs(booking_id)",
        ]

        # asyncpg .execute() = simple query protocol, not prepared — avoids pg_type collision
        for stmt in ddl_statements:
            await asyncpg_conn.execute(stmt)

        _care_table_ensured = True


# ── Routes ───────────────────────────────────────────────────


# ── 1. List all care logs (paginated) ────────────────────────
@router.get("", response_model=PaginatedCareLogsOut, tags=["Customer Care"])
async def list_care_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status_filter: Optional[str] = Query(None, alias="status"),
    issue_type: Optional[str] = Query(None),
    priority: Optional[str] = Query(None),
    search: Optional[str] = Query(None, description="customer name or mobile"),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
    scope: BranchScope = Depends(get_branch_scope),
):
    await _ensure_care_log_table(db)

    where_parts = ["1=1"]
    params: dict = {"limit": page_size, "offset": (page - 1) * page_size}

    # cl.city_id was added nullable by 0085, so a log with no city is head-office
    # work — allow_unassigned left at its default keeps it out of a sub-branch's
    # list but in HQ's. The same where_sql drives the COUNT above, so the total
    # and the page cannot disagree.
    scope_clause, scope_params = city_scope_sql(scope, "cl.city_id")
    where_parts.append(f"({scope_clause})")
    params.update(scope_params)

    if status_filter:
        where_parts.append("cl.status = :status")
        params["status"] = status_filter
    if issue_type:
        where_parts.append("cl.issue_type = :issue_type")
        params["issue_type"] = issue_type
    if priority:
        where_parts.append("cl.priority = :priority")
        params["priority"] = priority
    if search:
        where_parts.append(
            "(u.mobile_number ILIKE :search OR u.first_name ILIKE :search OR u.last_name ILIKE :search)"
        )
        params["search"] = f"%{search}%"

    where_sql = " AND ".join(where_parts)

    total_r = await db.execute(
        text(
            f"""
        SELECT COUNT(*) FROM customer_care_logs cl
        LEFT JOIN customers c ON c.id = cl.customer_id
        LEFT JOIN users u ON u.id = c.user_id
        WHERE {where_sql}
    """
        ),
        params,
    )
    total = total_r.scalar() or 0

    rows_r = await db.execute(
        text(
            f"""
        SELECT
            cl.id, cl.log_number, cl.customer_id,
            u.first_name || ' ' || COALESCE(u.last_name, '') AS customer_name,
            u.mobile_number AS customer_mobile,
            cl.issue_type, cl.subject, cl.description,
            cl.status, cl.priority, cl.booking_id,
            mb.booking_number,
            cl.resolved_at, cl.created_at, cl.updated_at,
            cb_u.first_name || ' ' || COALESCE(cb_u.last_name, '') AS created_by_name
        FROM customer_care_logs cl
        LEFT JOIN customers c ON c.id = cl.customer_id
        LEFT JOIN users u ON u.id = c.user_id
        LEFT JOIN master_bookings mb ON mb.id = cl.booking_id
        LEFT JOIN users cb_u ON cb_u.id = cl.created_by
        WHERE {where_sql}
        ORDER BY cl.created_at DESC
        LIMIT :limit OFFSET :offset
    """
        ),
        params,
    )

    items = [CustomerCareLogOut(**dict(r)) for r in rows_r.mappings().all()]
    pages = math.ceil(total / page_size) if total > 0 else 1
    return PaginatedCareLogsOut(
        total=total, page=page, page_size=page_size, total_pages=pages, items=items
    )


# ── 2. Look up customer by mobile ────────────────────────────
def _customer_in_branch_sql(scope: BranchScope) -> tuple[str, dict]:
    """Is this customer in the caller's book at all?

    A customer row's own city is only one answer and often no answer: it arrived
    with 0085 and is NULL for everyone registered before it. So the trip history
    and the care history get a vote too — a Cuttack agent who is asked about a
    Bhubaneswar regular is legitimately their customer, and a mobile that matches
    nothing in the branch is not.

    Head office also owns the customers nobody has placed yet, same as the queues.
    """
    if not scope.restricted:
        return "1 = 1", {}
    params = {"scope_cities": sorted(scope.cities)}
    home = "c.city_id = ANY(:scope_cities)"
    if scope.include_unassigned:
        home = f"({home} OR c.city_id IS NULL)"
    booked = (
        "EXISTS (SELECT 1 FROM master_bookings mbv "
        "WHERE mbv.customer_id = c.id AND mbv.city_id = ANY(:scope_cities))"
    )
    cared = (
        "EXISTS (SELECT 1 FROM customer_care_logs clv "
        "WHERE clv.customer_id = c.id AND clv.city_id = ANY(:scope_cities))"
    )
    return f"({home} OR {booked} OR {cared})", params


@router.get("/lookup", response_model=CustomerLookupResult, tags=["Customer Care"])
async def lookup_customer(
    mobile: str = Query(..., min_length=10),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
    scope: BranchScope = Depends(get_branch_scope),
):
    await _ensure_care_log_table(db)

    # The agent types what the customer said, so `+91 98765 43210` has to reach
    # the same row as `9876543210`. An exact string compare answered "not found"
    # for one of them — and the desk's next move after that is to register a
    # second account for a customer who already books here, splitting their
    # history across two profiles.
    digits = normalise_mobile(mobile)
    if digits is None:
        # Not "found=False". A number that cannot be a mobile has no answer to
        # give about who owns it, and telling the agent nobody has it sends them
        # to register a customer who exists.
        raise ValidationException(
            "That is not a 10-digit mobile number, so there is nothing to look "
            "up. Check the number the customer gave you."
        )

    branch_clause, branch_params = _customer_in_branch_sql(scope)
    r = await db.execute(
        text(
            f"""
        SELECT
            c.id AS customer_id, c.customer_code,
            u.id AS user_id,
            u.first_name, u.last_name,
            u.first_name || ' ' || COALESCE(u.last_name, '') AS full_name,
            u.mobile_number, u.email, u.is_active,
            u.created_at,
            (SELECT COUNT(*) FROM master_bookings WHERE customer_id = c.id) AS total_bookings,
            (SELECT COUNT(*) FROM customer_care_logs WHERE customer_id = c.id) AS total_issues
        FROM users u
        JOIN customers c ON c.user_id = u.id
        WHERE u.user_type = 'CUSTOMER'
          AND {mobile_match_sql("u.mobile_number", "digits")}
          AND ({branch_clause})
        LIMIT 1
    """
        ),
        {"digits": digits, **branch_params},
    )

    row = r.mappings().one_or_none()
    if not row:
        return CustomerLookupResult(found=False)

    d = dict(row)
    d["user_id"] = str(d["user_id"])
    return CustomerLookupResult(found=True, **d)


# ── 2b. Fetch a single customer's profile by id ──────────────
@router.get(
    "/customer/{customer_id}",
    response_model=CustomerLookupResult,
    tags=["Customer Care"],
)
async def get_customer_profile(
    customer_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
    scope: BranchScope = Depends(get_branch_scope),
):
    """Basic customer profile by id — used by the admin booking pages to
    pre-fill the primary guest / passenger with the customer's own details."""
    branch_clause, branch_params = _customer_in_branch_sql(scope)
    r = await db.execute(
        text(
            f"""
        SELECT
            c.id AS customer_id, c.customer_code,
            u.id AS user_id,
            u.first_name, u.last_name,
            u.first_name || ' ' || COALESCE(u.last_name, '') AS full_name,
            u.mobile_number, u.email, u.is_active,
            u.created_at,
            (SELECT COUNT(*) FROM master_bookings WHERE customer_id = c.id) AS total_bookings
        FROM customers c
        JOIN users u ON u.id = c.user_id
        WHERE c.id = :cid AND u.user_type = 'CUSTOMER'
          AND ({branch_clause})
        LIMIT 1
    """
        ),
        {"cid": customer_id, **branch_params},
    )

    row = r.mappings().one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Customer not found")

    d = dict(row)
    d["user_id"] = str(d["user_id"])
    return CustomerLookupResult(found=True, **d)


@router.post(
    "/register-customer", response_model=CustomerLookupResult, tags=["Customer Care"]
)
async def register_customer_by_admin(
    payload: CustomerRegisterByAdmin,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
    scope: BranchScope = Depends(get_branch_scope),
):
    # A customer is registered in the caller's city: a branch cannot file a
    # customer it will never own work for, and a super admin's supplied city has
    # to be active. Supplying nothing keeps the customer city-less, i.e.
    # head-office — the same rule the enquiry lists already follow.
    if scope.restricted:
        effective_city_id = scope.city_id
    elif payload.city_id is not None:
        await assert_city_active(db, payload.city_id, "Customer city")
        effective_city_id = payload.city_id
    else:
        effective_city_id = None

    # `users.mobile_number` is unique on the string, which is a weaker claim than
    # "one person, one account": `9876543210` and `+919876543210` both insert
    # cleanly. Matching on the digits is what makes "already registered" true,
    # and storing the digits is what lets `/lookup` find the row back.
    digits = normalise_mobile(payload.mobile_number)
    if digits is None:
        raise ValidationException(
            "That is not a 10-digit mobile number. Re-enter the customer's "
            "number — a profile needs a number a driver can actually call."
        )
    dup = await db.execute(
        text(
            f"SELECT id FROM users WHERE {mobile_match_sql('mobile_number', 'digits')} LIMIT 1"
        ),
        {"digits": digits},
    )
    if dup.scalar_one_or_none():
        raise HTTPException(
            status_code=409,
            detail=(
                "This mobile is already registered. Look the customer up in "
                "Customer Care and book on that profile instead of creating a "
                "second one."
            ),
        )

    # Create user
    new_uid = _uuid.uuid4()
    count_r = await db.execute(
        text("SELECT COUNT(*) FROM users WHERE user_type = 'CUSTOMER'")
    )
    seq = (count_r.scalar() or 0) + 1
    user_code = f"CUS-{seq:06d}"

    await db.execute(
        text(
            """
        INSERT INTO users (
            id, user_code, first_name, last_name, mobile_number, email,
            user_type, status, is_active, is_mobile_verified, is_email_verified,
            created_by, created_at, updated_at
        ) VALUES (
            :id, :user_code, :first_name, :last_name, :mobile, :email,
            'CUSTOMER', 'ACTIVE', TRUE, FALSE, FALSE,
            :created_by, NOW(), NOW()
        )
    """
        ),
        {
            "id": str(new_uid),
            "user_code": user_code,
            "first_name": payload.first_name,
            "last_name": payload.last_name,
            "mobile": digits,
            "email": payload.email,
            "created_by": str(current_user["sub"]),
        },
    )

    # Create customer profile
    cust_count = await db.execute(text("SELECT COUNT(*) FROM customers"))
    cseq = (cust_count.scalar() or 0) + 1
    cust_code = f"CT-{cseq:06d}"

    await db.execute(
        text(
            """
        INSERT INTO customers (uuid, user_id, customer_code, first_name, last_name, city_id, is_active, created_at, updated_at)
        VALUES (:uuid, :user_id, :code, :first_name, :last_name, :city_id, TRUE, NOW(), NOW())
    """
        ),
        {
            "uuid": str(_uuid.uuid4()),
            "user_id": str(new_uid),
            "code": cust_code,
            "first_name": payload.first_name,
            "last_name": payload.last_name,
            "city_id": effective_city_id,
        },
    )

    # Auto-create customer wallet (₹0 balance) — uses customer id from DB
    await db.flush()  # flush so customer row is visible for the wallet FK
    new_cust_row = await db.execute(
        text("SELECT id FROM customers WHERE user_id = :uid"), {"uid": str(new_uid)}
    )
    new_cust_id = new_cust_row.scalar_one_or_none()
    if new_cust_id:
        await db.execute(
            text(
                """
            INSERT INTO customer_wallets
                (customer_id, available_balance, hold_balance, wallet_status, created_at, updated_at)
            VALUES (:cid, 0, 0, 'ACTIVE', NOW(), NOW())
            ON CONFLICT (customer_id) DO NOTHING
        """
            ),
            {"cid": new_cust_id},
        )

    await db.commit()

    # Return lookup result
    r = await db.execute(
        text(
            """
        SELECT c.id AS customer_id, c.customer_code,
               u.id AS user_id, u.first_name, u.last_name,
               u.first_name || ' ' || COALESCE(u.last_name, '') AS full_name,
               u.mobile_number, u.email, u.is_active, u.created_at,
               0 AS total_bookings, 0 AS total_issues
        FROM users u JOIN customers c ON c.user_id = u.id
        WHERE u.id = :uid
    """
        ),
        {"uid": str(new_uid)},
    )

    row = dict(r.mappings().one())
    row["user_id"] = str(row["user_id"])
    return CustomerLookupResult(found=True, **row)


# ── 4. Get customer previous records (bookings + issues) ──────
@router.get(
    "/customer/{customer_id}/records",
    response_model=CustomerPreviousRecordsOut,
    tags=["Customer Care"],
)
async def get_customer_records(
    customer_id: int,
    booking_page: int = Query(1, ge=1),
    issue_page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
    scope: BranchScope = Depends(get_branch_scope),
):
    await _ensure_care_log_table(db)

    # A customer's history is shown through city-carrying transactional rows, so
    # a branch sees only the part of that history that happened in its city. The
    # master booking's city is NOT NULL (allow_unassigned=False); a care log's
    # 0085 city is nullable, so an unassigned log stays head-office only.
    mb_scope, mb_params = city_scope_sql(
        scope, "master_bookings.city_id", allow_unassigned=False
    )
    cl_scope, cl_params = city_scope_sql(scope, "customer_care_logs.city_id")

    # Bookings
    b_total_r = await db.execute(
        text(
            f"SELECT COUNT(*) FROM master_bookings WHERE customer_id = :cid AND ({mb_scope})"
        ),
        {"cid": customer_id, **mb_params},
    )
    b_total = b_total_r.scalar() or 0
    b_offset = (booking_page - 1) * page_size

    b_mb_scope, b_mb_params = city_scope_sql(
        scope, "mb.city_id", allow_unassigned=False
    )
    b_rows = await db.execute(
        text(
            f"""
        SELECT mb.id, mb.booking_number, mb.booking_status, mb.payment_status,
               CAST(mb.total_amount AS FLOAT) AS total_amount,
               CAST(mb.journey_start_date AS TEXT) AS journey_start_date,
               ci.name AS city_name,
               TO_CHAR(mb.created_at, 'YYYY-MM-DD"T"HH24:MI:SS') AS created_at
        FROM master_bookings mb
        LEFT JOIN cities ci ON ci.id = mb.city_id
        WHERE mb.customer_id = :cid
          AND ({b_mb_scope})
        ORDER BY mb.created_at DESC
        LIMIT :limit OFFSET :offset
    """
        ),
        {"cid": customer_id, "limit": page_size, "offset": b_offset, **b_mb_params},
    )
    bookings = [CustomerBookingHistoryOut(**dict(r)) for r in b_rows.mappings().all()]

    # Issues
    i_total_r = await db.execute(
        text(
            f"SELECT COUNT(*) FROM customer_care_logs WHERE customer_id = :cid AND ({cl_scope})"
        ),
        {"cid": customer_id, **cl_params},
    )
    i_total = i_total_r.scalar() or 0
    i_offset = (issue_page - 1) * page_size

    i_cl_scope, i_cl_params = city_scope_sql(scope, "cl.city_id")
    i_rows = await db.execute(
        text(
            f"""
        SELECT
            cl.id, cl.log_number, cl.customer_id,
            u.first_name || ' ' || COALESCE(u.last_name, '') AS customer_name,
            u.mobile_number AS customer_mobile,
            cl.issue_type, cl.subject, cl.description,
            cl.status, cl.priority, cl.booking_id,
            mb.booking_number,
            cl.resolved_at, cl.created_at, cl.updated_at,
            cb_u.first_name || ' ' || COALESCE(cb_u.last_name, '') AS created_by_name
        FROM customer_care_logs cl
        LEFT JOIN customers c ON c.id = cl.customer_id
        LEFT JOIN users u ON u.id = c.user_id
        LEFT JOIN master_bookings mb ON mb.id = cl.booking_id
        LEFT JOIN users cb_u ON cb_u.id = cl.created_by
        WHERE cl.customer_id = :cid
          AND ({i_cl_scope})
        ORDER BY cl.created_at DESC
        LIMIT :limit OFFSET :offset
    """
        ),
        {"cid": customer_id, "limit": page_size, "offset": i_offset, **i_cl_params},
    )
    issues = [CustomerCareLogOut(**dict(r)) for r in i_rows.mappings().all()]

    return CustomerPreviousRecordsOut(
        bookings=bookings,
        booking_total=b_total,
        booking_page=booking_page,
        booking_pages=math.ceil(b_total / page_size) if b_total > 0 else 1,
        issues=issues,
        issue_total=i_total,
        issue_page=issue_page,
        issue_pages=math.ceil(i_total / page_size) if i_total > 0 else 1,
    )


# ── 5. Create a care log (issue/inquiry) ─────────────────────
@router.post(
    "", response_model=CustomerCareLogOut, status_code=201, tags=["Customer Care"]
)
async def create_care_log(
    payload: CustomerCareLogCreate,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
    scope: BranchScope = Depends(get_branch_scope),
):
    await _ensure_care_log_table(db)

    # A branch raises the log in its own city, so the row it just created is one
    # it can still see on its filtered list; scope.city_id is that city for a
    # branch (and for a super admin narrowed to one) and None otherwise, which
    # files an unassigned log as head-office work.
    log_num = _log_number()
    await db.execute(
        text(
            """
        INSERT INTO customer_care_logs
            (log_number, customer_id, city_id, issue_type, subject, description, status, priority,
             booking_id, created_by, created_at, updated_at)
        VALUES
            (:log_num, :cid, :city_id, :itype, :subject, :desc, 'OPEN', :priority,
             :bid, :created_by, NOW(), NOW())
    """
        ),
        {
            "log_num": log_num,
            "cid": payload.customer_id,
            "city_id": scope.city_id,
            "itype": payload.issue_type,
            "subject": payload.subject,
            "desc": payload.description,
            "priority": payload.priority,
            "bid": payload.booking_id,
            "created_by": str(current_user["sub"]),
        },
    )
    await db.commit()

    r = await db.execute(
        text(
            """
        SELECT
            cl.id, cl.log_number, cl.customer_id,
            u.first_name || ' ' || COALESCE(u.last_name, '') AS customer_name,
            u.mobile_number AS customer_mobile,
            cl.issue_type, cl.subject, cl.description,
            cl.status, cl.priority, cl.booking_id,
            mb.booking_number,
            cl.resolved_at, cl.created_at, cl.updated_at,
            cb_u.first_name || ' ' || COALESCE(cb_u.last_name, '') AS created_by_name
        FROM customer_care_logs cl
        LEFT JOIN customers c ON c.id = cl.customer_id
        LEFT JOIN users u ON u.id = c.user_id
        LEFT JOIN master_bookings mb ON mb.id = cl.booking_id
        LEFT JOIN users cb_u ON cb_u.id = cl.created_by
        WHERE cl.log_number = :log_num
    """
        ),
        {"log_num": log_num},
    )

    return CustomerCareLogOut(**dict(r.mappings().one()))


# ── 6. Update a care log status/priority ─────────────────────
@router.patch("/{log_id}", response_model=CustomerCareLogOut, tags=["Customer Care"])
async def update_care_log(
    log_id: int,
    payload: CustomerCareLogUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
    scope: BranchScope = Depends(get_branch_scope),
):
    await _ensure_care_log_table(db)

    # By-id write: the list filters, but a PATCH that takes log_id in the path has
    # to check the row before touching it. Out-of-scope is a 404, not a 403, so a
    # Cuttack manager editing a Bhubaneswar log gets the same answer as a
    # nonexistent one.
    await require_row_scope(
        db,
        scope,
        table="customer_care_logs",
        id_column="id",
        id_value=log_id,
        city_column="city_id",
        label="Care log",
    )

    sets = ["updated_at = NOW()"]
    params: dict = {"log_id": log_id, "resolver": str(current_user["sub"])}

    if payload.status:
        sets.append("status = :status")
        params["status"] = payload.status
        if payload.status == "RESOLVED":
            sets.append("resolved_at = NOW()")
            sets.append("resolved_by = :resolver")
    if payload.priority:
        sets.append("priority = :priority")
        params["priority"] = payload.priority
    if payload.description:
        sets.append("description = :description")
        params["description"] = payload.description
    if payload.resolution_notes:
        sets.append("resolution_notes = :resolution_notes")
        params["resolution_notes"] = payload.resolution_notes

    await db.execute(
        text(f"UPDATE customer_care_logs SET {', '.join(sets)} WHERE id = :log_id"),
        params,
    )
    await db.commit()

    r = await db.execute(
        text(
            """
        SELECT
            cl.id, cl.log_number, cl.customer_id,
            u.first_name || ' ' || COALESCE(u.last_name, '') AS customer_name,
            u.mobile_number AS customer_mobile,
            cl.issue_type, cl.subject, cl.description,
            cl.status, cl.priority, cl.booking_id,
            mb.booking_number, cl.resolved_at, cl.created_at, cl.updated_at,
            cb_u.first_name || ' ' || COALESCE(cb_u.last_name, '') AS created_by_name
        FROM customer_care_logs cl
        LEFT JOIN customers c ON c.id = cl.customer_id
        LEFT JOIN users u ON u.id = c.user_id
        LEFT JOIN master_bookings mb ON mb.id = cl.booking_id
        LEFT JOIN users cb_u ON cb_u.id = cl.created_by
        WHERE cl.id = :log_id
    """
        ),
        {"log_id": log_id},
    )

    row = r.mappings().one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Care log not found")
    return CustomerCareLogOut(**dict(row))


# ── 7. Get vehicle categories + pricing for cab booking ───────
@router.get("/cab-booking/meta", tags=["Customer Care"])
async def cab_booking_meta(
    city_id: Optional[int] = Query(None),
    for_date: Optional[date] = Query(
        None, description="Day the trip happens; fare sheets are read as of it"
    ),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
    scope: BranchScope = Depends(get_branch_scope),
):
    """Returns vehicle categories with pricing for a given city (for admin cab booking form)."""
    # A branch can only price and book in its own city, so the city it asks
    # about is forced to its scope's; a super admin's request is taken as-is.
    effective_city_id = scope.city_id if scope.restricted else city_id

    cats_r = await db.execute(
        text(
            """
        SELECT id, category_name, seating_capacity, image_url, icon_url
        FROM vehicle_categories WHERE is_active = TRUE ORDER BY display_order, category_name
    """
        )
    )
    cats = [dict(r) for r in cats_r.mappings().all()]

    # Pricing per category per trip_type, as of the day of the trip.
    # The window matters: a superseded sheet still sits in the table, and a
    # next-month trip has to be explained from the sheet that will be in force
    # when it runs. `/cab-booking/quote` applies the same filter through
    # DefaultPricingService, so a number shown here is a number the quote can
    # actually honour.
    on_day = for_date or datetime.now(timezone.utc).date()
    if effective_city_id:
        pricing_r = await db.execute(
            text(
                """
            SELECT vehicle_category_id, trip_type, base_fare, per_km_rate,
                   minimum_km, driver_allowance, night_charge,
                   driver_allowance_type, night_charge_type, toll,
                   effective_from, effective_to
            FROM vehicle_pricing_rules
            WHERE city_id = :city_id
              AND (effective_from IS NULL OR effective_from <= :on_day)
              AND (effective_to   IS NULL OR effective_to   >= :on_day)
        """
            ),
            {"city_id": effective_city_id, "on_day": on_day},
        )
    else:
        pricing_r = await db.execute(
            text(
                """
            SELECT vehicle_category_id, trip_type, base_fare, per_km_rate,
                   minimum_km, driver_allowance, night_charge,
                   driver_allowance_type, night_charge_type, toll,
                   NULL::date AS effective_from, NULL::date AS effective_to
            FROM default_vehicle_pricing_rules
        """
            )
        )

    # One sheet per (category, trip type) or none. A pair with two rows covering
    # the same day is a configuration conflict the quote will refuse outright, so
    # the form is not allowed to display a price for it — showing one of the two
    # at random would have the desk quote a fare the booking then rejects.
    rows_by_key: dict = {}
    for row in pricing_r.mappings().all():
        rows_by_key.setdefault(
            (str(row["vehicle_category_id"]), row["trip_type"]), []
        ).append(row)

    pricing = {}
    for (cat_key, trip_type), group in rows_by_key.items():
        if len(group) != 1:
            continue
        row = group[0]
        pricing.setdefault(cat_key, {})[trip_type] = {
            "base_fare": float(row["base_fare"] or 0),
            "per_km_rate": float(row["per_km_rate"] or 0),
            "minimum_km": int(row["minimum_km"] or 0),
            "driver_allowance": float(row["driver_allowance"] or 0),
            "night_charge": float(row["night_charge"] or 0),
            "driver_allowance_type": row["driver_allowance_type"] or "PER_TRIP",
            "night_charge_type": row["night_charge_type"] or "FIXED",
            "toll": float(row["toll"] or 0),
            # The agent is quoting a price the customer hears over the phone;
            # whether the sheet they read is temporary is part of that answer.
            "effective_from": (
                row["effective_from"].isoformat() if row["effective_from"] else None
            ),
            "effective_to": (
                row["effective_to"].isoformat() if row["effective_to"] else None
            ),
        }

    # The city picker shows a branch only its own city — offering another city
    # here would be a booking the branch cannot honestly file.
    city_scope_clause, city_scope_params = city_scope_sql(
        scope, "id", allow_unassigned=False
    )
    cities_r = await db.execute(
        text(
            f"SELECT id, name FROM cities WHERE is_active = TRUE AND ({city_scope_clause}) ORDER BY name"
        ),
        city_scope_params,
    )
    cities = [dict(r) for r in cities_r.mappings().all()]

    # The desk's cards come from the same admin switch as the website. The
    # full list is returned with its state so the screen can also explain why
    # a product is absent instead of silently showing fewer options.
    enabled_types = set(await enabled_cab_trip_types(db))
    trip_types = [
        {"code": code, "enabled": code in enabled_types} for code in CAB_TRIP_TYPES
    ]

    return {
        "vehicle_categories": cats,
        "pricing": pricing,
        "cities": cities,
        "trip_types": trip_types,
    }


# ── 7b. Quote a trip, then book it ───────────────────────────


async def _desk_customer(
    db: AsyncSession, customer_id: int, scope: BranchScope
) -> dict:
    """The customer the desk is acting for, only if this branch may act for them.

    `/lookup` already answers "is this customer ours" by home city, booking
    history and care history, and the booking has to agree with that answer.
    Without the same test here, a Cuttack agent who had the customer id could
    file a booking that `/lookup` would refuse to show them — and the booking
    would then be invisible to the branch that has to service it.
    """
    branch_clause, branch_params = _customer_in_branch_sql(scope)
    row = (
        (
            await db.execute(
                text(
                    f"""
            SELECT
                c.id AS customer_id, c.customer_code,
                u.first_name, u.last_name,
                u.first_name || ' ' || COALESCE(u.last_name, '') AS full_name,
                u.mobile_number, u.email
            FROM customers c
            JOIN users u ON u.id = c.user_id
            WHERE c.id = :cid AND u.user_type = 'CUSTOMER'
              AND ({branch_clause})
        """
                ),
                {"cid": customer_id, **branch_params},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        # 404, not 403: an out-of-scope customer must not be confirmable by id.
        raise ResourceNotFoundException(
            "That customer is not in this branch's book, so a booking cannot be "
            "filed for them from here."
        )
    return dict(row)


async def _desk_pricing_city(payload, db: AsyncSession, scope: BranchScope) -> int:
    """Where the trip starts, which is whose fare sheet prices it.

    A branch never gets to choose: it books in its own city. A super admin does,
    and the city has to exist and be active.
    """
    if scope.restricted:
        return scope.city_id
    if not payload.city_id:
        raise ValidationException(
            "Choose the city the trip starts in — the cab fare depends on it."
        )
    await assert_city_active(db, payload.city_id, "Booking city")
    return payload.city_id


def _assert_trip_times(
    pickup_datetime: datetime, return_datetime: Optional[datetime]
) -> None:
    """Reject a trip that cannot be driven as described.

    Two mistakes the desk makes on the phone and only notices after the booking
    exists: a pickup time typed in the past (usually yesterday's date), and a
    return before the pickup (a 10:00 pickup "returning at 09:00"). Both priced
    happily before this check, because neither measurement nor the fare engine
    looks at the clock.

    A few minutes of grace is deliberate: an agent booking a cab for a customer
    who is already outside is not an error, and the pickups are minute-level.
    """
    now = datetime.now(timezone.utc)
    pickup = (
        pickup_datetime
        if pickup_datetime.tzinfo
        else pickup_datetime.replace(tzinfo=timezone.utc)
    )
    if pickup < now - timedelta(minutes=5):
        raise ValidationException(
            "The pickup time is in the past. Choose a time that has not happened "
            "yet, or record the trip through Trip Assistance."
        )
    if return_datetime is not None:
        back = (
            return_datetime
            if return_datetime.tzinfo
            else return_datetime.replace(tzinfo=timezone.utc)
        )
        if back <= pickup:
            raise ValidationException(
                "The return time is before the pickup time. Fix the return or "
                "remove it before booking."
            )


async def _guard_trip_type(
    db: AsyncSession,
    *,
    trip_type: str,
    pickup_location: str,
    drop_location: str,
    stops: list[CabStopIn] | None,
    pickup_datetime: datetime,
    return_datetime: Optional[datetime],
    allow_drop_for_duration: bool = False,
    enforce_availability: bool = True,
) -> None:
    """Refuse a trip whose type does not match the places the agent named.

    The desk is where this matters most: the agent picks the type from memory
    while the customer says "airport, then Puri, then back". This runs before any
    pricing, so a Local ending in another city cannot be quoted at all — the
    error tells the agent which type to pick instead of pricing the wrong sheet.

    The same guard enforces the admin's booking switch. `enforce_availability`
    is false only when an operator is amending an existing trip of the same
    disabled type — servicing a booked trip is not creating a new one.
    """
    from app.modules.booking.services.trip_guard import TripPoint, check_trip

    trip_type = (trip_type or "").strip().upper()
    if enforce_availability and not await is_cab_trip_type_enabled(db, trip_type):
        raise ValidationException(cab_booking_disabled_message(trip_type))

    check = await check_trip(
        db,
        trip_type=trip_type,
        pickup=TripPoint(name=pickup_location),
        drop=TripPoint(name=drop_location) if drop_location else None,
        stops=[TripPoint(name=s.name) for s in (stops or []) if s.name],
        pickup_datetime=pickup_datetime,
        return_datetime=return_datetime,
        allow_drop_for_duration=allow_drop_for_duration,
    )
    if check.errors:
        raise ValidationException(" ".join(check.errors))


def _km_text(value: Decimal | float) -> str:
    """Kilometres the way a person reads them: 30, not 30.0 and not 3E+1.

    The fare engine hands back `billable_km` as a float and the distance
    resolver as a Decimal, so one formatter is what keeps "Distance charge
    (30 km)" from becoming "(30.0 km)" in a mail the customer keeps. Fixed-point
    then trimmed, because `Decimal.normalize()` turns 30 into `3E+1`.
    """
    rendered = f"{Decimal(str(value)).quantize(Decimal('0.1')):f}"
    return rendered.rstrip("0").rstrip(".")


def _cab_confirmation_context(
    quote,
    *,
    package: bool = False,
    booking_number: str,
    trip_type: str,
    pickup_location: str,
    drop_location: str,
    pickup_datetime: datetime,
    coupon_code: Optional[str],
    coupon_discount: Decimal,
) -> dict:
    """What the customer is emailed after the desk books for them.

    The customer agreed to a price over the telephone; this email is the only
    thing they keep. Every figure in it is the server's own quote — the same
    arithmetic that produced the amount stored on the booking — so the message
    cannot disagree with the row. Nothing here is written by hand: no route is
    named that was not given, no savings is claimed that the coupon engine did
    not compute, and there is no secret in the payload (no MPIN, no token).

    Kept apart from the endpoint because the sum of the lines and the total the
    customer is told they pay are money claims worth testing directly.
    """
    payable = quote.total - coupon_discount

    if package:
        # A package trip is sold as hours and kilometres up front, so the email
        # names the package instead of a distance charge the customer never saw
        # quoted. Only the lines that carry money are listed, for the same
        # reason the per-km panel omits its zeroes.
        q = quote.applied
        lines = [
            ("Trip type", f"{trip_type.title()} package"),
            ("Pickup", pickup_location or ""),
            ("Drop", drop_location or ""),
            ("Pickup time", pickup_datetime.strftime("%d %b %Y, %I:%M %p")),
            (
                "Package",
                f"{q.package_hours}h / {q.package_km}km"
                + (f" × {q.days} days" if q.days > 1 else ""),
            ),
        ]
        money_lines = [
            ("Package price", q.package_price),
            (f"Extra hours ({q.extra_hours:g}h)", q.extra_hour_charge),
            (f"Extra km ({q.extra_km:g}km)", q.extra_km_charge),
            ("Driver allowance", q.driver_allowance),
            ("Midnight return", q.midnight_charge),
        ]
    else:
        b = quote.breakdown
        lines = [
            ("Trip type", trip_type or ""),
            ("Pickup", pickup_location or ""),
            ("Drop", drop_location or ""),
            ("Pickup time", pickup_datetime.strftime("%d %b %Y, %I:%M %p")),
            ("Distance", f"{_km_text(quote.distance.km)} km"),
        ]
        money_lines = [
            ("Base fare", b["base_fare"]),
            # The kilometre figure in the label is what was *billed*, not what was
            # driven: a ONE_WAY shorter than the sheet's minimum_km is billed at the
            # minimum, and printing the driven distance there is the arithmetic that
            # does not add up.
            (
                f"Distance charge ({_km_text(b['billable_km'])} km)",
                b["distance_charge"],
            ),
            ("Driver allowance", b["driver_allowance"]),
            ("Night charge", b["night_charge_applied"]),
            ("Toll", b["toll"]),
            ("Waiting", b["waiting"]),
        ]
    lines += [
        (label, f"₹{amount.quantize(Decimal('0.01'))}")
        for label, amount in money_lines
        if amount
    ]
    if coupon_code:
        lines.append(
            (f"Coupon {coupon_code}", f"−₹{coupon_discount.quantize(Decimal('0.01'))}")
        )

    return {
        "service": "Cab",
        "booking_number": booking_number,
        "message": (
            f"Your cab booking {booking_number} is confirmed. A partner is being "
            "assigned — driver details will be shared before pickup."
        ),
        "details": lines,
        "amount": payable,
        "amount_label": "Estimated fare",
        "amount_note": "The final fare is billed on the trip actually driven.",
    }


def _timeline_actor(current_user: dict) -> Optional[_uuid.UUID]:
    """Who acted, as the timeline's `created_by`.

    A malformed subject in the token must not fail an amendment that has already
    happened to the customer's booking, so the row goes in unattributed rather
    than raising — the same tolerance the create path allows itself."""
    try:
        return _uuid.UUID(current_user["sub"])
    except (KeyError, TypeError, ValueError):
        return None


def _cab_amendment_note(cb, payload, quote, re_offered: bool) -> str:
    """The audit sentence an amendment leaves behind.

    The question months later is "what did we agree with this customer and who
    changed it", so the sentence carries the fare on both sides of the edit and
    only the fields that actually moved. Every figure is the server's re-measured
    one; the agent's own words appear only as the reason they gave.
    """
    old_total = Decimal(str(cb["estimated_amount"] or 0))
    parts = [
        "Cab booking amended by admin via Customer Care — "
        f"₹{old_total.quantize(Decimal('0.01'))} → "
        f"₹{quote.total.quantize(Decimal('0.01'))} for "
        f"{_km_text(quote.distance.km)} km ({quote.distance.source})"
    ]
    if str(cb["trip_type"] or "").upper() != payload.trip_type.upper():
        parts.append(f"trip type {cb['trip_type']} → {payload.trip_type}")
    if int(cb["vehicle_category_id"]) != payload.vehicle_category_id:
        parts.append(
            f"vehicle category {cb['vehicle_category_id']} → "
            f"{payload.vehicle_category_id}"
        )
    if str(cb["pickup_location"] or "") != payload.pickup_location:
        parts.append(f"pickup {cb['pickup_location']} → {payload.pickup_location}")
    if str(cb["drop_location"] or "") != payload.drop_location:
        parts.append(f"drop {cb['drop_location']} → {payload.drop_location}")
    if not cb["pickup_time_unchanged"]:
        # Only the new time is named. The stored one comes back as an instant and
        # the requested one as wall-clock, and putting both in one sentence would
        # read as a five-and-a-half hour delay that nobody asked for.
        parts.append(f"pickup moved to {payload.pickup_datetime:%d %b %Y %H:%M}")
    if not cb["return_time_unchanged"]:
        parts.append(
            "return removed"
            if payload.return_datetime is None
            else f"return moved to {payload.return_datetime:%d %b %Y %H:%M}"
        )
    if re_offered:
        parts.append(
            "outstanding partner offers closed — the trip needs assignment again"
        )
    if payload.reason:
        parts.append(f"reason: {payload.reason}")
    return "; ".join(parts)


@router.post("/cab-booking/quote", response_model=CabQuoteOut, tags=["Customer Care"])
async def quote_cab_booking_by_admin(
    payload: CabBookingQuoteRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
    scope: BranchScope = Depends(get_branch_scope),
):
    """What this trip costs, what it is made of, and what the customer could pay less with.

    The phone desk's price panel. Nothing in the answer is typed by anyone: the
    kilometres come from a published corridor or from Google, the rate comes from
    the fare sheet in force on the trip's day, and the lines are the fare engine's
    own breakdown. `POST /cab-booking` recomputes this same quote from the same
    inputs rather than trusting a number returned here, which is why there is no
    quote id to pass back and why a quote nobody re-used cannot go stale.

    Coupons are only listed when `customer_id` is supplied, because whether a code
    applies depends on who is asking as much as on what they are booking.
    """
    from app.modules.booking.services.trip_pricing import quote_cab_trip

    effective_city_id = await _desk_pricing_city(payload, db, scope)

    await _guard_trip_type(
        db,
        trip_type=payload.trip_type,
        pickup_location=payload.pickup_location,
        drop_location=payload.drop_location,
        stops=payload.stops,
        pickup_datetime=payload.pickup_datetime,
        return_datetime=payload.return_datetime,
    )

    quote = await quote_cab_trip(
        db,
        pickup_location=payload.pickup_location,
        drop_location=payload.drop_location,
        origin_city_id=effective_city_id,
        vehicle_category_id=payload.vehicle_category_id,
        trip_type=payload.trip_type,
        pickup_datetime=payload.pickup_datetime,
        return_datetime=payload.return_datetime,
        destination_city_id=payload.destination_city_id,
        waiting_minutes=payload.waiting_minutes,
        stops=[s.name for s in payload.stops if s.name],
    )
    b = quote.breakdown

    coupons = []
    if payload.customer_id:
        customer = await _desk_customer(db, payload.customer_id, scope)
        from app.modules.admin.coupon_api import recommend_coupons

        coupons = [
            CabCouponOptionOut(
                coupon_id=c.coupon_id,
                coupon_code=c.coupon_code,
                title=c.title,
                discount_amount=c.discount_amount,
                final_amount=c.final_amount,
                coupon_type=c.coupon_type,
                partner_funded=c.partner_funded,
            )
            for c in await recommend_coupons(
                db,
                service_type="CAB",
                city_id=effective_city_id,
                vehicle_category_id=payload.vehicle_category_id,
                booking_amount=quote.total,
                customer_id=payload.customer_id,
                customer_mobile=customer.get("mobile_number"),
                pickup_datetime=payload.pickup_datetime,
                return_datetime=payload.return_datetime,
            )
        ]

    return CabQuoteOut(
        city_id=effective_city_id,
        pricing_city_id=quote.pricing_city_id,
        vehicle_category_id=payload.vehicle_category_id,
        trip_type=payload.trip_type,
        distance_km=quote.distance.km,
        distance_source=quote.distance.source,
        billable_km=Decimal(str(b["billable_km"])),
        route_id=quote.distance.route_id,
        toll_configured=quote.distance.toll_configured,
        base_fare=Decimal(str(b["base_fare"])),
        distance_charge=Decimal(str(b["distance_charge"])),
        driver_allowance=Decimal(str(b["driver_allowance"])),
        night_charge=Decimal(str(b["night_charge_applied"])),
        toll=Decimal(str(b["toll"])),
        waiting=Decimal(str(b["waiting"])),
        total=quote.total,
        notes=quote.notes,
        coupons=coupons,
    )


# ── 8. Create cab booking by admin (on behalf of customer) ────
@router.post("/cab-booking", response_model=CabBookingCreateOut, tags=["Customer Care"])
async def create_cab_booking_by_admin(
    payload: CabBookingCreateByAdmin,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
    scope: BranchScope = Depends(get_branch_scope),
):
    """Admin creates a cab booking on behalf of a customer.

    Priced here, never accepted from the screen. The booking re-runs the same
    server-side quote the agent looked at, from the places they named, and stores
    the measured distance, the fare lines and the rule sheet behind them on the
    row. That is why there is no distance or amount on this payload: an agent who
    re-posts the form with a friendlier number changes nothing they can control.
    """
    from app.modules.booking.services.trip_guard import estimate_duration_minutes
    from app.modules.booking.services.trip_pricing import fare_columns, quote_cab_trip

    effective_city_id = await _desk_pricing_city(payload, db, scope)
    customer = await _desk_customer(db, payload.customer_id, scope)

    mode = (payload.pricing_mode or "PER_KM").upper()
    if mode not in {"PER_KM", "PACKAGE"}:
        raise ValidationException("pricing_mode must be PER_KM or PACKAGE.")
    _assert_trip_times(payload.pickup_datetime, payload.return_datetime)
    await _guard_trip_type(
        db,
        trip_type=payload.trip_type,
        pickup_location=payload.pickup_location,
        drop_location=payload.drop_location,
        stops=payload.stops,
        pickup_datetime=payload.pickup_datetime,
        return_datetime=payload.return_datetime,
        # A package is sold by duration, so its drop is context, not a claim
        # that the trip is a one-city ride.
        allow_drop_for_duration=mode == "PACKAGE",
    )

    # The two pricing modes answer different questions and both are priced here.
    # A PACKAGE trip is sold as a package (hours + kilometres agreed up front),
    # so its fare comes from the rate card and its category from the card's class
    # mapping — not from whatever the screen posted.
    package_quote = None
    if mode == "PACKAGE":
        from app.modules.booking.services.rental_booking import (
            quote_rental_package,
            rental_fare_columns,
        )

        if not payload.vehicle_class:
            raise ValidationException(
                "Choose a rate-card vehicle class for a package booking."
            )
        package_quote = await quote_rental_package(
            db,
            city_id=effective_city_id,
            vehicle_class=payload.vehicle_class,
            rental_type=payload.trip_type,
            hours=payload.hours or 0,
            km=payload.km or 0,
            days=payload.days or 1,
            returned_after_midnight=payload.returned_after_midnight,
            customer_id=payload.customer_id,
            pickup_datetime=payload.pickup_datetime,
        )
        # Every blocking reason at once: an agent fixing a form should not have
        # to resubmit four times to learn four things.
        if package_quote.errors:
            raise ValidationException(" ".join(v.message for v in package_quote.errors))
        if (
            payload.vehicle_category_id
            and payload.vehicle_category_id != package_quote.vehicle_category_id
        ):
            raise ValidationException(
                "That vehicle category does not match the priced rate class. "
                f"{package_quote.vehicle_class} books as category "
                f"{package_quote.vehicle_category_id}."
            )
        trip_type = package_quote.rental_type
        vehicle_category_id = package_quote.vehicle_category_id
        estimated_amount = package_quote.total
        distance_km = package_quote.km
        distance_source = "PACKAGE"
        billable_km = package_quote.km
        quote_notes = [v.message for v in package_quote.validations]
        fare_cols = rental_fare_columns(package_quote)
    else:
        if not payload.vehicle_category_id:
            raise ValidationException("Choose a vehicle category.")
        quote = await quote_cab_trip(
            db,
            pickup_location=payload.pickup_location,
            drop_location=payload.drop_location,
            origin_city_id=effective_city_id,
            vehicle_category_id=payload.vehicle_category_id,
            trip_type=payload.trip_type,
            pickup_datetime=payload.pickup_datetime,
            return_datetime=payload.return_datetime,
            destination_city_id=payload.destination_city_id,
            waiting_minutes=payload.waiting_minutes,
            stops=[s.name for s in payload.stops if s.name],
        )
        trip_type = payload.trip_type
        vehicle_category_id = payload.vehicle_category_id
        estimated_amount = quote.total
        distance_km = quote.distance.km
        distance_source = quote.distance.source
        billable_km = Decimal(str(quote.breakdown["billable_km"]))
        quote_notes = quote.notes
        fare_cols = fare_columns(quote)
    # Pre-coupon, matching the website: master_bookings.total_amount stays the
    # fare view and the discount lives on cab_bookings + coupon_usages, so the
    # invoice, the settlement line and the partner close-trip math all read the
    # discount from one place and subtract it once.

    # ── Coupon, through the website's validator ────────────────────────
    # Deliberately not a second rule set: `validate_coupon(reserve=True)` is the
    # function that decides whether a redemption is accepted at all, including
    # the per-customer history and the usage cap under a row lock. The quote
    # screen recommended from that same function, so a code listed there either
    # still works here or is refused with the reason the validator gave.
    coupon_discount = Decimal("0.00")
    coupon_id_used: Optional[int] = None
    coupon_code_used: Optional[str] = None
    offer_id_used: Optional[int] = None
    coupon_validation = None
    if payload.coupon_code:
        from app.modules.admin.coupon_api import ValidateRequest, validate_coupon
        from app.modules.admin.offer_service import resolve_offer_for_booking

        coupon_validation = await validate_coupon(
            ValidateRequest(
                coupon_code=payload.coupon_code,
                service_type="CAB",
                vehicle_category_id=vehicle_category_id,
                city_id=effective_city_id,
                booking_amount=estimated_amount,
                customer_id=payload.customer_id,
                customer_mobile=customer.get("mobile_number"),
                pickup_datetime=payload.pickup_datetime,
                return_datetime=payload.return_datetime,
            ),
            db,
            reserve=True,
        )
        if not coupon_validation.valid:
            # Same code the website path returns for the same event: the coupon
            # was real a moment ago and is not now (used up, expired, or this
            # customer has had it).
            raise HTTPException(
                status_code=409,
                detail=f"Coupon not applied: {coupon_validation.message}",
            )
        coupon_discount = Decimal(str(coupon_validation.discount_amount))
        coupon_id_used = coupon_validation.coupon_id
        coupon_code_used = payload.coupon_code.strip().upper()
        offer_id_used = await resolve_offer_for_booking(
            db,
            coupon_id=coupon_id_used,
            service_type="CAB",
            vehicle_category_id=vehicle_category_id,
            hotel_id=None,
            room_category_id=None,
            tour_package_id=None,
        )

    # Create master booking via ORM so the booking_number, uuid and FK
    # defaults are populated through the same path the model declares.
    # Doc Ref: BRD Part 3 §20 — booking numbers via MAX-based sequence.
    from app.modules.booking.services.numbering import (
        next_master_booking_number,
        next_cab_booking_number,
    )

    mbk_number = await next_master_booking_number(db)

    created_by_uuid = None
    try:
        created_by_uuid = _uuid.UUID(current_user["sub"])
    except (KeyError, ValueError, TypeError):
        created_by_uuid = None

    mb = MasterBooking(
        uuid=_uuid.uuid4(),
        booking_number=mbk_number,
        customer_id=payload.customer_id,
        city_id=effective_city_id,
        booking_status="CONFIRMED",
        payment_status="PENDING",
        total_amount=estimated_amount,
        journey_start_date=payload.pickup_datetime.date(),
        remarks=payload.remarks or "Booking created by admin via Customer Care",
    )
    db.add(mb)
    await db.flush()  # populate mb.id for the FK on cab_bookings

    # Create cab booking
    cb_number = await next_cab_booking_number(db)
    cb = CabBooking(
        uuid=_uuid.uuid4(),
        master_booking_id=mb.id,
        booking_number=cb_number,
        trip_type=trip_type,
        vehicle_category_id=vehicle_category_id,
        pickup_location=payload.pickup_location,
        pickup_latitude=payload.pickup_latitude,
        pickup_longitude=payload.pickup_longitude,
        drop_location=payload.drop_location,
        drop_latitude=payload.drop_latitude,
        drop_longitude=payload.drop_longitude,
        # The stops explain the measured distance, so they belong on the row
        # beside it rather than only in the agent's memory of the call.
        stops=[s.model_dump() for s in payload.stops if s.name],
        estimated_duration_minutes=(
            None
            if mode == "PACKAGE"
            else estimate_duration_minutes(float(distance_km), trip_type)
        ),
        pickup_datetime=payload.pickup_datetime,
        return_datetime=payload.return_datetime,
        # What the trip measured, as opposed to what will be billed: the billable
        # figure is the measured one or the trip type's minimum, whichever is
        # larger, and the difference is exactly what a customer argues about.
        estimated_distance=distance_km,
        estimated_amount=estimated_amount,
        coupon_code=coupon_code_used,
        coupon_id=coupon_id_used,
        coupon_discount=coupon_discount,
        offer_id=offer_id_used,
        **fare_cols,
        booking_status="PENDING_ASSIGNMENT",
    )
    db.add(cb)
    await db.flush()  # populate cb.id for booking_services.service_reference_id

    # The redemption row is the source of truth for the usage counter, the
    # invoice discount and the funding split, so it is written here exactly as
    # the website writes it.
    if coupon_id_used is not None:
        from app.modules.admin.coupon_api import record_coupon_usage

        await record_coupon_usage(
            db,
            validation=coupon_validation,
            customer_id=payload.customer_id,
            master_booking_id=mb.id,
            cab_booking_id=cb.id,
        )

    # Link the CAB service to the master booking
    bs = BookingService(
        master_booking_id=mb.id,
        service_type="CAB",
        service_reference_id=cb.id,
        service_status="PENDING_ASSIGNMENT",
        service_amount=estimated_amount,
    )
    db.add(bs)

    # Audit trail. The fare and how it was measured go in the description, not
    # only on the booking row, because the question asked months later is "what
    # did we tell this customer and why" — and the answer is a timestamped
    # sentence, not a re-derivation from rules that may since have changed.
    tl = BookingTimeline(
        master_booking_id=mb.id,
        event_type="BOOKING_CREATED",
        event_description=(
            "Booking created by admin via Customer Care — "
            f"₹{estimated_amount.quantize(Decimal('0.01'))} for "
            f"{_km_text(distance_km)} km ({distance_source})"
            + (
                f" on the {package_quote.vehicle_class} "
                f"{package_quote.applied.package_hours}h/"
                f"{package_quote.applied.package_km}km package"
                if package_quote
                else ""
            )
            + (
                f", coupon {coupon_code_used} −₹{coupon_discount.quantize(Decimal('0.01'))}"
                if coupon_code_used
                else ""
            )
        ),
        created_by=created_by_uuid,
    )
    db.add(tl)

    # The customer has agreed to the trip on the phone; with auto-assign
    # switched on the partners are asked now instead of waiting for someone to
    # open the assign screen, and with it off this is a no-op.
    from app.modules.booking.services.auto_assign import (
        begin_chain,
        notify_offered_partners,
    )

    # The booking and its rows are still pending in this session (autoflush is
    # off) and the chain reads/writes them with SQL.
    await db.flush()
    offered_partners = await begin_chain(db, cb, city_id=mb.city_id)

    # ── Email: the booking the agent just described on the phone ──────────
    # Only the customer's own address on their own profile, and only what they
    # already agreed to hear: the booking number, the route and the fare lines.
    #
    # The email engine never raises (failures land in `email_logs` for an admin
    # to resend), and the wrapped call keeps that promise even if importing or
    # rendering breaks: a booking the customer already agreed to must not fail
    # because SMTP is down.
    try:
        from app.infrastructure.email import send_event_email

        if customer.get("email"):
            await send_event_email(
                db,
                event_type="booking_confirmed",
                to_email=customer["email"],
                to_name=customer.get("full_name") or None,
                context=_cab_confirmation_context(
                    package_quote or quote,
                    package=package_quote is not None,
                    booking_number=cb_number,
                    trip_type=trip_type,
                    pickup_location=payload.pickup_location,
                    drop_location=payload.drop_location,
                    pickup_datetime=payload.pickup_datetime,
                    coupon_code=coupon_code_used,
                    coupon_discount=coupon_discount,
                ),
                related_type="CAB_BOOKING",
                related_id=cb.id,
            )
    except Exception:  # pragma: no cover — email must never break a booking
        pass

    # get_db auto-commits on success, and the popups go out after the rows that
    # the partners' Accept buttons act on are actually in the database.
    await db.commit()
    if offered_partners:
        await notify_offered_partners(db, cb, offered_partners)

    return CabBookingCreateOut(
        success=True,
        master_booking_id=mb.id,
        booking_number=mbk_number,
        cab_booking_id=cb.id,
        cab_booking_number=cb_number,
        distance_km=distance_km,
        distance_source=distance_source,
        billable_km=billable_km,
        estimated_amount=estimated_amount,
        coupon_discount=coupon_discount,
        payable_amount=estimated_amount - coupon_discount,
        notes=quote_notes,
        message=(
            "Cab booking created successfully"
            + (
                f" — offered to {len(offered_partners)} partner(s), awaiting "
                "acceptance"
                if offered_partners
                else ""
            )
        ),
    )


@router.put(
    "/cab-booking/{cab_booking_id}",
    response_model=CabBookingEditOut,
    tags=["Customer Care"],
)
async def edit_cab_booking_by_admin(
    cab_booking_id: int,
    payload: CabBookingEditByAdmin,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
    scope: BranchScope = Depends(get_branch_scope),
):
    """Change a booked cab trip to the customer's new requirement, and reprice it.

    The call the desk actually takes — "make it 11 instead of 9", "drop my wife at
    the airport then come back", "put me in an Innova instead". The old route for
    this (`/reschedule` on the booking screen) moves the pickup time and leaves the
    fare exactly as it was, so a trip that grew from 12 km to 60 km kept its
    original price and the partner was billed for the shorter job.

    Nothing here is typed money. The amended trip is measured and priced by the
    same `quote_cab_trip` call a new booking goes through, the lines and the rule
    sheet are rewritten on the row, and the old total goes into the audit trail
    next to the new one so a dispute has both figures and the acting user.

    Three things stop an edit, and each says what to do instead: the trip has
    started or ended (close or cancel it, do not amend it), money has been taken
    against it (reconcile the payment first), or the coupon on it would owe a
    different discount on the new fare (a booked redemption cannot be re-valued
    here). Once a partner has accepted, the offer is a commitment about this trip,
    so an edit that changes the fare or the kind of trip closes the open offers and
    puts the booking back for assignment rather than letting a partner deliver a
    job nobody quoted them.
    """
    from app.modules.booking.services.trip_guard import estimate_duration_minutes
    from app.modules.booking.services.trip_pricing import fare_columns, quote_cab_trip

    cb = (
        (
            await db.execute(
                text(
                    "SELECT cb.id, cb.master_booking_id, cb.booking_number, "
                    "       cb.booking_status, cb.trip_type, "
                    "       cb.vehicle_category_id, cb.pickup_location, "
                    "       cb.drop_location, cb.pickup_datetime, "
                    "       cb.return_datetime, cb.estimated_amount, "
                    "       cb.coupon_id, cb.coupon_code, cb.coupon_discount, "
                    "       cb.invoice_number, cb.final_amount, "
                    "       cb.pending_partner_id, "
                    # Which of the two times moved is decided by the database, on
                    # the same timestamptz column the UPDATE writes: in Python a
                    # naive request time and a stored instant can never compare
                    # equal, and the audit note would claim the pickup changed on
                    # every amendment.
                    "       (cb.pickup_datetime IS NOT DISTINCT FROM :new_pickup) "
                    "         AS pickup_time_unchanged, "
                    "       (cb.return_datetime IS NOT DISTINCT FROM :new_return) "
                    "         AS return_time_unchanged "
                    "FROM cab_bookings cb WHERE cb.id = :id"
                ),
                {
                    "id": cab_booking_id,
                    "new_pickup": payload.pickup_datetime,
                    "new_return": payload.return_datetime,
                },
            )
        )
        .mappings()
        .one_or_none()
    )
    if cb is None:
        raise ResourceNotFoundException("Cab booking not found")

    # The branch that owns the master booking owns the amendment, checked before
    # anything about the booking's state is reported — another office's booking
    # reads as absent rather than as interesting.
    await require_row_scope(
        db,
        scope,
        table="master_bookings",
        id_column="id",
        id_value=cb["master_booking_id"],
        city_column="city_id",
        allow_unassigned=False,
        label="Cab booking",
    )

    status = str(cb["booking_status"] or "").upper()
    if status not in {"PENDING_ASSIGNMENT", "PENDING_PARTNER_ACCEPTANCE"}:
        raise HTTPException(
            status_code=409,
            detail=(
                f"This booking is {status or 'in an unknown state'}, so the trip "
                "cannot be amended here. Once a partner has accepted, changes go "
                "through cancel / re-assign; after the trip has run, through "
                "close-trip and add-charges."
            ),
        )

    if cb["final_amount"] is not None or cb["invoice_number"] is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                "This booking already has a closed fare or an invoice against it, "
                "so the money is settled — amend it with add-charges / the "
                "invoice void flow instead of rewriting the fare."
            ),
        )

    advance = (
        (
            await db.execute(
                text(
                    "SELECT amount FROM advance_payments "
                    "WHERE cab_booking_id = :id AND status = 'ACTIVE' "
                    "ORDER BY id LIMIT 1"
                ),
                {"id": cab_booking_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if advance is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"₹{Decimal(str(advance['amount'])).quantize(Decimal('0.01'))} has "
                "been collected against this booking. Refund or void the advance "
                "first, then amend the trip — otherwise the balance the driver "
                "collects at trip end is computed on a fare the customer was "
                "never quoted."
            ),
        )

    mb = (
        (
            await db.execute(
                text(
                    "SELECT id, booking_number, city_id, customer_id, payment_status "
                    "FROM master_bookings WHERE id = :id"
                ),
                {"id": cb["master_booking_id"]},
            )
        )
        .mappings()
        .one()
    )

    # A branch amends in its own city; a super admin may move the trip, which is
    # also what re-files the booking under the new office.
    effective_city_id = await _desk_pricing_city(payload, db, scope)
    _assert_trip_times(payload.pickup_datetime, payload.return_datetime)

    await _guard_trip_type(
        db,
        trip_type=payload.trip_type,
        pickup_location=payload.pickup_location,
        drop_location=payload.drop_location,
        stops=payload.stops,
        pickup_datetime=payload.pickup_datetime,
        return_datetime=payload.return_datetime,
        # An existing One Way trip still has to be editable after the product
        # is switched off; changing a different trip *into* One Way does not.
        enforce_availability=(
            str(cb["trip_type"] or "").upper() != payload.trip_type.upper()
        ),
    )

    quote = await quote_cab_trip(
        db,
        pickup_location=payload.pickup_location,
        drop_location=payload.drop_location,
        origin_city_id=effective_city_id,
        vehicle_category_id=payload.vehicle_category_id,
        trip_type=payload.trip_type,
        pickup_datetime=payload.pickup_datetime,
        return_datetime=payload.return_datetime,
        destination_city_id=payload.destination_city_id,
        waiting_minutes=payload.waiting_minutes,
        stops=[s.name for s in payload.stops if s.name],
    )
    old_amount = Decimal(str(cb["estimated_amount"] or 0))
    coupon_discount = Decimal(str(cb["coupon_discount"] or 0))

    # A redemption stands or the amendment is refused — never re-valued. The
    # discount, the usage row and the partner/platform funding split were written
    # for the fare the customer agreed to, and `here not have adjust any
    # commission amount` is the owner's rule, not a convenience.
    if cb["coupon_id"] is not None:
        from app.modules.admin.coupon_api import ValidateRequest, validate_coupon

        mobile = (
            await db.execute(
                text(
                    "SELECT u.mobile_number FROM customers c "
                    "JOIN users u ON u.id = c.user_id WHERE c.id = :cid"
                ),
                {"cid": mb["customer_id"]},
            )
        ).scalar()

        revalidation = await validate_coupon(
            ValidateRequest(
                coupon_code=cb["coupon_code"],
                service_type="CAB",
                vehicle_category_id=payload.vehicle_category_id,
                city_id=effective_city_id,
                booking_amount=quote.total,
                customer_id=mb["customer_id"],
                customer_mobile=mobile,
                pickup_datetime=payload.pickup_datetime,
                return_datetime=payload.return_datetime,
            ),
            db,
            reserve=False,
        )
        if not revalidation.valid:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Coupon {cb['coupon_code']} no longer applies to the amended "
                    f"trip: {revalidation.message}. Cancel and re-book to change "
                    "the coupon, or amend a trip that still qualifies."
                ),
            )
        would_be = Decimal(str(revalidation.discount_amount or 0))
        if would_be != coupon_discount:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Coupon {cb['coupon_code']} would owe ₹"
                    f"{would_be.quantize(Decimal('0.01'))} on the new fare, not the "
                    f"₹{coupon_discount.quantize(Decimal('0.01'))} already redeemed. "
                    "A booked coupon cannot be re-valued here — cancel the booking "
                    "and re-book with the code."
                ),
            )

    # The offers a partner has not accepted yet are about the OLD trip. If this
    # edit changes what the job is, they are closed and the booking goes back for
    # assignment so nobody accepts a fare they were never offered.
    material_change = (
        quote.total != old_amount
        or str(cb["trip_type"] or "").upper() != payload.trip_type.upper()
        or int(cb["vehicle_category_id"]) != payload.vehicle_category_id
        or (mb["city_id"] is not None and int(mb["city_id"]) != int(effective_city_id))
    )
    re_offered = False
    new_status = status
    if material_change and status == "PENDING_PARTNER_ACCEPTANCE":
        await db.execute(
            text(
                "UPDATE cab_booking_assignments "
                "SET closed_at = NOW(), close_reason = 'ADMIN_REASSIGN', "
                "    rejected_at = NOW(), rejection_reason_code = 'ADMIN_REASSIGN', "
                "    acceptance_deadline = NULL "
                "WHERE cab_booking_id = :id AND closed_at IS NULL "
                "  AND accepted_at IS NULL"
            ),
            {"id": cab_booking_id},
        )
        new_status = "PENDING_ASSIGNMENT"
        re_offered = True

    fare_cols = fare_columns(quote)
    await db.execute(
        text(
            """
        UPDATE cab_bookings SET
            trip_type = :trip_type,
            vehicle_category_id = :vcid,
            pickup_location = :pickup_loc, pickup_latitude = :pickup_lat,
            pickup_longitude = :pickup_lng,
            drop_location = :drop_loc, drop_latitude = :drop_lat,
            drop_longitude = :drop_lng,
            stops = CAST(:stops AS JSONB),
            estimated_duration_minutes = :duration_minutes,
            pickup_datetime = :pickup_dt, return_datetime = :return_dt,
            estimated_distance = :distance_km, estimated_amount = :total,
            booking_status = :new_status,
            updated_at = NOW(),
            pricing_city_id = :pricing_city_id,
            distance_source = :distance_source,
            billing_distance_km = :billing_distance_km,
            base_fare_amount = :base_fare_amount,
            distance_charge_amount = :distance_charge_amount,
            driver_allowance_amount = :driver_allowance_amount,
            night_charge_amount = :night_charge_amount,
            toll_amount = :toll_amount,
            waiting_charge_amount = :waiting_charge_amount,
            rate_snapshot = CAST(:rate_snapshot AS JSONB)
        WHERE id = :id
    """
        ),
        {
            "id": cab_booking_id,
            "trip_type": payload.trip_type,
            "vcid": payload.vehicle_category_id,
            "pickup_loc": payload.pickup_location,
            "pickup_lat": payload.pickup_latitude,
            "pickup_lng": payload.pickup_longitude,
            "drop_loc": payload.drop_location,
            "drop_lat": payload.drop_latitude,
            "drop_lng": payload.drop_longitude,
            "stops": json.dumps(
                [s.model_dump() for s in payload.stops if s.name], default=str
            ),
            "duration_minutes": estimate_duration_minutes(
                float(quote.distance.km), payload.trip_type
            ),
            "pickup_dt": payload.pickup_datetime,
            "return_dt": payload.return_datetime,
            "distance_km": quote.distance.km,
            "total": quote.total,
            "new_status": new_status,
            **fare_cols,
            "rate_snapshot": json.dumps(fare_cols["rate_snapshot"], default=str),
        },
    )
    if re_offered:
        # The offer this booking was sitting on is gone with the other ones.
        await db.execute(
            text(
                "UPDATE cab_bookings SET pending_partner_id = NULL, "
                "       acceptance_deadline = NULL WHERE id = :id"
            ),
            {"id": cab_booking_id},
        )

    # The master booking and the service link carry the fare the customer is
    # billed, so they move with it — the same three writes a create makes.
    await db.execute(
        text(
            "UPDATE master_bookings SET total_amount = :amount, "
            "       journey_start_date = :jdate, city_id = :city_id, "
            "       updated_at = NOW() "
            "WHERE id = :mb_id"
        ),
        {
            "amount": quote.total,
            "jdate": payload.pickup_datetime.date(),
            "city_id": effective_city_id,
            "mb_id": mb["id"],
        },
    )
    # `service_reference_id` is a polymorphic id — a hotel reservation and a cab
    # booking can share a number — so the row is found through its master booking.
    await db.execute(
        text(
            "UPDATE booking_services SET service_amount = :amount, "
            "       service_status = :new_status "
            "WHERE master_booking_id = :mb_id AND service_type = 'CAB' "
            "  AND service_reference_id = :cb_id"
        ),
        {
            "amount": quote.total,
            "new_status": new_status,
            "mb_id": mb["id"],
            "cb_id": cab_booking_id,
        },
    )

    await db.execute(
        text(
            "INSERT INTO booking_timelines "
            "       (master_booking_id, event_type, event_description, "
            "        event_timestamp, created_by) "
            "VALUES (:mb_id, 'BOOKING_MODIFIED', :detail, NOW(), :by)"
        ),
        {
            "mb_id": mb["id"],
            "by": _timeline_actor(current_user),
            "detail": _cab_amendment_note(cb, payload, quote, re_offered),
        },
    )

    from app.modules.notification.services.booking_notifications import (
        notify_booking_updated,
    )

    await notify_booking_updated(
        db,
        service_type="CAB",
        master_booking_id=mb["id"],
        service_id=cab_booking_id,
        booking_number=cb["booking_number"],
        service_status=new_status,
        action="AMENDED",
        payment_status=mb["payment_status"],
        partner_ids=[cb["pending_partner_id"]] if cb["pending_partner_id"] else None,
    )

    # get_db auto-commits on success.

    return CabBookingEditOut(
        success=True,
        master_booking_id=mb["id"],
        booking_number=mb["booking_number"],
        cab_booking_id=cab_booking_id,
        cab_booking_number=cb["booking_number"],
        distance_km=quote.distance.km,
        distance_source=quote.distance.source,
        billable_km=Decimal(str(quote.breakdown["billable_km"])),
        old_amount=old_amount,
        estimated_amount=quote.total,
        coupon_discount=coupon_discount,
        payable_amount=quote.total - coupon_discount,
        re_offered=re_offered,
        notes=quote.notes,
        message="Cab booking amended successfully",
    )


# ── Hotel booking helpers + endpoints ────────────────────────
#
# Hotel money is materially harder than cab fare: it is priced per night, with
# date-ranged rate plans, per-date inventory overrides, tariff-based GST slabs
# and a resolved commission config. So — unlike cab, which the client can
# estimate — the stay is ALWAYS priced server-side through the shared hotel
# pricing engine (app/modules/hotel/services/pricing.py). This guarantees an
# admin-created booking is priced by exactly the same rules as a customer one.


async def _load_active_hotel(
    db: AsyncSession, hotel_id: int, scope: BranchScope
) -> Hotel:
    """Fetch the Hotel ORM row (needed by resolve_hotel_commission and for
    tax_mode). Only ACTIVE, non-deleted hotels are bookable.

    The scope check is not decoration: `/hotel-booking/meta` only offers a branch
    its own city's hotels, but the id arrives from the request body, so a pasted
    one has to fail the same way it does for a hotel that does not exist (404).
    """
    hotel = (
        await db.execute(
            select(Hotel).where(Hotel.id == hotel_id, Hotel.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if hotel is None:
        raise HTTPException(status_code=404, detail="Hotel not found")
    if scope.restricted and hotel.city_id not in scope.cities:
        raise HTTPException(status_code=404, detail="Hotel not found")
    if str(hotel.status) != "ACTIVE":
        raise HTTPException(status_code=400, detail="Hotel is not active / bookable")
    return hotel


async def _compute_hotel_quote(
    db: AsyncSession,
    hotel: Hotel,
    room_category_id: int,
    check_in: date,
    check_out: date,
    rooms_count: int,
    adults_count: int = 1,
    children_count: int = 0,
    extra_beds: int = 0,
) -> dict:
    """Thin wrapper — the actual pricing/commission math lives in
    `app.modules.hotel.services.quote.compute_hotel_quote` so the switch /
    split-stay service (migration 0042_hotel_switch) can quote a target stay
    without copying the formula. New code should call the service directly.
    """
    from app.modules.hotel.services.quote import compute_hotel_quote

    return await compute_hotel_quote(
        db,
        hotel,
        room_category_id,
        check_in,
        check_out,
        rooms_count,
        adults_count=adults_count,
        children_count=children_count,
        extra_beds=extra_beds,
    )


def _quote_to_response(q: dict) -> dict:
    """Money → float for the JSON wire; keep the human-readable per-night list."""
    return {
        "hotel_id": q["hotel_id"],
        "hotel_name": q["hotel_name"],
        "room_category_id": q["room_category_id"],
        "room_category_name": q["room_category_name"],
        "check_in_date": q["check_in_date"].isoformat(),
        "check_out_date": q["check_out_date"].isoformat(),
        "nights": q["nights"],
        "rooms_count": q["rooms_count"],
        "room_nights": q["room_nights"],
        "base_amount": float(q["base_amount"]),
        "taxable_amount": float(q["taxable_amount"]),
        "gst_percent": float(q["gst_percent"]),
        "gst_amount": float(q["gst_amount"]),
        "is_tax_invoice": q["is_tax_invoice"],
        "total_amount": float(q["total_amount"]),
        "platform_commission": float(q["platform_commission"]),
        "partner_payout": float(q["partner_payout"]),
        "average_nightly_rate": float(q["average_nightly_rate"]),
        "nightly": q["nightly"],
        "occupancy": q.get("occupancy"),
    }


# ── 8a. Hotel booking meta (active hotels + room categories) ──
@router.get("/hotel-booking/meta", tags=["Customer Care"])
async def hotel_booking_meta(
    city_id: Optional[int] = Query(None),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
    scope: BranchScope = Depends(get_branch_scope),
):
    """Active, bookable hotels with their active room categories, for the admin
    hotel-booking form. Optionally filtered by city."""
    where = ["h.status = 'ACTIVE'", "h.deleted_at IS NULL"]
    params: dict = {}
    if city_id:
        where.append("h.city_id = :city_id")
        params["city_id"] = city_id

    # hotels.city_id is NOT NULL, so allow_unassigned=False; a branch picks only
    # hotels in its own city, which bounds the hotel-booking flow downstream.
    hotel_scope, hotel_params = city_scope_sql(
        scope, "h.city_id", allow_unassigned=False
    )
    where.append(f"({hotel_scope})")
    params.update(hotel_params)
    where_sql = " AND ".join(where)

    hotels_r = await db.execute(
        text(
            f"""
        SELECT h.id, h.hotel_code, h.hotel_name, h.star_rating,
               h.city_id, ci.name AS city_name, h.address, h.tax_mode
        FROM hotels h
        LEFT JOIN cities ci ON ci.id = h.city_id
        WHERE {where_sql}
        ORDER BY h.hotel_name
    """
        ),
        params,
    )
    hotels = [dict(r) for r in hotels_r.mappings().all()]

    hotel_ids = [h["id"] for h in hotels]
    cats_by_hotel: dict = {}
    if hotel_ids:
        cats_r = await db.execute(
            text(
                """
            SELECT id, hotel_id, category_name, room_type, meal_plan,
                   base_occupancy, max_adults, max_children, max_occupancy,
                   extra_bed_allowed, extra_bed_charge,
                   extra_adult_charge, extra_child_charge,
                   CAST(base_price AS FLOAT) AS base_price,
                   CAST(published_price AS FLOAT) AS published_price,
                   total_rooms
            FROM hotel_room_categories
            WHERE hotel_id = ANY(:hids) AND is_active = TRUE
            ORDER BY display_order, category_name
        """
            ),
            {"hids": hotel_ids},
        )
        for r in cats_r.mappings().all():
            cats_by_hotel.setdefault(r["hotel_id"], []).append(dict(r))

    for h in hotels:
        h["room_categories"] = cats_by_hotel.get(h["id"], [])

    city_scope_clause, city_scope_params = city_scope_sql(
        scope, "id", allow_unassigned=False
    )
    cities_r = await db.execute(
        text(
            f"SELECT id, name FROM cities WHERE is_active = TRUE AND ({city_scope_clause}) ORDER BY name"
        ),
        city_scope_params,
    )
    cities = [dict(r) for r in cities_r.mappings().all()]

    return {"hotels": hotels, "cities": cities}


# ── 8b. Hotel booking quote (server-side price preview) ───────
@router.post("/hotel-booking/quote", tags=["Customer Care"])
async def hotel_booking_quote(
    payload: HotelBookingQuoteRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
    scope: BranchScope = Depends(get_branch_scope),
):
    """Price a stay without creating anything — drives the admin form's live
    fare panel."""
    hotel = await _load_active_hotel(db, payload.hotel_id, scope)
    quote = await _compute_hotel_quote(
        db,
        hotel,
        payload.room_category_id,
        payload.check_in_date,
        payload.check_out_date,
        payload.rooms_count,
        payload.adults_count,
        payload.children_count,
        payload.extra_beds,
    )
    return {"success": True, **_quote_to_response(quote)}


# ── 8c. Create hotel booking by admin (on behalf of customer) ─
@router.post("/hotel-booking", tags=["Customer Care"])
async def create_hotel_booking_by_admin(
    payload: HotelBookingCreateByAdmin,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
    scope: BranchScope = Depends(get_branch_scope),
):
    """Admin creates a hotel reservation on behalf of a customer. Prices the
    stay server-side, writes the master booking + reservation + service link +
    timeline, and decrements per-night inventory (guarded by the DB
    CHECK(available_rooms >= 0) constraint)."""

    # Verify the customer exists. The booking's branch is the hotel's city, not
    # the customer's — a Bhubaneswar regular staying in a Cuttack hotel is
    # Cuttack's work — so their own city_id is not what scopes this route.
    cust_r = await db.execute(
        text("SELECT id, city_id FROM customers WHERE id = :cid"),
        {"cid": payload.customer_id},
    )
    cust = cust_r.mappings().one_or_none()
    if not cust:
        raise HTTPException(status_code=404, detail="Customer not found")

    hotel = await _load_active_hotel(db, payload.hotel_id, scope)
    q = await _compute_hotel_quote(
        db,
        hotel,
        payload.room_category_id,
        payload.check_in_date,
        payload.check_out_date,
        payload.rooms_count,
        payload.adults_count,
        payload.children_count,
        payload.extra_beds,
    )

    # Decrement inventory per night. If a date has no inventory row we skip it
    # (open availability); where a row exists the CHECK constraint enforces
    # oversell protection and surfaces as a 409.
    try:
        upd = await db.execute(
            text(
                """
            UPDATE hotel_inventory
               SET booked_rooms   = booked_rooms + :rooms,
                   available_rooms = available_rooms - :rooms,
                   updated_at = NOW()
             WHERE room_category_id = :cid
               AND inventory_date >= :dfrom AND inventory_date < :dto
               AND is_stop_sell = FALSE
            RETURNING inventory_date
        """
            ),
            {
                "rooms": payload.rooms_count,
                "cid": payload.room_category_id,
                "dfrom": payload.check_in_date,
                "dto": payload.check_out_date,
            },
        )
        _ = upd.fetchall()
    except Exception:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Not enough rooms available for the selected dates",
        )

    # Master booking — hotel journey starts on check-in.
    mbk_number = _booking_number()
    mb_r = await db.execute(
        text(
            """
        INSERT INTO master_bookings (
            uuid, booking_number, customer_id, city_id,
            booking_status, payment_status,
            total_amount, total_paid_amount, total_refund_amount,
            journey_start_date, remarks, created_at, updated_at
        ) VALUES (
            :uuid, :bnum, :cid, :city_id,
            'CONFIRMED', 'PENDING',
            :amount, 0, 0,
            :jdate, :remarks, NOW(), NOW()
        ) RETURNING id
    """
        ),
        {
            "uuid": str(_uuid.uuid4()),
            "bnum": mbk_number,
            "cid": payload.customer_id,
            "city_id": hotel.city_id,
            "amount": q["total_amount"],
            "jdate": payload.check_in_date,
            "remarks": payload.remarks
            or "Hotel booking created by admin via Customer Care",
        },
    )
    mb_id = mb_r.scalar_one()

    # Booking service link (HOTEL).
    svc_r = await db.execute(
        text(
            """
        INSERT INTO booking_services (master_booking_id, service_type, service_reference_id, service_status, service_amount, created_at)
        VALUES (:mb_id, 'HOTEL', 0, 'CONFIRMED', :amount, NOW())
        RETURNING id
    """
        ),
        {"mb_id": mb_id, "amount": q["total_amount"]},
    )
    svc_id = svc_r.scalar_one()

    # Hotel reservation.
    res_number = _reservation_number()
    res_r = await db.execute(
        text(
            """
        INSERT INTO hotel_reservations (
            uuid, master_booking_id, booking_service_id, hotel_id, room_category_id,
            customer_id, reservation_number,
            check_in_date, check_out_date, nights, rooms_count, room_nights,
            adults_count, children_count,
            base_amount, extra_charges, discount_amount, taxable_amount,
            gst_percent, gst_amount, is_tax_invoice, total_amount,
            platform_commission, partner_payout,
            rate_snapshot, commission_config_snapshot,
            reservation_status, special_requests, confirmed_at, created_at, updated_at
        ) VALUES (
            :uuid, :mb_id, :svc_id, :hotel_id, :cat_id,
            :cust_id, :res_num,
            :cin, :cout, :nights, :rooms, :room_nights,
            :adults, :children,
            :base, 0, 0, :taxable,
            :gst_pct, :gst_amt, :is_tax_inv, :total,
            :commission, :payout,
            CAST(:rate_snap AS JSONB), CAST(:comm_snap AS JSONB),
            'CONFIRMED', :special, NOW(), NOW(), NOW()
        ) RETURNING id
    """
        ),
        {
            "uuid": str(_uuid.uuid4()),
            "mb_id": mb_id,
            "svc_id": svc_id,
            "hotel_id": payload.hotel_id,
            "cat_id": payload.room_category_id,
            "cust_id": payload.customer_id,
            "res_num": res_number,
            "cin": payload.check_in_date,
            "cout": payload.check_out_date,
            "nights": q["nights"],
            "rooms": payload.rooms_count,
            "room_nights": q["room_nights"],
            "adults": payload.adults_count,
            "children": payload.children_count,
            "base": q["base_amount"],
            "taxable": q["taxable_amount"],
            "gst_pct": q["gst_percent"],
            "gst_amt": q["gst_amount"],
            "is_tax_inv": q["is_tax_invoice"],
            "total": q["total_amount"],
            "commission": q["platform_commission"],
            "payout": q["partner_payout"],
            "rate_snap": json.dumps(q["rate_snapshot"]),
            "comm_snap": json.dumps(q["commission_snapshot"]),
            "special": payload.special_requests,
        },
    )
    res_id = res_r.scalar_one()

    # Point the service link at the reservation now that we have its id.
    await db.execute(
        text("UPDATE booking_services SET service_reference_id = :rid WHERE id = :sid"),
        {"rid": res_id, "sid": svc_id},
    )

    # Guest roster (optional). Ensure at least the customer is primary if none given.
    guests = payload.guests
    if guests:
        for g in guests:
            await db.execute(
                text(
                    """
                INSERT INTO hotel_reservation_guests
                    (reservation_id, guest_name, mobile, gender, age, id_type, id_number, is_primary, created_at)
                VALUES (:rid, :name, :mobile, :gender, :age, :id_type, :id_number, :primary, NOW())
            """
                ),
                {
                    "rid": res_id,
                    "name": g.guest_name,
                    "mobile": g.mobile,
                    "gender": g.gender,
                    "age": g.age,
                    "id_type": g.id_type,
                    "id_number": g.id_number,
                    "primary": g.is_primary,
                },
            )

    # Timeline entry.
    await db.execute(
        text(
            """
        INSERT INTO booking_timelines (master_booking_id, event_type, event_description, event_timestamp, created_by)
        VALUES (:mb_id, 'BOOKING_CREATED', 'Hotel booking created by admin via Customer Care', NOW(), :by)
    """
        ),
        {"mb_id": mb_id, "by": str(current_user["sub"])},
    )

    await db.commit()

    return {
        "success": True,
        "master_booking_id": mb_id,
        "booking_number": mbk_number,
        "reservation_id": res_id,
        "reservation_number": res_number,
        "total_amount": float(q["total_amount"]),
        "nights": q["nights"],
        "rooms_count": payload.rooms_count,
        "message": "Hotel booking created successfully",
    }


# ── 8d. Edit an existing (still-confirmed) hotel booking ──────
@router.put("/hotel-booking/{reservation_id}", tags=["Customer Care"])
async def edit_hotel_booking_by_admin(
    reservation_id: int,
    payload: HotelBookingEditByAdmin,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
    scope: BranchScope = Depends(get_branch_scope),
):
    """Change a confirmed reservation to the customer's new requirement (dates,
    rooms, room category, occupancy, extra beds). Only permitted while the stay
    has not started — once checked-in/out, cancelled or invoiced the money is in
    play and edits go through the check-out/charge flows instead. Re-prices the
    stay, moves inventory off the old window onto the new one, and rewrites the
    reservation + master booking + service link. Advances are left untouched;
    the balance is reconciled against them at check-out."""

    hr = (
        (
            await db.execute(
                text(
                    "SELECT id, master_booking_id, booking_service_id, hotel_id, "
                    "       room_category_id, customer_id, reservation_number, "
                    "       check_in_date, check_out_date, rooms_count, reservation_status "
                    "FROM hotel_reservations WHERE id = :id"
                ),
                {"id": reservation_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if not hr:
        raise HTTPException(status_code=404, detail="Reservation not found")

    # The stay belongs to the branch its master booking is filed under, which is
    # the hotel's city. Checked before the status rule so another office's
    # reservation reads as absent rather than as a booking with an interesting
    # status to probe.
    await require_row_scope(
        db,
        scope,
        table="master_bookings",
        id_column="id",
        id_value=hr["master_booking_id"],
        city_column="city_id",
        allow_unassigned=False,
        label="Hotel reservation",
    )

    res_status = str(hr["reservation_status"] or "").upper()
    if res_status != "CONFIRMED":
        raise HTTPException(
            status_code=409,
            detail=(
                f"Only a CONFIRMED booking can be edited here (this one is "
                f"{res_status or 'UNKNOWN'}). Use check-out / add-charges instead."
            ),
        )

    hotel = await _load_active_hotel(db, int(hr["hotel_id"]), scope)
    q = await _compute_hotel_quote(
        db,
        hotel,
        payload.room_category_id,
        payload.check_in_date,
        payload.check_out_date,
        payload.rooms_count,
        payload.adults_count,
        payload.children_count,
        payload.extra_beds,
    )

    old_cat = int(hr["room_category_id"])
    old_rooms = int(hr["rooms_count"] or 1)
    old_cin = hr["check_in_date"]
    old_cout = hr["check_out_date"]

    inventory_changed = (
        old_cat != payload.room_category_id
        or old_rooms != payload.rooms_count
        or old_cin != payload.check_in_date
        or old_cout != payload.check_out_date
    )

    if inventory_changed:
        # Release the old hold first, then take the new one under the same
        # oversell CHECK constraint that guards creation.
        await db.execute(
            text(
                """
            UPDATE hotel_inventory
               SET booked_rooms    = GREATEST(booked_rooms - :rooms, 0),
                   available_rooms  = available_rooms + :rooms,
                   updated_at = NOW()
             WHERE room_category_id = :cid
               AND inventory_date >= :dfrom AND inventory_date < :dto
        """
            ),
            {
                "rooms": old_rooms,
                "cid": old_cat,
                "dfrom": old_cin,
                "dto": old_cout,
            },
        )
        try:
            await db.execute(
                text(
                    """
                UPDATE hotel_inventory
                   SET booked_rooms   = booked_rooms + :rooms,
                       available_rooms = available_rooms - :rooms,
                       updated_at = NOW()
                 WHERE room_category_id = :cid
                   AND inventory_date >= :dfrom AND inventory_date < :dto
                   AND is_stop_sell = FALSE
            """
                ),
                {
                    "rooms": payload.rooms_count,
                    "cid": payload.room_category_id,
                    "dfrom": payload.check_in_date,
                    "dto": payload.check_out_date,
                },
            )
        except Exception:
            await db.rollback()
            raise HTTPException(
                status_code=409,
                detail="Not enough rooms available for the selected dates",
            )

    await db.execute(
        text(
            """
        UPDATE hotel_reservations SET
            room_category_id = :cat_id,
            check_in_date = :cin, check_out_date = :cout,
            nights = :nights, rooms_count = :rooms, room_nights = :room_nights,
            adults_count = :adults, children_count = :children,
            base_amount = :base, taxable_amount = :taxable,
            gst_percent = :gst_pct, gst_amount = :gst_amt,
            is_tax_invoice = :is_tax_inv, total_amount = :total,
            platform_commission = :commission, partner_payout = :payout,
            rate_snapshot = CAST(:rate_snap AS JSONB),
            commission_config_snapshot = CAST(:comm_snap AS JSONB),
            special_requests = :special,
            updated_at = NOW()
        WHERE id = :rid
    """
        ),
        {
            "rid": reservation_id,
            "cat_id": payload.room_category_id,
            "cin": payload.check_in_date,
            "cout": payload.check_out_date,
            "nights": q["nights"],
            "rooms": payload.rooms_count,
            "room_nights": q["room_nights"],
            "adults": payload.adults_count,
            "children": payload.children_count,
            "base": q["base_amount"],
            "taxable": q["taxable_amount"],
            "gst_pct": q["gst_percent"],
            "gst_amt": q["gst_amount"],
            "is_tax_inv": q["is_tax_invoice"],
            "total": q["total_amount"],
            "commission": q["platform_commission"],
            "payout": q["partner_payout"],
            "rate_snap": json.dumps(q["rate_snapshot"]),
            "comm_snap": json.dumps(q["commission_snapshot"]),
            "special": payload.special_requests,
        },
    )

    # Keep the master booking + service link amounts in step with the new total.
    await db.execute(
        text(
            "UPDATE master_bookings "
            "SET total_amount = :amount, journey_start_date = :jdate, updated_at = NOW() "
            "WHERE id = :mb_id"
        ),
        {
            "amount": q["total_amount"],
            "jdate": payload.check_in_date,
            "mb_id": hr["master_booking_id"],
        },
    )
    await db.execute(
        text("UPDATE booking_services SET service_amount = :amount " "WHERE id = :sid"),
        {"amount": q["total_amount"], "sid": hr["booking_service_id"]},
    )

    await db.execute(
        text(
            """
        INSERT INTO booking_timelines (master_booking_id, event_type, event_description, event_timestamp, created_by)
        VALUES (:mb_id, 'BOOKING_MODIFIED', 'Hotel booking amended by admin via Customer Care', NOW(), :by)
    """
        ),
        {"mb_id": hr["master_booking_id"], "by": str(current_user["sub"])},
    )

    await db.commit()

    return {
        "success": True,
        "reservation_id": reservation_id,
        "reservation_number": hr["reservation_number"],
        "total_amount": float(q["total_amount"]),
        "nights": q["nights"],
        "rooms_count": payload.rooms_count,
        "occupancy": q.get("occupancy"),
        "message": "Hotel booking updated successfully",
    }


# ── 9. Stats for customer care dashboard ─────────────────────
@router.get("/stats", tags=["Customer Care"])
async def care_stats(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
    scope: BranchScope = Depends(get_branch_scope),
):
    await _ensure_care_log_table(db)
    # These counts sit on the same rows the care-log list shows — a dashboard
    # badge that ignored the branch would tell a Cuttack manager to expect 40
    # open issues when their queue has 3.
    scope_clause, scope_params = city_scope_sql(scope, "customer_care_logs.city_id")
    r = await db.execute(
        text(
            f"""
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE status = 'OPEN')     AS open_count,
            COUNT(*) FILTER (WHERE status = 'IN_PROGRESS') AS in_progress_count,
            COUNT(*) FILTER (WHERE status = 'RESOLVED') AS resolved_count,
            COUNT(*) FILTER (WHERE status = 'CLOSED')   AS closed_count,
            COUNT(*) FILTER (WHERE priority = 'URGENT') AS urgent_count,
            COUNT(*) FILTER (WHERE DATE(created_at) = CURRENT_DATE) AS today_count
        FROM customer_care_logs
        WHERE ({scope_clause})
    """
        ),
        scope_params,
    )
    row = dict(r.mappings().one())
    return {k: int(v or 0) for k, v in row.items()}
