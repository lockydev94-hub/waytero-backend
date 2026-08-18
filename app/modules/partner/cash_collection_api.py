# ============================================================
# WAYTERO — PARTNER DRIVER CASH COLLECTION API
# File: app/modules/partner/cash_collection_api.py
# Prefix: /partners  (registered in api/router.py)
# Endpoints:
#   GET  /partners/me/cash-collection/drivers            — drivers holding cash
#   GET  /partners/me/cash-collection/drivers/{id}/pending — that driver's pending bookings
#   POST /partners/me/cash-collection/collect            — record a collection
#   GET  /partners/me/cash-collection/history            — past collections
#
# Domain:
#   A driver who takes CASH from a customer holds it until the partner collects
#   it. cab_bookings.cash_pending_at tracks custody: 'DRIVER' after the driver
#   takes cash, 'PARTNER' once the partner has it, 'NONE' when no cash is owed.
#   Nothing previously performed the DRIVER -> PARTNER transition; this module
#   is that step. 'PARTNER' is already treated as settled by the admin
#   trip-assistance attention bucket, so collected bookings drop out of it.
# ============================================================

from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.modules.partner.booking_api import _resolve_partner_id, _log_timeline

router = APIRouter()

# Rupee tolerance when matching the typed amount against the expected total.
# Exact-to-the-paisa equality would reject "9200" for a 9200.00 total.
AMOUNT_TOLERANCE = Decimal("0.01")


# ════════════════════════════════════════════════════════════════
# SCHEMAS
# ════════════════════════════════════════════════════════════════


class DriverCashSummary(BaseModel):
    driver_id: int
    driver_code: Optional[str]
    driver_name: str
    driver_mobile: Optional[str]
    driver_status: Optional[str]
    pending_count: int
    pending_amount: float
    oldest_pending_at: Optional[str]
    vehicle_reg: Optional[str]


class DriverCashSummaryResponse(BaseModel):
    total_drivers: int
    total_pending_amount: float
    total_pending_count: int
    drivers: List[DriverCashSummary]


class PendingCashBooking(BaseModel):
    cab_booking_id: int
    booking_number: str
    master_booking_number: str
    booking_status: str
    trip_type: Optional[str]
    pickup_location: Optional[str]
    drop_location: Optional[str]
    customer_name: str
    customer_mobile: Optional[str]
    trip_ended_at: Optional[str]
    final_amount: float
    cash_amount: float
    platform_commission: float
    partner_payout: float
    invoice_number: Optional[str]
    vehicle_reg: Optional[str]


class DriverPendingResponse(BaseModel):
    driver_id: int
    driver_code: Optional[str]
    driver_name: str
    driver_mobile: Optional[str]
    pending_count: int
    pending_amount: float
    bookings: List[PendingCashBooking]


class CollectRequest(BaseModel):
    driver_id: int
    cab_booking_ids: List[int] = Field(..., min_length=1)
    # Partner re-types the figure they physically counted; the server rejects the
    # collection unless it matches the selected bookings' total.
    entered_amount: float = Field(..., gt=0)
    payment_method: str = Field("CASH", description="CASH | UPI | BANK_TRANSFER")
    reference_note: Optional[str] = Field(None, max_length=500)


class CollectResponse(BaseModel):
    success: bool
    message: str
    collection_number: str
    driver_id: int
    driver_name: str
    bookings_count: int
    expected_amount: float
    collected_amount: float
    booking_numbers: List[str]
    collected_at: str


class CollectionHistoryItem(BaseModel):
    id: int
    collection_number: str
    driver_id: int
    driver_name: str
    driver_mobile: Optional[str]
    bookings_count: int
    expected_amount: float
    collected_amount: float
    payment_method: str
    status: str
    reference_note: Optional[str]
    collected_at: str
    booking_numbers: List[str]


class CollectionHistoryResponse(BaseModel):
    total: int
    page: int
    limit: int
    pages: int
    items: List[CollectionHistoryItem]


# ════════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════════


def _iso(value) -> Optional[str]:
    return value.isoformat() if value else None


# The cash figure the driver actually holds. cash_amount_due is written at
# payment time; COALESCE keeps rows created before migration 0026 usable.
_CASH_EXPR = "COALESCE(cb.cash_amount_due, cb.final_amount, cb.estimated_amount, 0)"

# cab_booking_assignments has no UNIQUE on cab_booking_id, so a booking could
# in principle carry more than one row for this partner. A plain JOIN would then
# count the same cash twice. Collapse to the latest assignment per booking — the
# same "latest assignment" rule used elsewhere in the partner booking API.
_LATEST_ASSIGNMENT_JOIN = """
    FROM cab_bookings cb
    JOIN LATERAL (
        SELECT a.driver_id, a.vehicle_id, a.partner_id
        FROM cab_booking_assignments a
        WHERE a.cab_booking_id = cb.id AND a.partner_id = :pid
        ORDER BY a.assigned_at DESC NULLS LAST, a.id DESC
        LIMIT 1
    ) cba ON TRUE
"""

# A booking's cash is pending only while custody sits with the driver AND no
# collection item already claims it. The UNIQUE constraint on
# driver_cash_collection_items.cab_booking_id makes double-collection impossible;
# this clause keeps already-collected rows out of the UI in the first place.
_PENDING_WHERE = """
    cb.payment_mode = 'CASH'
    AND cb.cash_pending_at = 'DRIVER'
    AND cba.driver_id IS NOT NULL
    AND NOT EXISTS (
        SELECT 1 FROM driver_cash_collection_items dci
        WHERE dci.cab_booking_id = cb.id
    )
"""


async def _next_collection_number(db: AsyncSession) -> str:
    """
    WT-CC-YYYYMM-00001. MAX-based rather than COUNT-based so a concurrent insert
    cannot hand two collections the same number.
    """
    row = (
        await db.execute(
            text(
                """
        SELECT MAX(CAST(SPLIT_PART(collection_number, '-', 4) AS INTEGER))
        FROM driver_cash_collections
        WHERE collection_number LIKE 'WT-CC-%'
    """
            )
        )
    ).scalar()
    seq = (row or 0) + 1
    return f"WT-CC-{datetime.now(timezone.utc).strftime('%Y%m')}-{str(seq).zfill(5)}"


# ════════════════════════════════════════════════════════════════
# LIST DRIVERS HOLDING CASH
# ════════════════════════════════════════════════════════════════


@router.get(
    "/me/cash-collection/drivers",
    response_model=DriverCashSummaryResponse,
    summary="Drivers of this partner who are holding uncollected cash",
    tags=["Partner Cash Collection"],
)
async def list_drivers_with_pending_cash(
    search: Optional[str] = Query(
        None, description="Filter by driver name / code / mobile"
    ),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    partner_id = await _resolve_partner_id(db, current_user["sub"])

    search_clause = ""
    params: dict = {"pid": partner_id}
    if search and search.strip():
        search_clause = """
            AND (d.full_name ILIKE :q OR d.driver_code ILIKE :q OR d.mobile ILIKE :q)
        """
        params["q"] = f"%{search.strip()}%"

    rows = (
        (
            await db.execute(
                text(
                    f"""
        SELECT
            d.id                       AS driver_id,
            d.driver_code,
            d.full_name                AS driver_name,
            d.mobile                   AS driver_mobile,
            d.status                   AS driver_status,
            COUNT(cb.id)               AS pending_count,
            COALESCE(SUM({_CASH_EXPR}), 0) AS pending_amount,
            MIN(cb.trip_ended_at)      AS oldest_pending_at,
            MAX(v.registration_number) AS vehicle_reg
        {_LATEST_ASSIGNMENT_JOIN}
        JOIN drivers d       ON d.id = cba.driver_id
        LEFT JOIN vehicles v ON v.id = cba.vehicle_id
        WHERE {_PENDING_WHERE}
          {search_clause}
        GROUP BY d.id, d.driver_code, d.full_name, d.mobile, d.status
        ORDER BY pending_amount DESC
    """
                ),
                params,
            )
        )
        .mappings()
        .all()
    )

    drivers = [
        DriverCashSummary(
            driver_id=r["driver_id"],
            driver_code=r["driver_code"],
            driver_name=r["driver_name"],
            driver_mobile=r["driver_mobile"],
            driver_status=r["driver_status"],
            pending_count=int(r["pending_count"] or 0),
            pending_amount=float(r["pending_amount"] or 0),
            oldest_pending_at=_iso(r["oldest_pending_at"]),
            vehicle_reg=r["vehicle_reg"],
        )
        for r in rows
    ]

    return DriverCashSummaryResponse(
        total_drivers=len(drivers),
        total_pending_amount=round(sum(d.pending_amount for d in drivers), 2),
        total_pending_count=sum(d.pending_count for d in drivers),
        drivers=drivers,
    )


# ════════════════════════════════════════════════════════════════
# PENDING BOOKINGS FOR ONE DRIVER
# ════════════════════════════════════════════════════════════════


@router.get(
    "/me/cash-collection/drivers/{driver_id}/pending",
    response_model=DriverPendingResponse,
    summary="Uncollected cash bookings for one of this partner's drivers",
    tags=["Partner Cash Collection"],
)
async def list_driver_pending_cash(
    driver_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    partner_id = await _resolve_partner_id(db, current_user["sub"])

    driver = (
        (
            await db.execute(
                text(
                    """
        SELECT id, driver_code, full_name, mobile
        FROM drivers
        WHERE id = :did AND partner_id = :pid
    """
                ),
                {"did": driver_id, "pid": partner_id},
            )
        )
        .mappings()
        .one_or_none()
    )

    if not driver:
        raise HTTPException(404, "Driver not found for this partner.")

    rows = (
        (
            await db.execute(
                text(
                    f"""
        SELECT
            cb.id                  AS cab_booking_id,
            cb.booking_number,
            mb.booking_number      AS master_booking_number,
            cb.booking_status,
            cb.trip_type,
            cb.pickup_location,
            cb.drop_location,
            cb.trip_ended_at,
            cb.final_amount,
            cb.platform_commission,
            cb.partner_payout,
            cb.invoice_number,
            {_CASH_EXPR}           AS cash_amount,
            v.registration_number  AS vehicle_reg,
            COALESCE(NULLIF(TRIM(COALESCE(c.first_name,'') || ' ' || COALESCE(c.last_name,'')), ''), 'Unknown') AS customer_name,
            u.mobile_number        AS customer_mobile
        {_LATEST_ASSIGNMENT_JOIN}
        JOIN master_bookings mb ON mb.id = cb.master_booking_id
        JOIN customers c        ON c.id = mb.customer_id
        LEFT JOIN users u       ON u.id = c.user_id
        LEFT JOIN vehicles v    ON v.id = cba.vehicle_id
        WHERE {_PENDING_WHERE}
          AND cba.driver_id = :did
        ORDER BY cb.trip_ended_at ASC NULLS LAST, cb.id ASC
    """
                ),
                {"pid": partner_id, "did": driver_id},
            )
        )
        .mappings()
        .all()
    )

    bookings = [
        PendingCashBooking(
            cab_booking_id=r["cab_booking_id"],
            booking_number=r["booking_number"],
            master_booking_number=r["master_booking_number"],
            booking_status=r["booking_status"],
            trip_type=r["trip_type"],
            pickup_location=r["pickup_location"],
            drop_location=r["drop_location"],
            customer_name=r["customer_name"],
            customer_mobile=r["customer_mobile"],
            trip_ended_at=_iso(r["trip_ended_at"]),
            final_amount=float(r["final_amount"] or 0),
            cash_amount=float(r["cash_amount"] or 0),
            platform_commission=float(r["platform_commission"] or 0),
            partner_payout=float(r["partner_payout"] or 0),
            invoice_number=r["invoice_number"],
            vehicle_reg=r["vehicle_reg"],
        )
        for r in rows
    ]

    return DriverPendingResponse(
        driver_id=driver["id"],
        driver_code=driver["driver_code"],
        driver_name=driver["full_name"],
        driver_mobile=driver["mobile"],
        pending_count=len(bookings),
        pending_amount=round(sum(b.cash_amount for b in bookings), 2),
        bookings=bookings,
    )


# ════════════════════════════════════════════════════════════════
# RECORD A COLLECTION
# ════════════════════════════════════════════════════════════════


@router.post(
    "/me/cash-collection/collect",
    response_model=CollectResponse,
    summary="Record cash collected from a driver for selected bookings",
    tags=["Partner Cash Collection"],
)
async def collect_driver_cash(
    payload: CollectRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    partner_id = await _resolve_partner_id(db, current_user["sub"])

    method = payload.payment_method.upper()
    if method not in {"CASH", "UPI", "BANK_TRANSFER"}:
        raise HTTPException(400, "payment_method must be CASH, UPI or BANK_TRANSFER.")

    driver = (
        (
            await db.execute(
                text(
                    """
        SELECT id, full_name, mobile FROM drivers
        WHERE id = :did AND partner_id = :pid
    """
                ),
                {"did": payload.driver_id, "pid": partner_id},
            )
        )
        .mappings()
        .one_or_none()
    )

    if not driver:
        raise HTTPException(404, "Driver not found for this partner.")

    booking_ids = list(dict.fromkeys(payload.cab_booking_ids))

    # Re-read the selected bookings under FOR UPDATE and re-apply the pending
    # filter, so a stale browser selection cannot collect the same cash twice or
    # reach another partner's booking. The client-supplied amount is never trusted
    # as the source of truth — only compared against this.
    rows = (
        (
            await db.execute(
                text(
                    f"""
        SELECT cb.id, cb.booking_number, cb.master_booking_id,
               {_CASH_EXPR} AS cash_amount
        {_LATEST_ASSIGNMENT_JOIN}
        WHERE {_PENDING_WHERE}
          AND cba.driver_id = :did
          AND cb.id = ANY(:ids)
        FOR UPDATE OF cb
    """
                ),
                {"pid": partner_id, "did": payload.driver_id, "ids": booking_ids},
            )
        )
        .mappings()
        .all()
    )

    if len(rows) != len(booking_ids):
        found = {r["id"] for r in rows}
        missing = [str(i) for i in booking_ids if i not in found]
        raise HTTPException(
            409,
            f"{len(missing)} of the selected booking(s) are no longer pending collection "
            f"(id: {', '.join(missing)}). Refresh the list and try again.",
        )

    expected = sum((Decimal(str(r["cash_amount"])) for r in rows), Decimal("0"))
    entered = Decimal(str(payload.entered_amount))

    if abs(entered - expected) > AMOUNT_TOLERANCE:
        raise HTTPException(
            400,
            f"Amount mismatch. Selected bookings total ₹{expected:.2f} "
            f"but ₹{entered:.2f} was entered. Amounts must match to collect.",
        )

    collection_number = await _next_collection_number(db)

    collection_id = (
        await db.execute(
            text(
                """
        INSERT INTO driver_cash_collections
            (collection_number, partner_id, driver_id, bookings_count,
             expected_amount, collected_amount, payment_method, status,
             reference_note, collected_by_user_id, collected_at, created_at)
        VALUES
            (:num, :pid, :did, :cnt, :expected, :collected, :method, 'COLLECTED',
             :note, :uid, NOW(), NOW())
        RETURNING id
    """
            ),
            {
                "num": collection_number,
                "pid": partner_id,
                "did": payload.driver_id,
                "cnt": len(rows),
                "expected": expected,
                "collected": entered,
                "method": method,
                "note": payload.reference_note,
                "uid": current_user["sub"],
            },
        )
    ).scalar_one()

    for r in rows:
        await db.execute(
            text(
                """
            INSERT INTO driver_cash_collection_items
                (collection_id, cab_booking_id, booking_number, amount, created_at)
            VALUES (:cid, :bid, :bn, :amt, NOW())
        """
            ),
            {
                "cid": collection_id,
                "bid": r["id"],
                "bn": r["booking_number"],
                "amt": r["cash_amount"],
            },
        )

        await db.execute(
            text(
                """
            UPDATE cab_bookings SET cash_pending_at = 'PARTNER', updated_at = NOW()
            WHERE id = :bid
        """
            ),
            {"bid": r["id"]},
        )

        await _log_timeline(
            db,
            r["master_booking_id"],
            "CASH_COLLECTED_BY_PARTNER",
            f"Cash ₹{float(r['cash_amount']):.2f} collected from driver "
            f"{driver['full_name']} by partner via {method}. "
            f"Collection: {collection_number}. Cash pending at: PARTNER.",
        )

    try:
        await db.commit()
    except Exception as err:
        await db.rollback()
        # UNIQUE(cab_booking_id) tripped — a concurrent request collected the
        # same booking between our FOR UPDATE read and this commit.
        raise HTTPException(
            409,
            "One of these bookings was collected by another request just now. "
            f"Refresh and try again. ({err.__class__.__name__})",
        )

    return CollectResponse(
        success=True,
        message=f"₹{expected:.2f} collected from {driver['full_name']} across {len(rows)} booking(s).",
        collection_number=collection_number,
        driver_id=payload.driver_id,
        driver_name=driver["full_name"],
        bookings_count=len(rows),
        expected_amount=float(expected),
        collected_amount=float(entered),
        booking_numbers=[r["booking_number"] for r in rows],
        collected_at=datetime.now(timezone.utc).isoformat(),
    )


# ════════════════════════════════════════════════════════════════
# COLLECTION HISTORY
# ════════════════════════════════════════════════════════════════


@router.get(
    "/me/cash-collection/history",
    response_model=CollectionHistoryResponse,
    summary="Past cash collections recorded by this partner",
    tags=["Partner Cash Collection"],
)
async def list_collection_history(
    driver_id: Optional[int] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    partner_id = await _resolve_partner_id(db, current_user["sub"])

    where = "dcc.partner_id = :pid"
    params: dict = {"pid": partner_id}
    if driver_id is not None:
        where += " AND dcc.driver_id = :did"
        params["did"] = driver_id

    total = (
        await db.execute(
            text(f"SELECT COUNT(*) FROM driver_cash_collections dcc WHERE {where}"),
            params,
        )
    ).scalar() or 0

    rows = (
        (
            await db.execute(
                text(
                    f"""
        SELECT
            dcc.id, dcc.collection_number, dcc.driver_id, dcc.bookings_count,
            dcc.expected_amount, dcc.collected_amount, dcc.payment_method,
            dcc.status, dcc.reference_note, dcc.collected_at,
            d.full_name AS driver_name,
            d.mobile    AS driver_mobile,
            COALESCE(
                ARRAY_AGG(dci.booking_number ORDER BY dci.id)
                    FILTER (WHERE dci.booking_number IS NOT NULL),
                '{{}}'
            ) AS booking_numbers
        FROM driver_cash_collections dcc
        JOIN drivers d ON d.id = dcc.driver_id
        LEFT JOIN driver_cash_collection_items dci ON dci.collection_id = dcc.id
        WHERE {where}
        GROUP BY dcc.id, d.full_name, d.mobile
        ORDER BY dcc.collected_at DESC
        LIMIT :limit OFFSET :offset
    """
                ),
                {**params, "limit": limit, "offset": (page - 1) * limit},
            )
        )
        .mappings()
        .all()
    )

    items = [
        CollectionHistoryItem(
            id=r["id"],
            collection_number=r["collection_number"],
            driver_id=r["driver_id"],
            driver_name=r["driver_name"],
            driver_mobile=r["driver_mobile"],
            bookings_count=int(r["bookings_count"] or 0),
            expected_amount=float(r["expected_amount"] or 0),
            collected_amount=float(r["collected_amount"] or 0),
            payment_method=r["payment_method"],
            status=r["status"],
            reference_note=r["reference_note"],
            collected_at=_iso(r["collected_at"]) or "",
            booking_numbers=list(r["booking_numbers"] or []),
        )
        for r in rows
    ]

    return CollectionHistoryResponse(
        total=total,
        page=page,
        limit=limit,
        pages=max(1, (total + limit - 1) // limit),
        items=items,
    )
