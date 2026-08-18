# ============================================================
# WAYTERO — ADMIN REPORTS API
# File: app/modules/admin/reports_api.py
# Prefix: /admin/reports
#
# Tabs:
#   1. Booking Reports  — completed/settled booking records
#   2. GST Reports      — GST collected per period + challan generation (PERSISTED)
#   3. TDS Reports      — TDS for B2B company partners only
#   4. Partner Reports  — per-partner breakdown
#   5. Customer Reports — advanced filtered customer records
#
# India GST Rules (Transport / Cab):
#   - GST @ 5% (2.5% CGST + 2.5% SGST) on transport services (SAC 9964)
#   - COMPANY partner: has GSTIN, files own GST returns (GSTR-1/3B monthly)
#   - INDIVIDUAL partner: not GST-registered; platform files on their behalf
#   - Tax invoice shown only if GST_ENABLED = true at booking time
#   - Challan = PMT-06 (GST portal) generated before 20th of next month
#
# India TDS Rules (Partner Payouts):
#   - Section 194C: 1% TDS on payments to contractors (transporters)
#   - Applies ONLY to COMPANY (B2B) partners with PAN
#   - Individual partners: no TDS (threshold INR 30,000/year per payment)
#   - Filed quarterly via Form 26Q
#
# Doc Ref: 14_Reporting_Business_Intelligence/04_FINANCIAL_REPORTING.md
#          14_Reporting_Business_Intelligence/07_PARTNER_ANALYTICS.md
#          Migration 0024 — GST columns on cab_bookings
#          Migration 0029 — gst_challan_records, partner_gst_challans, tds_deduction_records
# ============================================================

import logging
import math
from datetime import datetime, timezone, date, timedelta
from decimal import Decimal
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func, text, and_, or_, extract, case
from sqlalchemy import (
    table as sa_table,
    column as sa_column,
    BigInteger as sa_BigInteger,
    Numeric as sa_Numeric,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.modules.booking.models import MasterBooking, CabBooking, CabBookingAssignment
from app.modules.customer.models import Customer
from app.modules.auth.models.user import User
from app.modules.partner.models import Partner, PartnerGSTDetails
from app.modules.master.models import City

_log = logging.getLogger(__name__)

router = APIRouter()

# ═══════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════


def _financial_year(dt: date) -> str:
    """Returns '2025-26' style FY string for an Indian financial year (Apr–Mar)."""
    y = dt.year
    if dt.month < 4:
        return f"{y-1}-{str(y)[2:]}"
    return f"{y}-{str(y+1)[2:]}"


def _quarter(dt: date) -> str:
    """Returns Q1–Q4 for Indian FY (Q1=Apr-Jun, Q2=Jul-Sep, Q3=Oct-Dec, Q4=Jan-Mar)."""
    m = dt.month
    if m in (4, 5, 6):
        return "Q1"
    if m in (7, 8, 9):
        return "Q2"
    if m in (10, 11, 12):
        return "Q3"
    return "Q4"


MONTH_NAMES = [
    "",
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]

# tds_deduction_records (migration 0029) has no ORM model; declare the columns
# the TDS report needs so it can be joined in a normal select().
_tds_records = sa_table(
    "tds_deduction_records",
    sa_column("id", sa_BigInteger),
    sa_column("cab_booking_id", sa_BigInteger),
    sa_column("tds_amount", sa_Numeric(14, 2)),
)

# ═══════════════════════════════════════════════════════════
# 1. BOOKING REPORTS
# ═══════════════════════════════════════════════════════════


class BookingReportItem(BaseModel):
    booking_number: str
    customer_name: Optional[str]
    customer_mobile: Optional[str]
    city_name: Optional[str]
    journey_date: Optional[str]
    trip_type: Optional[str]
    pickup_location: Optional[str]
    drop_location: Optional[str]
    vehicle_category: Optional[str]
    partner_name: Optional[str]
    final_amount: Optional[float]
    gst_amount: Optional[float]
    is_tax_invoice: bool
    payment_mode: Optional[str]
    payment_status: str
    booking_status: str
    cab_status: Optional[str]
    invoice_number: Optional[str]
    platform_commission: Optional[float]
    partner_payout: Optional[float]
    created_at: str


class BookingReportResponse(BaseModel):
    total: int
    page: int
    page_size: int
    total_pages: int
    summary: dict
    items: List[BookingReportItem]


@router.get("/bookings", response_model=BookingReportResponse)
async def booking_report(
    period: str = Query("month"),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    city_id: Optional[int] = Query(None),
    partner_id: Optional[int] = Query(None),
    booking_status: Optional[str] = Query(None),
    payment_mode: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(
            MasterBooking.booking_number,
            MasterBooking.booking_status,
            MasterBooking.payment_status,
            MasterBooking.created_at,
            MasterBooking.journey_start_date,
            CabBooking.booking_number.label("cab_booking_number"),
            CabBooking.booking_status.label("cab_status"),
            CabBooking.trip_type,
            CabBooking.pickup_location,
            CabBooking.drop_location,
            CabBooking.final_amount,
            CabBooking.payment_mode,
            CabBooking.platform_commission,
            CabBooking.partner_payout,
            CabBooking.invoice_number,
            text("COALESCE(cab_bookings.gst_amount, 0) AS gst_amount"),
            text("COALESCE(cab_bookings.is_tax_invoice, false) AS is_tax_invoice"),
            func.concat(User.first_name, " ", func.coalesce(User.last_name, "")).label(
                "customer_name"
            ),
            User.mobile_number.label("customer_mobile"),
            City.name.label("city_name"),
            Partner.business_name.label("partner_name"),
        )
        .join(
            CabBooking, CabBooking.master_booking_id == MasterBooking.id, isouter=True
        )
        .join(Customer, Customer.id == MasterBooking.customer_id, isouter=True)
        .join(User, User.id == Customer.user_id, isouter=True)
        .join(City, City.id == MasterBooking.city_id, isouter=True)
        .join(
            CabBookingAssignment,
            CabBookingAssignment.cab_booking_id == CabBooking.id,
            isouter=True,
        )
        .join(Partner, Partner.id == CabBookingAssignment.partner_id, isouter=True)
    )

    filters = []
    if period == "custom" and date_from:
        try:
            df = date.fromisoformat(date_from[:10])
            filters.append(
                text(
                    f"COALESCE(master_bookings.journey_start_date, master_bookings.created_at::date) >= '{df}'"
                )
            )
            if date_to:
                dt = date.fromisoformat(date_to[:10])
                filters.append(
                    text(
                        f"COALESCE(master_bookings.journey_start_date, master_bookings.created_at::date) <= '{dt}'"
                    )
                )
        except Exception:
            pass
    elif period != "all":
        now = datetime.now(timezone.utc)
        today = now.date()
        if period == "today":
            start_date = today
        elif period == "week":
            start_date = today - timedelta(days=7)
        elif period == "month":
            start_date = today.replace(day=1)
        elif period == "year":
            start_date = today.replace(month=1, day=1)
        else:
            start_date = None
        if start_date:
            filters.append(
                text(
                    f"COALESCE(master_bookings.journey_start_date, master_bookings.created_at::date) >= '{start_date}'"
                )
            )

    if city_id:
        filters.append(MasterBooking.city_id == city_id)
    if partner_id:
        filters.append(CabBookingAssignment.partner_id == partner_id)
    if booking_status:
        filters.append(MasterBooking.booking_status == booking_status)
    if payment_mode:
        filters.append(CabBooking.payment_mode == payment_mode)
    filters.append(
        MasterBooking.booking_status.in_(
            ["COMPLETED", "CLOSED", "CONFIRMED", "IN_PROGRESS"]
        )
    )

    if filters:
        stmt = stmt.where(and_(*filters))

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await db.execute(count_stmt)).scalar_one() or 0

    summary_stmt = (
        select(
            func.count(MasterBooking.id).label("total_bookings"),
            func.sum(
                case(
                    (MasterBooking.booking_status.in_(["COMPLETED", "CLOSED"]), 1),
                    else_=0,
                )
            ).label("completed"),
            func.sum(case((CabBooking.booking_status == "SETTLED", 1), else_=0)).label(
                "settled"
            ),
            func.coalesce(func.sum(CabBooking.final_amount), 0).label("total_revenue"),
            func.coalesce(func.sum(CabBooking.platform_commission), 0).label(
                "total_commission"
            ),
            func.coalesce(func.sum(CabBooking.partner_payout), 0).label("total_payout"),
            func.coalesce(
                func.sum(text("COALESCE(cab_bookings.gst_amount, 0)")), 0
            ).label("total_gst"),
        )
        .select_from(MasterBooking)
        .join(
            CabBooking, CabBooking.master_booking_id == MasterBooking.id, isouter=True
        )
        .join(
            CabBookingAssignment,
            CabBookingAssignment.cab_booking_id == CabBooking.id,
            isouter=True,
        )
    )
    if filters:
        summary_stmt = summary_stmt.where(and_(*filters))
    summary_row = (await db.execute(summary_stmt)).one()
    summary = {
        "total_bookings": int(summary_row.total_bookings or 0),
        "completed": int(summary_row.completed or 0),
        "settled": int(summary_row.settled or 0),
        "total_revenue": float(summary_row.total_revenue or 0),
        "total_commission": float(summary_row.total_commission or 0),
        "total_payout": float(summary_row.total_payout or 0),
        "total_gst": float(summary_row.total_gst or 0),
    }

    offset = (page - 1) * page_size
    stmt = (
        stmt.order_by(MasterBooking.created_at.desc()).offset(offset).limit(page_size)
    )
    rows = (await db.execute(stmt)).mappings().all()

    items = [
        BookingReportItem(
            booking_number=r["booking_number"],
            customer_name=r["customer_name"],
            customer_mobile=r["customer_mobile"],
            city_name=r["city_name"],
            journey_date=(
                r["journey_start_date"].isoformat() if r["journey_start_date"] else None
            ),
            trip_type=r["trip_type"],
            pickup_location=r["pickup_location"],
            drop_location=r["drop_location"],
            vehicle_category=None,
            partner_name=r["partner_name"],
            final_amount=float(r["final_amount"]) if r["final_amount"] else None,
            gst_amount=float(r["gst_amount"]) if r.get("gst_amount") else 0.0,
            is_tax_invoice=bool(r.get("is_tax_invoice", False)),
            payment_mode=r["payment_mode"],
            payment_status=r["payment_status"],
            booking_status=r["booking_status"],
            cab_status=r["cab_status"],
            invoice_number=r["invoice_number"],
            platform_commission=(
                float(r["platform_commission"]) if r["platform_commission"] else None
            ),
            partner_payout=float(r["partner_payout"]) if r["partner_payout"] else None,
            created_at=r["created_at"].isoformat() if r["created_at"] else "",
        )
        for r in rows
    ]

    return BookingReportResponse(
        total=total,
        page=page,
        page_size=page_size,
        total_pages=max(1, math.ceil(total / page_size)),
        summary=summary,
        items=items,
    )


# ═══════════════════════════════════════════════════════════
# 2. GST REPORTS + CHALLAN (PERSISTED)
# India: 5% GST on transport (SAC 9964) = 2.5% CGST + 2.5% SGST
# Only bookings with is_tax_invoice=TRUE are counted.
# Challan = PMT-06 on GST portal; file GSTR-3B by 20th of next month.
# ═══════════════════════════════════════════════════════════


class GSTRecord(BaseModel):
    month: str
    month_label: str
    total_taxable: float
    gst_amount: float
    cgst_amount: float
    sgst_amount: float
    total_invoices: int
    challan_generated: bool
    challan_number: Optional[str]
    challan_date: Optional[str]
    challan_status: Optional[str]


class GSTReportResponse(BaseModel):
    year: int
    gst_enabled: bool
    records: List[GSTRecord]
    yearly_summary: dict


@router.get("/gst", response_model=GSTReportResponse)
async def gst_report(
    year: int = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """
    Monthly GST report for the year.
    - Only counts bookings where is_tax_invoice = TRUE (GST-enabled bookings).
    - Reads persisted challan records from gst_challan_records table.
    - Exposes the platform-level GST_ENABLED switch (same config the booking
      engine reads) so the frontend can render a "GST not enabled" state
      instead of an all-zeros table when the admin has turned tax off.
    """
    if not year:
        year = datetime.now(timezone.utc).year

    # Platform-level GST switch — mirrors the TDS report's tds_enabled pattern.
    gst_cfg = (
        await db.execute(
            text(
                "SELECT config_value FROM system_configurations WHERE config_key = 'GST_ENABLED'"
            )
        )
    ).first()
    gst_enabled = (gst_cfg[0] if gst_cfg else "false").lower() == "true"

    # Aggregate GST-taxable bookings by month
    stmt = (
        select(
            extract("month", CabBooking.updated_at).label("month_num"),
            func.count(CabBooking.id).label("total_invoices"),
            func.coalesce(
                func.sum(text("COALESCE(cab_bookings.gst_amount, 0)")), 0
            ).label("total_gst"),
            func.coalesce(func.sum(CabBooking.final_amount), 0).label("total_revenue"),
        )
        .where(
            and_(
                extract("year", CabBooking.updated_at) == year,
                CabBooking.booking_status.in_(["COMPLETED", "SETTLED"]),
                CabBooking.final_amount.isnot(None),
                text("COALESCE(cab_bookings.is_tax_invoice, false) = true"),
            )
        )
        .group_by(extract("month", CabBooking.updated_at))
        .order_by(extract("month", CabBooking.updated_at))
    )
    rows = (await db.execute(stmt)).all()

    # Load existing challan records
    challan_rows = (
        (
            await db.execute(
                text(
                    "SELECT month, challan_number, challan_date, challan_status FROM gst_challan_records WHERE year = :y"
                ),
                {"y": year},
            )
        )
        .mappings()
        .all()
    )
    challan_map = {r["month"]: r for r in challan_rows}

    existing = {int(r.month_num): r for r in rows}
    records = []
    total_year_taxable = Decimal("0")
    total_year_gst = Decimal("0")

    for m in range(1, 13):
        month_key = f"{year}-{m:02d}"
        month_label = f"{MONTH_NAMES[m]} {year}"
        challan = challan_map.get(month_key)

        if m in existing:
            r = existing[m]
            # Use stored gst_amount directly (set at booking time from GST_RATE config)
            gst_amt = Decimal(str(r.total_gst))
            total_revenue = Decimal(str(r.total_revenue))
            taxable = total_revenue - gst_amt
            cgst = (gst_amt / 2).quantize(Decimal("0.01"))
            sgst = (gst_amt / 2).quantize(Decimal("0.01"))
            total_year_taxable += taxable
            total_year_gst += gst_amt
            records.append(
                GSTRecord(
                    month=month_key,
                    month_label=month_label,
                    total_taxable=round(float(taxable), 2),
                    gst_amount=round(float(gst_amt), 2),
                    cgst_amount=float(cgst),
                    sgst_amount=float(sgst),
                    total_invoices=int(r.total_invoices),
                    challan_generated=bool(challan),
                    challan_number=challan["challan_number"] if challan else None,
                    challan_date=(
                        str(challan["challan_date"])
                        if challan and challan["challan_date"]
                        else None
                    ),
                    challan_status=challan["challan_status"] if challan else None,
                )
            )
        else:
            records.append(
                GSTRecord(
                    month=month_key,
                    month_label=month_label,
                    total_taxable=0.0,
                    gst_amount=0.0,
                    cgst_amount=0.0,
                    sgst_amount=0.0,
                    total_invoices=0,
                    challan_generated=bool(challan),
                    challan_number=challan["challan_number"] if challan else None,
                    challan_date=(
                        str(challan["challan_date"])
                        if challan and challan["challan_date"]
                        else None
                    ),
                    challan_status=challan["challan_status"] if challan else None,
                )
            )

    return GSTReportResponse(
        year=year,
        gst_enabled=gst_enabled,
        records=records,
        yearly_summary={
            "total_taxable": round(float(total_year_taxable), 2),
            "total_gst": round(float(total_year_gst), 2),
            "total_cgst": round(float(total_year_gst / 2), 2),
            "total_sgst": round(float(total_year_gst / 2), 2),
        },
    )


class GenerateChallanRequest(BaseModel):
    month: str
    challan_number: str


class GenerateChallanResponse(BaseModel):
    success: bool
    message: str
    challan_number: str
    month: str
    gst_amount: float
    cgst_amount: float
    sgst_amount: float


@router.post("/gst/generate-challan", response_model=GenerateChallanResponse)
async def generate_gst_challan(
    payload: GenerateChallanRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Generate and PERSIST a GST challan record for a specific month.
    Creates/updates the gst_challan_records row.
    """
    try:
        year_str, month_str = payload.month.split("-")
        year_val = int(year_str)
        month_val = int(month_str)
    except Exception:
        raise HTTPException(400, "Invalid month format. Use YYYY-MM.")

    # Validate challan number not duplicate for a DIFFERENT month
    existing_cn = (
        await db.execute(
            text(
                "SELECT month FROM gst_challan_records WHERE challan_number = :cn AND month != :m"
            ),
            {"cn": payload.challan_number.upper(), "m": payload.month},
        )
    ).one_or_none()
    if existing_cn:
        raise HTTPException(
            400,
            f"Challan number {payload.challan_number} already used for {existing_cn[0]}.",
        )

    # Aggregate GST for the month (only tax invoices)
    stmt = select(
        func.coalesce(func.sum(text("COALESCE(cab_bookings.gst_amount, 0)")), 0).label(
            "total_gst"
        ),
        func.coalesce(func.sum(CabBooking.final_amount), 0).label("total_revenue"),
        func.count(CabBooking.id).label("total_invoices"),
    ).where(
        and_(
            extract("year", CabBooking.updated_at) == year_val,
            extract("month", CabBooking.updated_at) == month_val,
            CabBooking.booking_status.in_(["COMPLETED", "SETTLED"]),
            CabBooking.final_amount.isnot(None),
            text("COALESCE(cab_bookings.is_tax_invoice, false) = true"),
        )
    )
    row = (await db.execute(stmt)).one()
    gst_amount = Decimal(str(row.total_gst))
    total_revenue = Decimal(str(row.total_revenue))
    taxable = total_revenue - gst_amount
    cgst = (gst_amount / 2).quantize(Decimal("0.01"))
    sgst = (gst_amount / 2).quantize(Decimal("0.01"))
    total_invoices = int(row.total_invoices)

    # Upsert challan record
    await db.execute(
        text(
            """
        INSERT INTO gst_challan_records
            (month, year, month_number, total_taxable, gst_amount, total_invoices,
             cgst_amount, sgst_amount, challan_number, challan_date, challan_status, updated_at)
        VALUES
            (:month, :year, :month_num, :taxable, :gst, :invoices,
             :cgst, :sgst, :challan_no, CURRENT_DATE, 'PENDING', NOW())
        ON CONFLICT (month) DO UPDATE SET
            challan_number = EXCLUDED.challan_number,
            challan_date   = EXCLUDED.challan_date,
            challan_status = 'PENDING',
            total_taxable  = EXCLUDED.total_taxable,
            gst_amount     = EXCLUDED.gst_amount,
            cgst_amount    = EXCLUDED.cgst_amount,
            sgst_amount    = EXCLUDED.sgst_amount,
            total_invoices = EXCLUDED.total_invoices,
            updated_at     = NOW()
    """
        ),
        {
            "month": payload.month,
            "year": year_val,
            "month_num": month_val,
            "taxable": float(taxable),
            "gst": float(gst_amount),
            "invoices": total_invoices,
            "cgst": float(cgst),
            "sgst": float(sgst),
            "challan_no": payload.challan_number.upper().strip(),
        },
    )
    await db.commit()

    return GenerateChallanResponse(
        success=True,
        message=f"Challan {payload.challan_number.upper()} generated for {MONTH_NAMES[month_val]} {year_val}.",
        challan_number=payload.challan_number.upper(),
        month=payload.month,
        gst_amount=round(float(gst_amount), 2),
        cgst_amount=float(cgst),
        sgst_amount=float(sgst),
    )


class MarkChallanFiledRequest(BaseModel):
    month: str
    filed_date: Optional[str] = None
    notes: Optional[str] = None


@router.post("/gst/mark-filed")
async def mark_challan_filed(
    payload: MarkChallanFiledRequest,
    db: AsyncSession = Depends(get_db),
):
    """Mark a GST challan as FILED after payment on the GST portal."""
    row = (
        await db.execute(
            text("SELECT id FROM gst_challan_records WHERE month = :m"),
            {"m": payload.month},
        )
    ).one_or_none()
    if not row:
        raise HTTPException(404, "No challan found for this month. Generate it first.")

    filed_date = payload.filed_date or str(date.today())
    await db.execute(
        text(
            """
        UPDATE gst_challan_records
        SET challan_status = 'FILED', filed_date = :fd, notes = :notes, updated_at = NOW()
        WHERE month = :m
    """
        ),
        {"fd": filed_date, "notes": payload.notes, "m": payload.month},
    )
    await db.commit()
    return {"success": True, "message": f"Challan for {payload.month} marked as FILED."}


# ═══════════════════════════════════════════════════════════
# 3. TDS REPORTS (B2B Company Partners only — Section 194C)
# 1% TDS on payout to company partners with PAN.
# Filed quarterly via Form 26Q.
# ═══════════════════════════════════════════════════════════


class TDSRecord(BaseModel):
    partner_id: int
    partner_name: str
    partner_code: str
    partner_type: str
    gst_number: Optional[str]
    pan_number: Optional[str]
    total_payout: float
    tds_amount: float
    tds_deducted: float
    tds_gap: float
    net_payout: float
    total_bookings: int
    deducted_bookings: int
    period: str
    financial_year: Optional[str]
    quarter: Optional[str]


class TDSReportResponse(BaseModel):
    period: str
    tds_enabled: bool
    tds_rate: float
    total_tds: float
    total_deducted: float
    total_gap: float
    total_payout: float
    records: List[TDSRecord]


@router.get("/tds", response_model=TDSReportResponse)
async def tds_report(
    period: str = Query("month"),
    year: Optional[int] = Query(None),
    month: Optional[int] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """
    TDS report for B2B (COMPANY) partners only.
    Individual partners are excluded (not liable for TDS under 194C
    unless single payment > INR 30,000 — handled separately).

    Reports both the computed liability (TDS due on payout) and the amount
    actually withheld at settlement (tds_deduction_records), so the admin can
    see the gap. The rate comes from the TDS_RATE system configuration.
    """
    now = datetime.now(timezone.utc)
    yr = year or now.year
    mn = month or now.month

    cfg_rows = (
        (
            await db.execute(
                text(
                    "SELECT config_key, config_value FROM system_configurations "
                    "WHERE config_key IN ('TDS_ENABLED', 'TDS_RATE')"
                )
            )
        )
        .mappings()
        .all()
    )
    cfg = {r["config_key"]: r["config_value"] for r in cfg_rows}
    tds_enabled = str(cfg.get("TDS_ENABLED", "false")).lower() == "true"
    tds_rate_pct = Decimal(str(cfg.get("TDS_RATE") or "1"))
    tds_rate = tds_rate_pct / Decimal("100")

    if period == "month":
        filters = [
            extract("year", CabBooking.updated_at) == yr,
            extract("month", CabBooking.updated_at) == mn,
        ]
        period_label = f"{yr}-{mn:02d}"
        ref_date = date(yr, mn, 1)
    elif period == "year":
        filters = [extract("year", CabBooking.updated_at) == yr]
        period_label = str(yr)
        ref_date = date(yr, 1, 1)
    else:
        filters = []
        period_label = "all"
        ref_date = now.date()

    if not tds_enabled:
        return TDSReportResponse(
            period=period_label,
            tds_enabled=False,
            tds_rate=float(tds_rate_pct),
            total_tds=0.0,
            total_deducted=0.0,
            total_gap=0.0,
            total_payout=0.0,
            records=[],
        )

    stmt = (
        select(
            Partner.id.label("partner_id"),
            Partner.business_name.label("partner_name"),
            Partner.partner_code,
            Partner.partner_type,
            PartnerGSTDetails.gst_number,
            PartnerGSTDetails.pan_number,
            func.coalesce(func.sum(CabBooking.partner_payout), 0).label("total_payout"),
            func.count(CabBooking.id).label("total_bookings"),
        )
        .join(CabBookingAssignment, CabBookingAssignment.partner_id == Partner.id)
        .join(CabBooking, CabBooking.id == CabBookingAssignment.cab_booking_id)
        .join(
            PartnerGSTDetails, PartnerGSTDetails.partner_id == Partner.id, isouter=True
        )
        .where(
            and_(
                Partner.partner_type == "COMPANY",
                CabBooking.partner_payout.isnot(None),
                CabBooking.booking_status.in_(["COMPLETED", "SETTLED"]),
                *filters,
            )
        )
        .group_by(
            Partner.id,
            Partner.business_name,
            Partner.partner_code,
            Partner.partner_type,
            PartnerGSTDetails.gst_number,
            PartnerGSTDetails.pan_number,
        )
        .order_by(func.sum(CabBooking.partner_payout).desc())
    )
    rows = (await db.execute(stmt)).all()

    # Amounts actually withheld at settlement, keyed by partner.
    deducted_stmt = (
        select(
            CabBookingAssignment.partner_id.label("partner_id"),
            func.coalesce(func.sum(_tds_records.c.tds_amount), 0).label("tds_deducted"),
            func.count(_tds_records.c.id).label("deducted_bookings"),
        )
        .select_from(CabBooking)
        .join(
            CabBookingAssignment, CabBookingAssignment.cab_booking_id == CabBooking.id
        )
        .join(_tds_records, _tds_records.c.cab_booking_id == CabBooking.id)
        .group_by(CabBookingAssignment.partner_id)
    )
    if filters:
        deducted_stmt = deducted_stmt.where(and_(*filters))
    deducted_map = {
        r["partner_id"]: r for r in (await db.execute(deducted_stmt)).mappings().all()
    }

    records = []
    total_tds = Decimal("0")
    total_deducted = Decimal("0")
    total_payout = Decimal("0")
    fy = _financial_year(ref_date)
    q = _quarter(ref_date) if period == "month" else None

    for r in rows:
        payout = Decimal(str(r.total_payout))
        tds_due = (payout * tds_rate).quantize(Decimal("0.01"))
        d = deducted_map.get(r.partner_id)
        deducted = (
            Decimal(str(d["tds_deducted"])).quantize(Decimal("0.01"))
            if d
            else Decimal("0.00")
        )
        gap = tds_due - deducted
        total_tds += tds_due
        total_deducted += deducted
        total_payout += payout
        records.append(
            TDSRecord(
                partner_id=r.partner_id,
                partner_name=r.partner_name or r.partner_code,
                partner_code=r.partner_code,
                partner_type=r.partner_type,
                gst_number=r.gst_number,
                pan_number=r.pan_number,
                total_payout=round(float(payout), 2),
                tds_amount=round(float(tds_due), 2),
                tds_deducted=round(float(deducted), 2),
                tds_gap=round(float(gap), 2),
                net_payout=round(float(payout - deducted), 2),
                total_bookings=int(r.total_bookings),
                deducted_bookings=int(d["deducted_bookings"]) if d else 0,
                period=period_label,
                financial_year=fy,
                quarter=q,
            )
        )

    return TDSReportResponse(
        period=period_label,
        tds_enabled=True,
        tds_rate=float(tds_rate_pct),
        total_tds=round(float(total_tds), 2),
        total_deducted=round(float(total_deducted), 2),
        total_gap=round(float(total_tds - total_deducted), 2),
        total_payout=round(float(total_payout), 2),
        records=records,
    )


# ═══════════════════════════════════════════════════════════
# 4. PARTNER REPORT
# ═══════════════════════════════════════════════════════════


class PartnerReportSummary(BaseModel):
    partner_id: int
    partner_name: str
    partner_code: str
    partner_type: str
    city_name: Optional[str]
    total_bookings: int
    completed_bookings: int
    cancelled_bookings: int
    total_revenue: float
    total_gst: float
    total_commission: float
    total_payout: float
    average_trip_amount: float
    settled_bookings: int


class PartnerBookingDetail(BaseModel):
    booking_number: str
    cab_booking_number: Optional[str]
    journey_date: Optional[str]
    trip_type: Optional[str]
    pickup_location: Optional[str]
    drop_location: Optional[str]
    final_amount: Optional[float]
    gst_amount: Optional[float]
    is_tax_invoice: bool
    platform_commission: Optional[float]
    partner_payout: Optional[float]
    payment_mode: Optional[str]
    cab_status: Optional[str]
    invoice_number: Optional[str]
    created_at: str


class PartnerReportResponse(BaseModel):
    summary: PartnerReportSummary
    items: List[PartnerBookingDetail]
    total: int
    page: int
    page_size: int
    total_pages: int


@router.get("/partner/{partner_id}", response_model=PartnerReportResponse)
async def partner_report(
    partner_id: int,
    period: str = Query("month"),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    partner = (
        await db.execute(select(Partner).where(Partner.id == partner_id))
    ).scalar_one_or_none()
    if not partner:
        raise HTTPException(404, "Partner not found.")
    city_name = (
        await db.execute(select(City.name).where(City.id == partner.city_id))
    ).scalar_one_or_none()

    now = datetime.now(timezone.utc)
    filters = [CabBookingAssignment.partner_id == partner_id]
    if period == "custom":
        if date_from:
            filters.append(
                MasterBooking.created_at
                >= datetime.fromisoformat(date_from).replace(tzinfo=timezone.utc)
            )
        if date_to:
            filters.append(
                MasterBooking.created_at
                <= datetime.fromisoformat(date_to).replace(tzinfo=timezone.utc)
            )
    elif period != "all":
        if period == "today":
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == "week":
            start = now - timedelta(days=7)
        elif period == "month":
            start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        elif period == "year":
            start = now.replace(
                month=1, day=1, hour=0, minute=0, second=0, microsecond=0
            )
        else:
            start = None
        if start:
            filters.append(MasterBooking.created_at >= start)

    summary_stmt = (
        select(
            func.count(MasterBooking.id).label("total_bookings"),
            func.sum(
                case(
                    (MasterBooking.booking_status.in_(["COMPLETED", "CLOSED"]), 1),
                    else_=0,
                )
            ).label("completed"),
            func.sum(
                case((MasterBooking.booking_status.in_(["CANCELLED"]), 1), else_=0)
            ).label("cancelled"),
            func.sum(case((CabBooking.booking_status == "SETTLED", 1), else_=0)).label(
                "settled"
            ),
            func.coalesce(func.sum(CabBooking.final_amount), 0).label("total_revenue"),
            func.coalesce(
                func.sum(text("COALESCE(cab_bookings.gst_amount, 0)")), 0
            ).label("total_gst"),
            func.coalesce(func.sum(CabBooking.platform_commission), 0).label(
                "total_commission"
            ),
            func.coalesce(func.sum(CabBooking.partner_payout), 0).label("total_payout"),
            func.coalesce(func.avg(CabBooking.final_amount), 0).label("avg_amount"),
        )
        .select_from(CabBookingAssignment)
        .join(CabBooking, CabBooking.id == CabBookingAssignment.cab_booking_id)
        .join(MasterBooking, MasterBooking.id == CabBooking.master_booking_id)
        .where(and_(*filters))
    )
    s = (await db.execute(summary_stmt)).one()
    summary = PartnerReportSummary(
        partner_id=partner.id,
        partner_name=partner.business_name or partner.owner_name,
        partner_code=partner.partner_code,
        partner_type=partner.partner_type,
        city_name=city_name,
        total_bookings=int(s.total_bookings or 0),
        completed_bookings=int(s.completed or 0),
        cancelled_bookings=int(s.cancelled or 0),
        total_revenue=round(float(s.total_revenue or 0), 2),
        total_gst=round(float(s.total_gst or 0), 2),
        total_commission=round(float(s.total_commission or 0), 2),
        total_payout=round(float(s.total_payout or 0), 2),
        average_trip_amount=round(float(s.avg_amount or 0), 2),
        settled_bookings=int(s.settled or 0),
    )

    list_stmt = (
        select(
            MasterBooking.booking_number,
            MasterBooking.booking_status,
            MasterBooking.journey_start_date,
            MasterBooking.created_at,
            CabBooking.booking_number.label("cab_booking_number"),
            CabBooking.trip_type,
            CabBooking.pickup_location,
            CabBooking.drop_location,
            CabBooking.final_amount,
            CabBooking.platform_commission,
            CabBooking.partner_payout,
            CabBooking.payment_mode,
            CabBooking.booking_status.label("cab_status"),
            CabBooking.invoice_number,
            text("COALESCE(cab_bookings.gst_amount, 0) AS gst_amount"),
            text("COALESCE(cab_bookings.is_tax_invoice, false) AS is_tax_invoice"),
        )
        .select_from(CabBookingAssignment)
        .join(CabBooking, CabBooking.id == CabBookingAssignment.cab_booking_id)
        .join(MasterBooking, MasterBooking.id == CabBooking.master_booking_id)
        .where(and_(*filters))
        .order_by(MasterBooking.created_at.desc())
    )
    total = (
        await db.execute(select(func.count()).select_from(list_stmt.subquery()))
    ).scalar_one() or 0
    rows = (
        (await db.execute(list_stmt.offset((page - 1) * page_size).limit(page_size)))
        .mappings()
        .all()
    )

    items = [
        PartnerBookingDetail(
            booking_number=r["booking_number"],
            cab_booking_number=r["cab_booking_number"],
            journey_date=(
                r["journey_start_date"].isoformat() if r["journey_start_date"] else None
            ),
            trip_type=r["trip_type"],
            pickup_location=r["pickup_location"],
            drop_location=r["drop_location"],
            final_amount=float(r["final_amount"]) if r["final_amount"] else None,
            gst_amount=float(r["gst_amount"]) if r.get("gst_amount") else 0.0,
            is_tax_invoice=bool(r.get("is_tax_invoice", False)),
            platform_commission=(
                float(r["platform_commission"]) if r["platform_commission"] else None
            ),
            partner_payout=float(r["partner_payout"]) if r["partner_payout"] else None,
            payment_mode=r["payment_mode"],
            cab_status=r["cab_status"],
            invoice_number=r["invoice_number"],
            created_at=r["created_at"].isoformat() if r["created_at"] else "",
        )
        for r in rows
    ]

    return PartnerReportResponse(
        summary=summary,
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=max(1, math.ceil(total / page_size)),
    )


class PartnerOption(BaseModel):
    id: int
    name: str
    code: str
    type: str


@router.get("/partner-options", response_model=List[PartnerOption])
async def partner_options(db: AsyncSession = Depends(get_db)):
    stmt = (
        select(
            Partner.id,
            Partner.business_name,
            Partner.owner_name,
            Partner.partner_code,
            Partner.partner_type,
        )
        .where(Partner.status == "ACTIVE")
        .order_by(Partner.business_name)
    )
    rows = (await db.execute(stmt)).all()
    return [
        PartnerOption(
            id=r.id,
            name=r.business_name or r.owner_name,
            code=r.partner_code,
            type=r.partner_type,
        )
        for r in rows
    ]


# ═══════════════════════════════════════════════════════════
# 5. CUSTOMER REPORT
# ═══════════════════════════════════════════════════════════


class CustomerReportItem(BaseModel):
    customer_id: int
    customer_name: Optional[str]
    customer_mobile: Optional[str]
    customer_email: Optional[str]
    city_name: Optional[str]
    total_bookings: int
    completed_bookings: int
    total_spent: float
    last_booking_date: Optional[str]
    first_booking_date: Optional[str]
    avg_booking_value: float


class CustomerReportResponse(BaseModel):
    total: int
    page: int
    page_size: int
    total_pages: int
    summary: dict
    items: List[CustomerReportItem]


@router.get("/customers", response_model=CustomerReportResponse)
async def customer_report(
    period: str = Query("month"),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    city_id: Optional[int] = Query(None),
    search: Optional[str] = Query(None),
    min_bookings: Optional[int] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    now = datetime.now(timezone.utc)
    filters = []
    if period == "custom":
        if date_from:
            filters.append(
                MasterBooking.created_at
                >= datetime.fromisoformat(date_from).replace(tzinfo=timezone.utc)
            )
        if date_to:
            filters.append(
                MasterBooking.created_at
                <= datetime.fromisoformat(date_to).replace(tzinfo=timezone.utc)
            )
    elif period != "all":
        if period == "today":
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == "week":
            start = now - timedelta(days=7)
        elif period == "month":
            start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        elif period == "year":
            start = now.replace(
                month=1, day=1, hour=0, minute=0, second=0, microsecond=0
            )
        else:
            start = None
        if start:
            filters.append(MasterBooking.created_at >= start)
    if city_id:
        filters.append(MasterBooking.city_id == city_id)
    if search:
        like = f"%{search}%"
        filters.append(
            or_(
                User.first_name.ilike(like),
                User.last_name.ilike(like),
                User.mobile_number.ilike(like),
                User.email.ilike(like),
            )
        )

    stmt = (
        select(
            Customer.id.label("customer_id"),
            func.concat(User.first_name, " ", func.coalesce(User.last_name, "")).label(
                "customer_name"
            ),
            User.mobile_number.label("customer_mobile"),
            User.email.label("customer_email"),
            City.name.label("city_name"),
            func.count(MasterBooking.id).label("total_bookings"),
            func.sum(
                case(
                    (MasterBooking.booking_status.in_(["COMPLETED", "CLOSED"]), 1),
                    else_=0,
                )
            ).label("completed_bookings"),
            func.coalesce(func.sum(MasterBooking.total_paid_amount), 0).label(
                "total_spent"
            ),
            func.max(MasterBooking.created_at).label("last_booking_date"),
            func.min(MasterBooking.created_at).label("first_booking_date"),
            func.coalesce(func.avg(MasterBooking.total_paid_amount), 0).label(
                "avg_value"
            ),
        )
        .join(User, User.id == Customer.user_id)
        .join(MasterBooking, MasterBooking.customer_id == Customer.id)
        .join(City, City.id == Customer.city_id, isouter=True)
        .where(and_(*filters) if filters else text("1=1"))
        .group_by(
            Customer.id,
            User.first_name,
            User.last_name,
            User.mobile_number,
            User.email,
            City.name,
        )
        .order_by(func.sum(MasterBooking.total_paid_amount).desc())
    )
    if min_bookings:
        stmt = stmt.having(func.count(MasterBooking.id) >= min_bookings)

    total = (
        await db.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one() or 0
    sum_stmt = select(
        func.count(func.distinct(MasterBooking.customer_id)).label("unique_customers"),
        func.coalesce(func.sum(MasterBooking.total_paid_amount), 0).label(
            "total_revenue"
        ),
        func.coalesce(func.avg(MasterBooking.total_paid_amount), 0).label(
            "avg_booking"
        ),
    ).where(and_(*filters) if filters else text("1=1"))
    s = (await db.execute(sum_stmt)).one()
    rows = (
        (await db.execute(stmt.offset((page - 1) * page_size).limit(page_size)))
        .mappings()
        .all()
    )

    items = [
        CustomerReportItem(
            customer_id=r["customer_id"],
            customer_name=r["customer_name"],
            customer_mobile=r["customer_mobile"],
            customer_email=r["customer_email"],
            city_name=r["city_name"],
            total_bookings=int(r["total_bookings"]),
            completed_bookings=int(r["completed_bookings"] or 0),
            total_spent=round(float(r["total_spent"]), 2),
            last_booking_date=(
                r["last_booking_date"].isoformat() if r["last_booking_date"] else None
            ),
            first_booking_date=(
                r["first_booking_date"].isoformat() if r["first_booking_date"] else None
            ),
            avg_booking_value=round(float(r["avg_value"]), 2),
        )
        for r in rows
    ]

    return CustomerReportResponse(
        total=total,
        page=page,
        page_size=page_size,
        total_pages=max(1, math.ceil(total / page_size)),
        summary={
            "unique_customers": int(s.unique_customers or 0),
            "total_revenue": round(float(s.total_revenue or 0), 2),
            "avg_booking_value": round(float(s.avg_booking or 0), 2),
        },
        items=items,
    )
