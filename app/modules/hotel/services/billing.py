# ============================================================
# WAY TERO — HOTEL BILLING SERVICE
# File: app/modules/hotel/services/billing.py
# Doc Ref: BRD Part 4 §57-92 — Hotel Booking Lifecycle
#          BRD Part 3 §45 — Advance collection & settlement custody
# Migration: 0035_hotel_payments
#
# Assembles the final bill an admin sees at check-out, in the order a real
# property-management system does it:
#
#     room tariff  (nights x rate, already snapshotted at booking)
#   + late check-out / overtime
#   + extra charges (food, laundry, damage, minibar...)
#   - coupon discount
#   ────────────────────────────────
#   = taxable amount
#   + GST                       (only if the platform switch is on)
#   ────────────────────────────────
#   = grand total
#   - advances already received
#   ────────────────────────────────
#   = balance due at the desk
#
# GST is delegated to pricing.compute_tax so the tariff-slab rules and the
# INCLUSIVE back-out arithmetic stay in exactly one place.
# ============================================================

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Optional, Sequence

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import (
    as_utc as _legacy_as_utc,
    to_platform_tz as _to_platform_tz,
    get_platform_tz_sync as _get_platform_tz,
)
from app.modules.hotel.models import (
    Hotel,
    HotelAdvancePayment,
    HotelGstSlab,
    HotelReservation,
)
from app.modules.hotel.services.pricing import (
    ZERO,
    GstSlabInput,
    compute_tax,
    money,
)

# ── Config keys (seeded by migration 0035) ──────────────────────
CONFIG_CHECKOUT_TIME = "HOTEL_CHECKOUT_TIME"
CONFIG_GRACE_MINUTES = "HOTEL_OVERTIME_GRACE_MINUTES"
CONFIG_OVERTIME_MODE = "HOTEL_OVERTIME_MODE"
CONFIG_HOURLY_PERCENT = "HOTEL_OVERTIME_HOURLY_PERCENT"
CONFIG_HALFDAY_PERCENT = "HOTEL_OVERTIME_HALFDAY_PERCENT"
CONFIG_FULLDAY_PERCENT = "HOTEL_OVERTIME_FULLDAY_PERCENT"
CONFIG_HALFDAY_UNTIL_HOURS = "HOTEL_OVERTIME_HALFDAY_UNTIL_HOURS"

OVERTIME_MODE_SLAB = "SLAB"
OVERTIME_MODE_HOURLY = "HOURLY"

# The platform's operating zone is read from PLATFORM_TIMEZONE (see
# app/core/timezone.py) and cached for 60 s. The default is Asia/Kolkata
# (seeded in migration 0009_platform_config_seed). Check-out times and night
# boundaries are platform-local, and the partner portal sends the device's
# wall-clock as a naive value while the admin portal sends UTC ISO stamps —
# everything is reduced to the platform zone before any delta is computed.
#
# `IST` is kept as a back-compat alias so external callers that imported
# `hotel_billing.IST` continue to work — it now resolves to whatever the
# platform zone is (Asia/Kolkata by default).
IST = _get_platform_tz()


def _to_ist(dt: datetime) -> datetime:
    """Back-compat shim — the real implementation lives in
    :func:`app.core.timezone.to_platform_tz`. Kept so the rest of this
    file and any external caller continues to work without changes.
    """
    return _to_platform_tz(dt)


def as_utc(dt: datetime) -> datetime:
    """Back-compat shim — see :func:`app.core.timezone.as_utc`."""
    return _legacy_as_utc(dt)


# ════════════════════════════════════════════════════════════════
# OVERTIME
# ════════════════════════════════════════════════════════════════


@dataclass
class OvertimeResult:
    """What a guest owes for staying past the standard check-out time."""

    is_overtime: bool
    scheduled_checkout_at: Optional[datetime]
    actual_checkout_at: Optional[datetime]
    overtime_hours: Decimal  # billable hours, after grace
    raw_hours: Decimal  # hours past checkout time, before grace
    grace_minutes: int
    mode: str  # SLAB | HOURLY
    slab: Optional[str]  # HALF_DAY | FULL_DAY when mode is SLAB
    percent_applied: Decimal
    nightly_rate: Decimal
    charge: Decimal
    reason: str

    def as_dict(self) -> dict:
        return {
            "is_overtime": self.is_overtime,
            "scheduled_checkout_at": (
                self.scheduled_checkout_at.isoformat()
                if self.scheduled_checkout_at
                else None
            ),
            "actual_checkout_at": (
                self.actual_checkout_at.isoformat() if self.actual_checkout_at else None
            ),
            "overtime_hours": float(self.overtime_hours),
            "raw_hours": float(self.raw_hours),
            "grace_minutes": self.grace_minutes,
            "mode": self.mode,
            "slab": self.slab,
            "percent_applied": float(self.percent_applied),
            "nightly_rate": float(self.nightly_rate),
            "charge": float(self.charge),
            "reason": self.reason,
        }


def _parse_checkout_time(raw: Optional[str]) -> time:
    """Parse an 'HH:MM' config value, falling back to 11:00 industry standard."""
    if raw:
        try:
            hh, _, mm = raw.strip().partition(":")
            return time(hour=int(hh), minute=int(mm or 0))
        except (ValueError, TypeError):
            pass
    return time(hour=11, minute=0)


def compute_overtime(
    *,
    check_out_date,
    actual_check_out_at: Optional[datetime],
    nightly_rate: Decimal,
    rooms_count: int,
    checkout_time: time,
    grace_minutes: int,
    mode: str,
    hourly_percent: Decimal,
    halfday_percent: Decimal,
    fullday_percent: Decimal,
    halfday_until_hours: int,
) -> OvertimeResult:
    """Bill a late check-out.

    The reserved period ends at `checkout_time` on the booked check-out date.
    Anything past that, once the grace window is spent, is billable.

    All stamps are reduced to the platform's operating zone (Asia/Kolkata)
    before the delta is computed — `checkout_time` is India-local and the two
    portals send the actual stamp in different frames (admin: UTC ISO, partner:
    naive wall-clock).

    SLAB is what most Indian properties actually do: a short overrun is half a
    day's tariff, a long one is a full day. HOURLY is offered for properties
    that bill by the hour. Either way the charge scales with rooms_count,
    because every room was held past its release time.
    """
    nightly_rate = money(nightly_rate or ZERO)
    rooms = max(1, int(rooms_count or 1))

    if not actual_check_out_at or not check_out_date:
        return OvertimeResult(
            is_overtime=False,
            scheduled_checkout_at=None,
            actual_checkout_at=actual_check_out_at,
            overtime_hours=ZERO,
            raw_hours=ZERO,
            grace_minutes=grace_minutes,
            mode=mode,
            slab=None,
            percent_applied=ZERO,
            nightly_rate=nightly_rate,
            charge=ZERO,
            reason="Check-out time not recorded yet.",
        )

    # `checkout_time` is the property's India-local release hour; reduce the
    # actual stamp to the same zone so the delta is computed in the wall-clock
    # the hotel (and the guest) thinks in. Naive stamps (partner portal) are
    # IST; aware stamps (admin portal) are converted.
    actual = _to_ist(actual_check_out_at)

    # An overstay's extra nights are already billed as room nights by
    # _recompute_room_charge, so overtime is measured against the check-out
    # hour of the *actual* departure day — otherwise a guest who stays two
    # nights late pays for those nights twice (room charge + full-day fee).
    # An early check-out (before the booked date) releases the room early and
    # owes nothing, which `max(...)` also preserves.
    scheduled = datetime.combine(
        max(actual.date(), check_out_date), checkout_time
    ).replace(tzinfo=IST)

    delta = actual - scheduled
    raw_hours = Decimal(str(round(delta.total_seconds() / 3600, 2)))

    # Left on or before the scheduled time — nothing owed.
    if delta <= timedelta(0):
        return OvertimeResult(
            is_overtime=False,
            scheduled_checkout_at=scheduled,
            actual_checkout_at=actual,
            overtime_hours=ZERO,
            raw_hours=raw_hours if raw_hours > 0 else ZERO,
            grace_minutes=grace_minutes,
            mode=mode,
            slab=None,
            percent_applied=ZERO,
            nightly_rate=nightly_rate,
            charge=ZERO,
            reason="Checked out on or before the standard check-out time.",
        )

    # Within the grace window — late, but not billable.
    if delta <= timedelta(minutes=grace_minutes):
        return OvertimeResult(
            is_overtime=False,
            scheduled_checkout_at=scheduled,
            actual_checkout_at=actual,
            overtime_hours=ZERO,
            raw_hours=raw_hours,
            grace_minutes=grace_minutes,
            mode=mode,
            slab=None,
            percent_applied=ZERO,
            nightly_rate=nightly_rate,
            charge=ZERO,
            reason=(
                f"Checked out {raw_hours}h late — within the "
                f"{grace_minutes}-minute grace window, not charged."
            ),
        )

    billable = delta - timedelta(minutes=grace_minutes)
    billable_hours = Decimal(str(round(billable.total_seconds() / 3600, 2)))

    if mode == OVERTIME_MODE_HOURLY:
        # Part-hours round up: an hour is started, so an hour is billed.
        charged_hours = Decimal(str(-(-billable.total_seconds() // 3600)))
        percent = hourly_percent * charged_hours
        slab_name = None
        reason = (
            f"{charged_hours}h past the {grace_minutes}-minute grace window "
            f"at {hourly_percent}% of one night per hour."
        )
    else:
        if billable_hours <= Decimal(str(halfday_until_hours)):
            percent = halfday_percent
            slab_name = "HALF_DAY"
            reason = (
                f"{billable_hours}h past the grace window — billed as a half "
                f"day at {halfday_percent}% of one night."
            )
        else:
            percent = fullday_percent
            slab_name = "FULL_DAY"
            reason = (
                f"{billable_hours}h past the grace window (over "
                f"{halfday_until_hours}h) — billed as a full day at "
                f"{fullday_percent}% of one night."
            )

    charge = money(nightly_rate * (percent / Decimal("100")) * Decimal(rooms))

    return OvertimeResult(
        is_overtime=charge > ZERO,
        scheduled_checkout_at=scheduled,
        actual_checkout_at=actual,
        overtime_hours=billable_hours,
        raw_hours=raw_hours,
        grace_minutes=grace_minutes,
        mode=mode,
        slab=slab_name,
        percent_applied=percent,
        nightly_rate=nightly_rate,
        charge=charge,
        reason=reason + (f" x {rooms} rooms." if rooms > 1 else ""),
    )


async def compute_overtime_for_reservation(
    db: AsyncSession,
    hr: HotelReservation,
    actual_check_out_at: Optional[datetime] = None,
) -> OvertimeResult:
    """Load the overtime config and apply it to one reservation."""
    from app.modules.hotel.services import (
        get_config,
        get_config_decimal,
        get_config_int,
    )

    checkout_time = _parse_checkout_time(await get_config(db, CONFIG_CHECKOUT_TIME))
    grace = await get_config_int(db, CONFIG_GRACE_MINUTES, 60)
    mode = (
        (
            await get_config(db, CONFIG_OVERTIME_MODE, OVERTIME_MODE_SLAB)
            or OVERTIME_MODE_SLAB
        )
        .strip()
        .upper()
    )

    return compute_overtime(
        check_out_date=hr.check_out_date,
        actual_check_out_at=actual_check_out_at or hr.actual_check_out_at,
        nightly_rate=_nightly_rate_of(hr),
        rooms_count=hr.rooms_count or 1,
        checkout_time=checkout_time,
        grace_minutes=grace,
        mode=mode,
        hourly_percent=await get_config_decimal(
            db, CONFIG_HOURLY_PERCENT, Decimal("10")
        ),
        halfday_percent=await get_config_decimal(
            db, CONFIG_HALFDAY_PERCENT, Decimal("50")
        ),
        fullday_percent=await get_config_decimal(
            db, CONFIG_FULLDAY_PERCENT, Decimal("100")
        ),
        halfday_until_hours=await get_config_int(db, CONFIG_HALFDAY_UNTIL_HOURS, 6),
    )


def actual_nights_of(
    hr: HotelReservation,
    check_out_at: Optional[datetime] = None,
) -> Optional[int]:
    """Nights actually stayed — whole days between the recorded check-in and
    check-out stamps.

    Returns ``None`` while either stamp is missing so callers can fall back to
    the booked snapshot; a same-day (day-use) stay bills as one night.
    """
    check_in = hr.actual_check_in_at
    check_out = check_out_at or hr.actual_check_out_at
    if not check_in or not check_out:
        return None
    # Count whole days in India-local dates. The stamps are stored UTC, so a
    # stay straddling midnight IST would otherwise mis-count a night.
    days = (_to_ist(check_out).date() - _to_ist(check_in).date()).days
    return max(1, days)


def _recompute_room_charge(
    hr: HotelReservation, actual_nights: int
) -> tuple[Decimal, Decimal, list[BillLine]]:
    """Real-world room charge for the stay actually served.

    Nightly tariff x actually-stayed nights x rooms, plus the per-night person
    surcharges (extra adult / child) scaled to the actual stay and the
    one-time extra-bed charge — the same arithmetic the quote used, but driven
    by the recorded check-in / check-out stamps instead of the booked window.
    Returns ``(room_charge, room_tariff, lines)`` where the lines itemise the
    charge exactly as the frozen-snapshot path does.
    """
    nightly = _nightly_rate_of(hr)
    rooms = max(1, int(hr.rooms_count or 1))
    occ = _occupancy_of(hr) or {}
    extra_adults = int(occ.get("extra_adults", 0) or 0)
    extra_children = int(occ.get("extra_children", 0) or 0)
    extra_beds = int(occ.get("extra_beds", 0) or 0)
    adult_rate = money(Decimal(str(occ.get("extra_adult_charge", 0) or 0)))
    child_rate = money(Decimal(str(occ.get("extra_child_charge", 0) or 0)))
    bed_rate = money(Decimal(str(occ.get("extra_bed_charge", 0) or 0)))

    nights_txt = f"{actual_nights} night(s) x {rooms} room(s)"
    tariff = money(nightly * Decimal(actual_nights) * Decimal(rooms))
    lines = [BillLine("Room Tariff", tariff, detail=nights_txt)]
    if extra_adults > 0 and adult_rate > ZERO:
        amt = money(adult_rate * extra_adults * Decimal(actual_nights))
        lines.append(
            BillLine(
                f"Extra Adult x {extra_adults}",
                amt,
                detail=f"{fmt_inr(adult_rate)}/adult/night x {actual_nights} night(s)",
            )
        )
    if extra_children > 0 and child_rate > ZERO:
        amt = money(child_rate * extra_children * Decimal(actual_nights))
        lines.append(
            BillLine(
                f"Extra Child x {extra_children}",
                amt,
                detail=f"{fmt_inr(child_rate)}/child/night x {actual_nights} night(s)",
            )
        )
    if extra_beds > 0 and bed_rate > ZERO:
        amt = money(bed_rate * extra_beds)
        lines.append(
            BillLine(
                f"Extra Bed x {extra_beds}",
                amt,
                detail=f"{fmt_inr(bed_rate)}/bed (one-time)",
            )
        )
    return money(tariff + sum((ln.amount for ln in lines[1:]), ZERO)), tariff, lines


def _nightly_rate_of(hr: HotelReservation) -> Decimal:
    """Per-room, per-night tariff.

    Prefer the rate snapshot frozen at confirmation; fall back to dividing the
    base amount by room-nights so an older row without a snapshot still bills.
    """
    snap: dict = hr.rate_snapshot or {}
    for key in ("nightly_rate", "average_nightly_rate", "rate_per_night"):
        if snap.get(key) is not None:
            try:
                return money(Decimal(str(snap[key])))
            except (ArithmeticError, ValueError, TypeError):
                pass

    room_nights = hr.room_nights or ((hr.nights or 1) * (hr.rooms_count or 1))
    if room_nights and hr.base_amount:
        return money(Decimal(str(hr.base_amount)) / Decimal(str(room_nights)))
    return ZERO


# ════════════════════════════════════════════════════════════════
# ADVANCES
# ════════════════════════════════════════════════════════════════


async def list_advances(
    db: AsyncSession, reservation_id: int
) -> list[HotelAdvancePayment]:
    return list(
        (
            await db.execute(
                select(HotelAdvancePayment)
                .where(
                    HotelAdvancePayment.hotel_reservation_id == reservation_id,
                    HotelAdvancePayment.status == "ACTIVE",
                )
                .order_by(HotelAdvancePayment.collected_at.asc())
            )
        )
        .scalars()
        .all()
    )


def total_advance(advances: Sequence[HotelAdvancePayment]) -> Decimal:
    """Net of refunds — a partly refunded advance only offsets what it still holds."""
    return money(
        sum(
            (
                Decimal(str(a.amount)) - Decimal(str(a.refunded_amount or 0))
                for a in advances
            ),
            ZERO,
        )
    )


async def next_receipt_number(db: AsyncSession) -> str:
    """Sequential, human-quotable receipt id: WT-HADV-<year><6-digit seq>."""
    year = datetime.now(timezone.utc).year
    prefix = f"WT-HADV-{year}"
    last = (
        await db.execute(
            text(
                "SELECT receipt_number FROM hotel_advance_payments "
                "WHERE receipt_number LIKE :p ORDER BY id DESC LIMIT 1"
            ),
            {"p": f"{prefix}%"},
        )
    ).scalar_one_or_none()

    seq = 1
    if last:
        try:
            seq = int(str(last)[len(prefix) :]) + 1
        except ValueError:
            seq = 1
    return f"{prefix}{seq:06d}"


async def next_invoice_number(db: AsyncSession) -> str:
    """Sequential tax-invoice id: WT-HINV-<year><6-digit seq>."""
    year = datetime.now(timezone.utc).year
    prefix = f"WT-HINV-{year}"
    last = (
        await db.execute(
            text(
                "SELECT invoice_number FROM hotel_reservations "
                "WHERE invoice_number LIKE :p ORDER BY id DESC LIMIT 1"
            ),
            {"p": f"{prefix}%"},
        )
    ).scalar_one_or_none()

    seq = 1
    if last:
        try:
            seq = int(str(last)[len(prefix) :]) + 1
        except ValueError:
            seq = 1
    return f"{prefix}{seq:06d}"


# ════════════════════════════════════════════════════════════════
# FULL BILL
# ════════════════════════════════════════════════════════════════


@dataclass
class BillLine:
    label: str
    amount: Decimal
    kind: str = "CHARGE"  # CHARGE | DISCOUNT | TAX | PAYMENT
    detail: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "amount": float(self.amount),
            "kind": self.kind,
            "detail": self.detail,
        }


def _occupancy_of(hr: HotelReservation) -> Optional[dict]:
    """The occupancy surcharge breakdown frozen onto the reservation at booking.

    Lives under rate_snapshot["occupancy"] (customer_care_api._compute_hotel_quote).
    Older reservations booked before occupancy pricing have no block — callers
    treat ``None`` as "nothing to itemize, the room charge is just the tariff".
    """
    snap: dict = hr.rate_snapshot or {}
    occ = snap.get("occupancy")
    return occ if isinstance(occ, dict) else None


def _room_charge_lines(
    room_charge: Decimal, occ: Optional[dict], nights: int, rooms: int
) -> tuple[Decimal, list[BillLine]]:
    """Split a lumped room charge into tariff + per-occupant surcharges.

    ``room_charge`` (= hr.base_amount) already includes the occupancy surcharge,
    so the base tariff is the remainder once the frozen surcharge is peeled off.
    Returns ``(room_tariff, lines)``; with no surcharge it's a single tariff line,
    matching the old behaviour exactly.
    """
    nights_txt = f"{nights} night(s) x {rooms} room(s)" if nights else None
    surcharge = money(Decimal(str((occ or {}).get("surcharge", 0) or 0)))
    room_tariff = money(room_charge - surcharge)

    lines = [BillLine("Room Tariff", room_tariff, detail=nights_txt)]
    if not occ or surcharge <= ZERO:
        # No occupancy surcharge — present it as one plain "Room Charge" line,
        # exactly as before so unchanged bookings read identically.
        lines[0] = BillLine("Room Charge", room_charge, detail=nights_txt)
        return room_tariff, lines

    extra_adults = int(occ.get("extra_adults", 0) or 0)
    extra_children = int(occ.get("extra_children", 0) or 0)
    extra_beds = int(occ.get("extra_beds", 0) or 0)
    adult_rate = money(Decimal(str(occ.get("extra_adult_charge", 0) or 0)))
    child_rate = money(Decimal(str(occ.get("extra_child_charge", 0) or 0)))
    bed_rate = money(Decimal(str(occ.get("extra_bed_charge", 0) or 0)))

    if extra_adults > 0 and adult_rate > ZERO:
        lines.append(
            BillLine(
                f"Extra Adult x {extra_adults}",
                money(adult_rate * extra_adults * Decimal(nights or 1)),
                detail=f"{fmt_inr(adult_rate)}/adult/night x {nights or 1} night(s)",
            )
        )
    if extra_children > 0 and child_rate > ZERO:
        lines.append(
            BillLine(
                f"Extra Child x {extra_children}",
                money(child_rate * extra_children * Decimal(nights or 1)),
                detail=f"{fmt_inr(child_rate)}/child/night x {nights or 1} night(s)",
            )
        )
    if extra_beds > 0 and bed_rate > ZERO:
        lines.append(
            BillLine(
                f"Extra Bed x {extra_beds}",
                money(bed_rate * extra_beds),
                detail=f"{fmt_inr(bed_rate)}/bed (one-time)",
            )
        )
    return room_tariff, lines


def fmt_inr(amount: Decimal) -> str:
    """Compact rupee label for line-item details (no decimals when whole)."""
    a = money(amount)
    whole = a == a.to_integral_value()
    return f"Rs.{int(a):,}" if whole else f"Rs.{a:,.2f}"


@dataclass
class HotelBill:
    room_charge: Decimal
    overtime: OvertimeResult
    extra_charges: Decimal
    discount: Decimal
    taxable_amount: Decimal
    gst_percent: Decimal
    gst_amount: Decimal
    is_tax_invoice: bool
    gst_enabled: bool
    tax_source: str
    grand_total: Decimal
    advance_paid: Decimal
    balance_due: Decimal
    refund_due: Decimal
    room_tariff: Decimal = ZERO  # room_charge less the occupancy surcharge
    occupancy: Optional[dict] = None  # frozen rate_snapshot["occupancy"], if any
    lines: list[BillLine] = field(default_factory=list)
    advances: list[dict] = field(default_factory=list)
    actual_nights: int = 1  # nights actually stayed (check-in → check-out)

    def as_dict(self) -> dict:
        return {
            "room_charge": float(self.room_charge),
            "room_tariff": float(self.room_tariff),
            "occupancy": self.occupancy,
            "overtime": self.overtime.as_dict(),
            "extra_charges": float(self.extra_charges),
            "discount": float(self.discount),
            "taxable_amount": float(self.taxable_amount),
            "gst_percent": float(self.gst_percent),
            "gst_amount": float(self.gst_amount),
            "is_tax_invoice": self.is_tax_invoice,
            "gst_enabled": self.gst_enabled,
            "tax_source": self.tax_source,
            "grand_total": float(self.grand_total),
            "advance_paid": float(self.advance_paid),
            "balance_due": float(self.balance_due),
            "refund_due": float(self.refund_due),
            "lines": [ln.as_dict() for ln in self.lines],
            "advances": self.advances,
            "actual_nights": self.actual_nights,
        }


async def build_bill(
    db: AsyncSession,
    hr: HotelReservation,
    *,
    projected_check_out_at: Optional[datetime] = None,
    projected_extra_charges: Decimal = ZERO,
    nights_override: Optional[int] = None,
) -> HotelBill:
    """Assemble the complete bill for a reservation.

    Used both to preview a check-out (pass the intended check-out time and any
    charges the admin is about to add) and to render an already-closed stay.

    GST rides on room + overtime + extras less discount, matching how the
    original booking was taxed, so a late check-out is taxed at the same slab
    the stay was.

    `nights_override` — used by the split-stay flow (migration 0042_hotel_switch)
    to render a bill against a *truncated* stay without mutating the
    reservation's `check_out_date`. Only the line-item display picks this up;
    the math uses `hr.base_amount` / `hr.extra_charges` etc. which the
    reservation carries as a snapshot, so a partial-stay truncated bill is
    produced by recomputing those snapshot fields (in services/switch.py)
    before calling this function with the override.
    """
    from app.modules.hotel.services import platform_gst_enabled

    # ── Real-world stay ────────────────────────────────────────────
    # Once both check-in and check-out stamps are recorded, the room charge
    # follows the nights actually stayed (nightly rate x actual nights x
    # rooms + per-night person surcharges), not the booked snapshot — so the
    # bill, collect-payment and invoice amounts always match the real stay.
    actual_nights = actual_nights_of(hr, check_out_at=projected_check_out_at)
    recomputed_room_lines: Optional[list[BillLine]] = None
    if actual_nights is not None:
        room_charge, _recomputed_tariff, recomputed_room_lines = _recompute_room_charge(
            hr, actual_nights
        )
    else:
        room_charge = money(Decimal(str(hr.base_amount or 0)))

    overtime = await compute_overtime_for_reservation(
        db, hr, actual_check_out_at=projected_check_out_at
    )
    extras = money(
        Decimal(str(hr.extra_charges or 0)) + money(projected_extra_charges or ZERO)
    )
    discount = money(
        Decimal(str(hr.discount_amount or 0))
        + Decimal(str(getattr(hr, "coupon_discount", 0) or 0))
    )

    pre_tax = money(room_charge + overtime.charge + extras - discount)
    if pre_tax < ZERO:
        pre_tax = ZERO

    # ── Tax, delegated to the single pricing implementation ──────────
    gst_on = await platform_gst_enabled(db)
    hotel = (
        await db.execute(select(Hotel).where(Hotel.id == hr.hotel_id))
    ).scalar_one_or_none()
    tax_mode = getattr(hotel, "tax_mode", "EXCLUSIVE") or "EXCLUSIVE"

    slab_rows = (
        (
            await db.execute(
                select(HotelGstSlab).where(HotelGstSlab.is_active == True)  # noqa: E712
            )
        )
        .scalars()
        .all()
    )
    slabs = [
        GstSlabInput(
            id=s.id,
            slab_name=getattr(s, "slab_name", None),
            tariff_from=Decimal(str(s.tariff_from or 0)),
            tariff_to=(Decimal(str(s.tariff_to)) if s.tariff_to is not None else None),
            gst_percent=Decimal(str(s.gst_percent or 0)),
            hsn_code=getattr(s, "hsn_code", None),
        )
        for s in slab_rows
    ]

    tax = compute_tax(
        amount=pre_tax,
        nightly_tariff=_nightly_rate_of(hr),
        tax_mode=tax_mode,
        slabs=slabs,
        platform_gst_enabled=gst_on,
    )

    grand_total = money(tax.total_amount)

    advances = await list_advances(db, hr.id)
    advance_paid = total_advance(advances)
    outstanding = money(grand_total - advance_paid)

    balance_due = outstanding if outstanding > ZERO else ZERO
    refund_due = money(-outstanding) if outstanding < ZERO else ZERO

    # ── Human-readable statement ─────────────────────────────────────
    # The room charge already folds in the occupancy surcharge, so surface it
    # as tariff + extra adult / child / bed rows for the admin (and invoice).
    occ = _occupancy_of(hr)
    if recomputed_room_lines is not None:
        room_tariff = _recomputed_tariff
        room_lines = recomputed_room_lines
    else:
        display_nights = (
            nights_override if nights_override is not None else (hr.nights or 1)
        )
        room_tariff, room_lines = _room_charge_lines(
            room_charge, occ, display_nights, hr.rooms_count or 1
        )
    lines: list[BillLine] = list(room_lines)
    if overtime.charge > ZERO:
        lines.append(
            BillLine("Late Check-Out", overtime.charge, detail=overtime.reason)
        )
    if extras > ZERO:
        lines.append(
            BillLine("Extra Charges", extras, detail="Food, laundry, damage, etc.")
        )
    if discount > ZERO:
        lines.append(
            BillLine(
                "Discount",
                discount,
                kind="DISCOUNT",
                detail=getattr(hr, "coupon_code", None),
            )
        )
    if tax.gst_amount > ZERO:
        lines.append(
            BillLine(
                f"GST @ {tax.gst_percent}%",
                tax.gst_amount,
                kind="TAX",
                detail=(
                    "Included in the tariff"
                    if tax_mode == "INCLUSIVE"
                    else "Added on the taxable amount"
                ),
            )
        )
    for a in advances:
        lines.append(
            BillLine(
                f"Advance ({a.payment_mode})",
                money(Decimal(str(a.amount)) - Decimal(str(a.refunded_amount or 0))),
                kind="PAYMENT",
                detail=f"{a.receipt_number} - received by {a.received_by}",
            )
        )

    return HotelBill(
        room_charge=room_charge,
        room_tariff=room_tariff,
        occupancy=occ,
        overtime=overtime,
        extra_charges=extras,
        discount=discount,
        taxable_amount=money(tax.taxable_amount),
        gst_percent=tax.gst_percent,
        gst_amount=money(tax.gst_amount),
        is_tax_invoice=tax.is_tax_invoice,
        gst_enabled=gst_on,
        tax_source=tax.source,
        grand_total=grand_total,
        advance_paid=advance_paid,
        balance_due=balance_due,
        refund_due=refund_due,
        lines=lines,
        actual_nights=actual_nights or 1,
        advances=[
            {
                "id": a.id,
                "receipt_number": a.receipt_number,
                "amount": float(a.amount),
                "refunded_amount": float(a.refunded_amount or 0),
                "payment_mode": a.payment_mode,
                "received_by": a.received_by,
                "reference_number": a.reference_number,
                "notes": a.notes,
                "collected_at": a.collected_at.isoformat() if a.collected_at else None,
            }
            for a in advances
        ],
    )
