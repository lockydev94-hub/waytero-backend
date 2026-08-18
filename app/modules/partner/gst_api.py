# ============================================================
# WAYTERO — PARTNER GST CHALLAN API
# File: app/modules/partner/gst_api.py
# Prefix: /partners  (registered in api/router.py)
#
# India GST Rules for Transport (SAC 9964):
#   COMPANY partners  — have GSTIN, file their own GSTR-1/3B monthly.
#                        Platform generates challan for them to reference.
#   INDIVIDUAL partners — not registered; platform files on their behalf.
#                          Admin handles this from the admin GST tab.
#
# Endpoints:
#   GET  /partners/me/gst/summary          — monthly challan list for this partner
#   GET  /partners/me/gst/challan/{month}  — detail for a specific month
#   POST /partners/me/gst/challan          — generate/update challan for a month
#   POST /partners/me/gst/mark-filed       — mark challan as FILED
#
# Gating:
#   All mutating endpoints are gated on the platform-level `GST_ENABLED` flag
#   in `system_configurations` (seeded by migration 0024). When the platform
#   admin has GST turned off, writes return 404 — the partner-side summary
#   endpoint still serves a payload with `is_gst_enabled=false` so the
#   frontend can render a graceful "feature disabled" state.
#
# Doc Ref: BRD §155 GST Integration, §156 TDS
#          Migration 0024 — GST_ENABLED config key
#          Migration 0029 — partner_gst_challans table
# ============================================================

from datetime import datetime, timezone, date
from decimal import Decimal
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func, text, and_, extract
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.exceptions import ResourceNotFoundException
from app.modules.booking.models import CabBooking, CabBookingAssignment
from app.modules.partner.models import Partner, PartnerGSTDetails

router = APIRouter()

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


async def _resolve_partner(db: AsyncSession, user_uuid: str) -> Partner:
    from uuid import UUID

    p = (
        await db.execute(select(Partner).where(Partner.user_id == UUID(user_uuid)))
    ).scalar_one_or_none()
    if not p:
        raise HTTPException(404, "Partner profile not found.")
    return p


async def _require_gst_enabled(db: AsyncSession) -> None:
    """
    Block mutating partner-GST endpoints when the platform has GST disabled.

    Reads the same `GST_ENABLED` row from `system_configurations` that the
    summary endpoint reads (see partner_gst_summary below). On `false`, the
    partner-side GST feature is treated as not present on the platform and
    we raise a 404 — mirroring the "feature does not exist" semantics rather
    than a 403, so a stale tab can't distinguish "you can't" from "it's not
    here." The summary endpoint intentionally still returns 200 with
    `is_gst_enabled=false` so the frontend can render a graceful state.
    """
    row = (
        await db.execute(
            text(
                "SELECT config_value FROM system_configurations WHERE config_key = 'GST_ENABLED'"
            )
        )
    ).first()
    enabled = (row[0] if row else "false").lower() == "true"
    if not enabled:
        raise ResourceNotFoundException(resource="GST Challan feature")


# ════════════════════════════════════════════════════════════
# SCHEMAS
# ════════════════════════════════════════════════════════════


class PartnerGSTMonthRecord(BaseModel):
    month: str  # "YYYY-MM"
    month_label: str
    year: int
    total_bookings: int
    taxable_amount: float
    gst_amount: float
    cgst_amount: float
    sgst_amount: float
    gst_number: Optional[str]
    challan_number: Optional[str]
    challan_date: Optional[str]
    challan_status: str  # PENDING | GENERATED | FILED
    filed_date: Optional[str]
    has_bookings: bool


class PartnerGSTSummaryResponse(BaseModel):
    year: int
    partner_type: str
    gst_number: Optional[str]
    is_gst_enabled: bool
    records: List[PartnerGSTMonthRecord]
    yearly_summary: dict


class GeneratePartnerChallanRequest(BaseModel):
    month: str
    challan_number: Optional[str] = None
    notes: Optional[str] = None


class MarkPartnerFiledRequest(BaseModel):
    month: str
    filed_date: Optional[str] = None
    notes: Optional[str] = None


# ════════════════════════════════════════════════════════════
# ENDPOINTS
# ════════════════════════════════════════════════════════════


@router.get("/me/gst/summary", response_model=PartnerGSTSummaryResponse)
async def partner_gst_summary(
    year: int = Query(None),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Monthly GST summary for the authenticated partner.
    - COMPANY partners: shows their own GST (they have GSTIN, file themselves).
    - INDIVIDUAL: shows GST collected (filed by admin on their behalf).
    """
    partner = await _resolve_partner(db, current_user["sub"])
    if not year:
        year = datetime.now(timezone.utc).year

    # Check GST enabled in platform config
    gst_cfg = (
        (
            await db.execute(
                text(
                    "SELECT config_key, config_value FROM system_configurations WHERE config_key IN ('GST_ENABLED', 'GST_RATE')"
                )
            )
        )
        .mappings()
        .all()
    )
    cfg_map = {r["config_key"]: r["config_value"] for r in gst_cfg}
    is_gst_enabled = cfg_map.get("GST_ENABLED", "false").lower() == "true"

    # GST details
    gst_row = (
        await db.execute(
            select(PartnerGSTDetails).where(PartnerGSTDetails.partner_id == partner.id)
        )
    ).scalar_one_or_none()
    gst_number = gst_row.gst_number if gst_row else None

    # Monthly aggregation: only GST bookings for THIS partner
    stmt = (
        select(
            extract("month", CabBooking.updated_at).label("month_num"),
            func.count(CabBooking.id).label("total_bookings"),
            func.coalesce(func.sum(CabBooking.final_amount), 0).label("total_revenue"),
            func.coalesce(
                func.sum(text("COALESCE(cab_bookings.gst_amount, 0)")), 0
            ).label("total_gst"),
        )
        .join(
            CabBookingAssignment, CabBookingAssignment.cab_booking_id == CabBooking.id
        )
        .where(
            and_(
                CabBookingAssignment.partner_id == partner.id,
                extract("year", CabBooking.updated_at) == year,
                CabBooking.booking_status.in_(["COMPLETED", "SETTLED"]),
                CabBooking.final_amount.isnot(None),
                text("COALESCE(cab_bookings.is_tax_invoice, false) = true"),
            )
        )
        .group_by(extract("month", CabBooking.updated_at))
    )
    rows = (await db.execute(stmt)).all()
    existing = {int(r.month_num): r for r in rows}

    # Load persisted challan records
    challan_rows = (
        (
            await db.execute(
                text(
                    """
            SELECT month, challan_number, challan_date, challan_status, filed_date
            FROM partner_gst_challans
            WHERE partner_id = :pid AND year = :yr
        """
                ),
                {"pid": partner.id, "yr": year},
            )
        )
        .mappings()
        .all()
    )
    challan_map = {r["month"]: r for r in challan_rows}

    records = []
    total_yr_gst = Decimal("0")
    total_yr_taxable = Decimal("0")

    for m in range(1, 13):
        month_key = f"{year}-{m:02d}"
        month_label = f"{MONTH_NAMES[m]} {year}"
        challan = challan_map.get(month_key)

        if m in existing:
            r = existing[m]
            gst_amt = Decimal(str(r.total_gst))
            total_rev = Decimal(str(r.total_revenue))
            taxable = total_rev - gst_amt
            cgst = (gst_amt / 2).quantize(Decimal("0.01"))
            sgst = (gst_amt / 2).quantize(Decimal("0.01"))
            total_yr_gst += gst_amt
            total_yr_taxable += taxable
            status = challan["challan_status"] if challan else "PENDING"
            records.append(
                PartnerGSTMonthRecord(
                    month=month_key,
                    month_label=month_label,
                    year=year,
                    total_bookings=int(r.total_bookings),
                    taxable_amount=round(float(taxable), 2),
                    gst_amount=round(float(gst_amt), 2),
                    cgst_amount=float(cgst),
                    sgst_amount=float(sgst),
                    gst_number=gst_number,
                    challan_number=challan["challan_number"] if challan else None,
                    challan_date=(
                        str(challan["challan_date"])
                        if challan and challan["challan_date"]
                        else None
                    ),
                    challan_status=status,
                    filed_date=(
                        str(challan["filed_date"])
                        if challan and challan.get("filed_date")
                        else None
                    ),
                    has_bookings=True,
                )
            )
        else:
            records.append(
                PartnerGSTMonthRecord(
                    month=month_key,
                    month_label=month_label,
                    year=year,
                    total_bookings=0,
                    taxable_amount=0.0,
                    gst_amount=0.0,
                    cgst_amount=0.0,
                    sgst_amount=0.0,
                    gst_number=gst_number,
                    challan_number=challan["challan_number"] if challan else None,
                    challan_date=(
                        str(challan["challan_date"])
                        if challan and challan["challan_date"]
                        else None
                    ),
                    challan_status=challan["challan_status"] if challan else "PENDING",
                    filed_date=(
                        str(challan["filed_date"])
                        if challan and challan.get("filed_date")
                        else None
                    ),
                    has_bookings=False,
                )
            )

    return PartnerGSTSummaryResponse(
        year=year,
        partner_type=partner.partner_type,
        gst_number=gst_number,
        is_gst_enabled=is_gst_enabled,
        records=records,
        yearly_summary={
            "total_gst": round(float(total_yr_gst), 2),
            "total_taxable": round(float(total_yr_taxable), 2),
            "total_cgst": round(float(total_yr_gst / 2), 2),
            "total_sgst": round(float(total_yr_gst / 2), 2),
        },
    )


@router.post("/me/gst/challan")
async def generate_partner_challan(
    payload: GeneratePartnerChallanRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    COMPANY partners generate/update their GST challan reference for a month.
    INDIVIDUAL partners: admin handles — still allowed to view but not file.
    Blocked at the platform level when GST_ENABLED is off (see _require_gst_enabled).
    """
    await _require_gst_enabled(db)
    partner = await _resolve_partner(db, current_user["sub"])

    try:
        year_str, month_str = payload.month.split("-")
        year_val = int(year_str)
        month_val = int(month_str)
    except Exception:
        raise HTTPException(400, "Invalid month format. Use YYYY-MM.")

    # GST details
    gst_row = (
        await db.execute(
            select(PartnerGSTDetails).where(PartnerGSTDetails.partner_id == partner.id)
        )
    ).scalar_one_or_none()
    gst_number = gst_row.gst_number if gst_row else None

    # Aggregate bookings
    stmt = (
        select(
            func.coalesce(
                func.sum(text("COALESCE(cab_bookings.gst_amount, 0)")), 0
            ).label("total_gst"),
            func.coalesce(func.sum(CabBooking.final_amount), 0).label("total_revenue"),
            func.count(CabBooking.id).label("total_bookings"),
        )
        .join(
            CabBookingAssignment, CabBookingAssignment.cab_booking_id == CabBooking.id
        )
        .where(
            and_(
                CabBookingAssignment.partner_id == partner.id,
                extract("year", CabBooking.updated_at) == year_val,
                extract("month", CabBooking.updated_at) == month_val,
                CabBooking.booking_status.in_(["COMPLETED", "SETTLED"]),
                CabBooking.final_amount.isnot(None),
                text("COALESCE(cab_bookings.is_tax_invoice, false) = true"),
            )
        )
    )
    row = (await db.execute(stmt)).one()
    gst_amount = Decimal(str(row.total_gst))
    total_revenue = Decimal(str(row.total_revenue))
    taxable = total_revenue - gst_amount
    cgst = (gst_amount / 2).quantize(Decimal("0.01"))
    sgst = (gst_amount / 2).quantize(Decimal("0.01"))
    total_bookings = int(row.total_bookings)

    challan_no = (payload.challan_number or "").upper().strip() or None
    status = "GENERATED" if challan_no else "PENDING"

    await db.execute(
        text(
            """
        INSERT INTO partner_gst_challans
            (partner_id, month, year, month_number, total_bookings,
             taxable_amount, gst_amount, cgst_amount, sgst_amount,
             gst_number, challan_number, challan_date, challan_status, notes, updated_at)
        VALUES
            (:pid, :month, :yr, :mn, :bk,
             :taxable, :gst, :cgst, :sgst,
             :gst_no, :challan_no, CASE WHEN :challan_no IS NOT NULL THEN CURRENT_DATE ELSE NULL END,
             :status, :notes, NOW())
        ON CONFLICT (partner_id, month) DO UPDATE SET
            total_bookings  = EXCLUDED.total_bookings,
            taxable_amount  = EXCLUDED.taxable_amount,
            gst_amount      = EXCLUDED.gst_amount,
            cgst_amount     = EXCLUDED.cgst_amount,
            sgst_amount     = EXCLUDED.sgst_amount,
            gst_number      = EXCLUDED.gst_number,
            challan_number  = COALESCE(EXCLUDED.challan_number, partner_gst_challans.challan_number),
            challan_date    = CASE WHEN EXCLUDED.challan_number IS NOT NULL THEN CURRENT_DATE ELSE partner_gst_challans.challan_date END,
            challan_status  = CASE WHEN EXCLUDED.challan_number IS NOT NULL THEN EXCLUDED.challan_status ELSE partner_gst_challans.challan_status END,
            notes           = COALESCE(EXCLUDED.notes, partner_gst_challans.notes),
            updated_at      = NOW()
    """
        ),
        {
            "pid": partner.id,
            "month": payload.month,
            "yr": year_val,
            "mn": month_val,
            "bk": total_bookings,
            "taxable": float(taxable),
            "gst": float(gst_amount),
            "cgst": float(cgst),
            "sgst": float(sgst),
            "gst_no": gst_number,
            "challan_no": challan_no,
            "status": status,
            "notes": payload.notes,
        },
    )
    await db.commit()

    return {
        "success": True,
        "message": f"Challan {'recorded' if challan_no else 'initialized'} for {MONTH_NAMES[month_val]} {year_val}.",
        "month": payload.month,
        "gst_amount": round(float(gst_amount), 2),
        "cgst_amount": float(cgst),
        "sgst_amount": float(sgst),
        "challan_number": challan_no,
        "status": status,
    }


@router.post("/me/gst/mark-filed")
async def mark_partner_challan_filed(
    payload: MarkPartnerFiledRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Mark a partner GST challan as FILED after paying on GST portal.
    Blocked at the platform level when GST_ENABLED is off (see _require_gst_enabled)."""
    await _require_gst_enabled(db)
    partner = await _resolve_partner(db, current_user["sub"])

    row = (
        await db.execute(
            text(
                "SELECT id FROM partner_gst_challans WHERE partner_id = :pid AND month = :m"
            ),
            {"pid": partner.id, "m": payload.month},
        )
    ).one_or_none()
    if not row:
        raise HTTPException(404, "No challan found for this month. Generate it first.")

    filed_date = payload.filed_date or str(date.today())
    await db.execute(
        text(
            """
        UPDATE partner_gst_challans
        SET challan_status = 'FILED', filed_date = :fd, notes = COALESCE(:notes, notes), updated_at = NOW()
        WHERE partner_id = :pid AND month = :m
    """
        ),
        {
            "fd": filed_date,
            "notes": payload.notes,
            "pid": partner.id,
            "m": payload.month,
        },
    )
    await db.commit()

    return {"success": True, "message": f"Challan for {payload.month} marked as FILED."}
