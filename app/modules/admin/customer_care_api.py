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
from datetime import datetime, timezone, date
from decimal import Decimal
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.modules.auth.models.user import User
from app.modules.booking.models import (
    MasterBooking,
    CabBooking,
    BookingService,
    BookingTimeline,
)

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


class CabBookingCreateByAdmin(BaseModel):
    customer_id: int
    city_id: int
    trip_type: str = Field(
        ..., description="LOCAL | AIRPORT | OUTSTATION | ONE_WAY | ROUND_TRIP"
    )
    vehicle_category_id: int
    pickup_location: str = Field(..., max_length=500)
    pickup_latitude: Optional[float] = None
    pickup_longitude: Optional[float] = None
    drop_location: str = Field(..., max_length=500)
    drop_latitude: Optional[float] = None
    drop_longitude: Optional[float] = None
    pickup_datetime: datetime
    # Return datetime for ROUND_TRIP — used by the fare engine to compute
    # ``trip_days`` when the matched pricing rule has
    # ``driver_allowance_type = PER_DAY``. Added migration 0049.
    return_datetime: Optional[datetime] = None
    estimated_distance: Optional[float] = None
    remarks: Optional[str] = None


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
):
    await _ensure_care_log_table(db)

    where_parts = ["1=1"]
    params: dict = {"limit": page_size, "offset": (page - 1) * page_size}

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
@router.get("/lookup", response_model=CustomerLookupResult, tags=["Customer Care"])
async def lookup_customer(
    mobile: str = Query(..., min_length=10),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    await _ensure_care_log_table(db)

    r = await db.execute(
        text(
            """
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
        WHERE u.mobile_number = :mobile AND u.user_type = 'CUSTOMER'
        LIMIT 1
    """
        ),
        {"mobile": mobile},
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
):
    """Basic customer profile by id — used by the admin booking pages to
    pre-fill the primary guest / passenger with the customer's own details."""
    r = await db.execute(
        text(
            """
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
        LIMIT 1
    """
        ),
        {"cid": customer_id},
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
):
    # Check duplicate
    dup = await db.execute(
        text("SELECT id FROM users WHERE mobile_number = :mob"),
        {"mob": payload.mobile_number},
    )
    if dup.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Mobile number already registered")

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
            "mobile": payload.mobile_number,
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
            "city_id": payload.city_id,
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
):
    await _ensure_care_log_table(db)

    # Bookings
    b_total_r = await db.execute(
        text("SELECT COUNT(*) FROM master_bookings WHERE customer_id = :cid"),
        {"cid": customer_id},
    )
    b_total = b_total_r.scalar() or 0
    b_offset = (booking_page - 1) * page_size

    b_rows = await db.execute(
        text(
            """
        SELECT mb.id, mb.booking_number, mb.booking_status, mb.payment_status,
               CAST(mb.total_amount AS FLOAT) AS total_amount,
               CAST(mb.journey_start_date AS TEXT) AS journey_start_date,
               ci.name AS city_name,
               TO_CHAR(mb.created_at, 'YYYY-MM-DD"T"HH24:MI:SS') AS created_at
        FROM master_bookings mb
        LEFT JOIN cities ci ON ci.id = mb.city_id
        WHERE mb.customer_id = :cid
        ORDER BY mb.created_at DESC
        LIMIT :limit OFFSET :offset
    """
        ),
        {"cid": customer_id, "limit": page_size, "offset": b_offset},
    )
    bookings = [CustomerBookingHistoryOut(**dict(r)) for r in b_rows.mappings().all()]

    # Issues
    i_total_r = await db.execute(
        text("SELECT COUNT(*) FROM customer_care_logs WHERE customer_id = :cid"),
        {"cid": customer_id},
    )
    i_total = i_total_r.scalar() or 0
    i_offset = (issue_page - 1) * page_size

    i_rows = await db.execute(
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
        WHERE cl.customer_id = :cid
        ORDER BY cl.created_at DESC
        LIMIT :limit OFFSET :offset
    """
        ),
        {"cid": customer_id, "limit": page_size, "offset": i_offset},
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
):
    await _ensure_care_log_table(db)

    log_num = _log_number()
    await db.execute(
        text(
            """
        INSERT INTO customer_care_logs
            (log_number, customer_id, issue_type, subject, description, status, priority,
             booking_id, created_by, created_at, updated_at)
        VALUES
            (:log_num, :cid, :itype, :subject, :desc, 'OPEN', :priority,
             :bid, :created_by, NOW(), NOW())
    """
        ),
        {
            "log_num": log_num,
            "cid": payload.customer_id,
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
):
    await _ensure_care_log_table(db)

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
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Returns vehicle categories with pricing for a given city (for admin cab booking form)."""
    cats_r = await db.execute(
        text(
            """
        SELECT id, category_name, seating_capacity, image_url, icon_url
        FROM vehicle_categories WHERE is_active = TRUE ORDER BY display_order, category_name
    """
        )
    )
    cats = [dict(r) for r in cats_r.mappings().all()]

    # Pricing per category per trip_type
    if city_id:
        pricing_r = await db.execute(
            text(
                """
            SELECT vehicle_category_id, trip_type, base_fare, per_km_rate,
                   minimum_km, driver_allowance, night_charge,
                   driver_allowance_type, night_charge_type, toll
            FROM vehicle_pricing_rules
            WHERE city_id = :city_id
        """
            ),
            {"city_id": city_id},
        )
    else:
        pricing_r = await db.execute(
            text(
                """
            SELECT vehicle_category_id, trip_type, base_fare, per_km_rate,
                   minimum_km, driver_allowance, night_charge,
                   driver_allowance_type, night_charge_type, toll
            FROM default_vehicle_pricing_rules
        """
            )
        )

    pricing = {}
    for row in pricing_r.mappings().all():
        key = str(row["vehicle_category_id"])
        if key not in pricing:
            pricing[key] = {}
        pricing[key][row["trip_type"]] = {
            "base_fare": float(row["base_fare"] or 0),
            "per_km_rate": float(row["per_km_rate"] or 0),
            "minimum_km": int(row["minimum_km"] or 0),
            "driver_allowance": float(row["driver_allowance"] or 0),
            "night_charge": float(row["night_charge"] or 0),
            "driver_allowance_type": row["driver_allowance_type"] or "PER_TRIP",
            "night_charge_type": row["night_charge_type"] or "FIXED",
            "toll": float(row["toll"] or 0),
        }

    cities_r = await db.execute(
        text("SELECT id, name FROM cities WHERE is_active = TRUE ORDER BY name")
    )
    cities = [dict(r) for r in cities_r.mappings().all()]

    return {"vehicle_categories": cats, "pricing": pricing, "cities": cities}


# ── 8. Create cab booking by admin (on behalf of customer) ────
@router.post("/cab-booking", tags=["Customer Care"])
async def create_cab_booking_by_admin(
    payload: CabBookingCreateByAdmin,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Admin creates a cab booking on behalf of a customer."""

    # Verify customer exists
    cust_r = await db.execute(
        text("SELECT id FROM customers WHERE id = :cid"), {"cid": payload.customer_id}
    )
    if not cust_r.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Customer not found")

    # Calculate estimated amount from pricing.
    # Doc Ref: BRD Part 3 §35 — fare formula. Centralised in
    # app.modules.booking.services.fare so admin/partner/customer previews
    # all agree on the same number.
    estimated_amount = Decimal("0.00")
    if payload.estimated_distance and payload.estimated_distance > 0:
        pr_r = await db.execute(
            text(
                """
            SELECT base_fare, per_km_rate, minimum_km, driver_allowance, night_charge,
                   driver_allowance_type, night_charge_type, toll
            FROM vehicle_pricing_rules
            WHERE city_id = :cid AND vehicle_category_id = :vcid AND trip_type = :ttype
            LIMIT 1
        """
            ),
            {
                "cid": payload.city_id,
                "vcid": payload.vehicle_category_id,
                "ttype": payload.trip_type,
            },
        )
        pr = pr_r.mappings().one_or_none()

        if not pr:
            # fallback to default
            pr_r2 = await db.execute(
                text(
                    """
                SELECT base_fare, per_km_rate, minimum_km, driver_allowance, night_charge,
                       driver_allowance_type, night_charge_type, toll
                FROM default_vehicle_pricing_rules
                WHERE vehicle_category_id = :vcid AND trip_type = :ttype
                LIMIT 1
            """
                ),
                {"vcid": payload.vehicle_category_id, "ttype": payload.trip_type},
            )
            pr = pr_r2.mappings().one_or_none()

        if pr:
            from app.modules.booking.services.fare import calculate_fare, trip_days_from

            # Estimate uses the pickup start (night-window for an
            # admin-future-dated booking) and the return datetime when the
            # matched rule uses PER_DAY driver allowance. Fall back to a
            # single-day trip when return_datetime is absent.
            estimated_amount = calculate_fare(
                dict(pr),
                payload.estimated_distance,
                start_dt=payload.pickup_datetime,
                end_dt=payload.return_datetime or payload.pickup_datetime,
                trip_type=payload.trip_type,
                trip_days=trip_days_from(
                    payload.pickup_datetime, payload.return_datetime
                ),
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
        city_id=payload.city_id,
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
        trip_type=payload.trip_type,
        vehicle_category_id=payload.vehicle_category_id,
        pickup_location=payload.pickup_location,
        pickup_latitude=payload.pickup_latitude,
        pickup_longitude=payload.pickup_longitude,
        drop_location=payload.drop_location,
        drop_latitude=payload.drop_latitude,
        drop_longitude=payload.drop_longitude,
        pickup_datetime=payload.pickup_datetime,
        return_datetime=payload.return_datetime,
        estimated_distance=payload.estimated_distance,
        estimated_amount=estimated_amount,
        booking_status="PENDING_ASSIGNMENT",
    )
    db.add(cb)
    await db.flush()  # populate cb.id for booking_services.service_reference_id

    # Link the CAB service to the master booking
    bs = BookingService(
        master_booking_id=mb.id,
        service_type="CAB",
        service_reference_id=cb.id,
        service_status="PENDING_ASSIGNMENT",
        service_amount=estimated_amount,
    )
    db.add(bs)

    # Audit trail
    tl = BookingTimeline(
        master_booking_id=mb.id,
        event_type="BOOKING_CREATED",
        event_description="Booking created by admin via Customer Care",
        created_by=created_by_uuid,
    )
    db.add(tl)

    # get_db auto-commits on success.

    return {
        "success": True,
        "master_booking_id": mb.id,
        "booking_number": mbk_number,
        "cab_booking_id": cb.id,
        "cab_booking_number": cb_number,
        "estimated_amount": float(estimated_amount),
        "message": "Cab booking created successfully",
    }


# ── Hotel booking helpers + endpoints ────────────────────────
#
# Hotel money is materially harder than cab fare: it is priced per night, with
# date-ranged rate plans, per-date inventory overrides, tariff-based GST slabs
# and a resolved commission config. So — unlike cab, which the client can
# estimate — the stay is ALWAYS priced server-side through the shared hotel
# pricing engine (app/modules/hotel/services/pricing.py). This guarantees an
# admin-created booking is priced by exactly the same rules as a customer one.


async def _load_active_hotel(db: AsyncSession, hotel_id: int) -> Hotel:
    """Fetch the Hotel ORM row (needed by resolve_hotel_commission and for
    tax_mode). Only ACTIVE, non-deleted hotels are bookable."""
    hotel = (
        await db.execute(
            select(Hotel).where(Hotel.id == hotel_id, Hotel.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if hotel is None:
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
):
    """Active, bookable hotels with their active room categories, for the admin
    hotel-booking form. Optionally filtered by city."""
    where = ["h.status = 'ACTIVE'", "h.deleted_at IS NULL"]
    params: dict = {}
    if city_id:
        where.append("h.city_id = :city_id")
        params["city_id"] = city_id
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

    cities_r = await db.execute(
        text("SELECT id, name FROM cities WHERE is_active = TRUE ORDER BY name")
    )
    cities = [dict(r) for r in cities_r.mappings().all()]

    return {"hotels": hotels, "cities": cities}


# ── 8b. Hotel booking quote (server-side price preview) ───────
@router.post("/hotel-booking/quote", tags=["Customer Care"])
async def hotel_booking_quote(
    payload: HotelBookingQuoteRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Price a stay without creating anything — drives the admin form's live
    fare panel."""
    hotel = await _load_active_hotel(db, payload.hotel_id)
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
):
    """Admin creates a hotel reservation on behalf of a customer. Prices the
    stay server-side, writes the master booking + reservation + service link +
    timeline, and decrements per-night inventory (guarded by the DB
    CHECK(available_rooms >= 0) constraint)."""

    # Verify customer exists (and grab their city for the master booking).
    cust_r = await db.execute(
        text("SELECT id, city_id FROM customers WHERE id = :cid"),
        {"cid": payload.customer_id},
    )
    cust = cust_r.mappings().one_or_none()
    if not cust:
        raise HTTPException(status_code=404, detail="Customer not found")

    hotel = await _load_active_hotel(db, payload.hotel_id)
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

    res_status = str(hr["reservation_status"] or "").upper()
    if res_status != "CONFIRMED":
        raise HTTPException(
            status_code=409,
            detail=(
                f"Only a CONFIRMED booking can be edited here (this one is "
                f"{res_status or 'UNKNOWN'}). Use check-out / add-charges instead."
            ),
        )

    hotel = await _load_active_hotel(db, int(hr["hotel_id"]))
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
):
    await _ensure_care_log_table(db)
    r = await db.execute(
        text(
            """
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE status = 'OPEN')     AS open_count,
            COUNT(*) FILTER (WHERE status = 'IN_PROGRESS') AS in_progress_count,
            COUNT(*) FILTER (WHERE status = 'RESOLVED') AS resolved_count,
            COUNT(*) FILTER (WHERE status = 'CLOSED')   AS closed_count,
            COUNT(*) FILTER (WHERE priority = 'URGENT') AS urgent_count,
            COUNT(*) FILTER (WHERE DATE(created_at) = CURRENT_DATE) AS today_count
        FROM customer_care_logs
    """
        )
    )
    row = dict(r.mappings().one())
    return {k: int(v or 0) for k, v in row.items()}
