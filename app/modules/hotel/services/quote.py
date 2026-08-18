# ============================================================
# WAY TERO — HOTEL QUOTE SERVICE
# File: app/modules/hotel/services/quote.py
# Doc Ref: BRD Part 4 §57-92, SRS Part 5 §153-194
# Migration: 0031_hotel_module (rates), 0035_hotel_payments (commission)
#
# Extracted from app/modules/admin/customer_care_api.py so the hotel switch /
# split-stay service (migration 0042_hotel_switch) can re-quote a new stay
# without copying the rate/tax/commission math.
#
# Both admin (customer-care booking) and admin (switch/split) endpoints must
# price a stay through exactly one path; otherwise a switch preview can
# silently disagree with a normal booking.
# ============================================================

from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.hotel.models import Hotel
from app.modules.hotel.services import (
    platform_gst_enabled,
    resolve_hotel_commission,
)
from app.modules.hotel.services.pricing import (
    GstSlabInput,
    RatePlanInput,
    commission_snapshot,
    compute_commission,
    compute_tax,
    money,
    rate_snapshot,
    resolve_stay_rates,
)


_HZERO = Decimal("0.00")


async def compute_hotel_quote(
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
    """Price a stay for `rooms_count` identical rooms. Returns Decimal money
    fields plus the rate/commission snapshots to freeze onto the reservation.

    Occupancy beyond the category's `base_occupancy` adds room-side surcharges:
    extra adults and extra children are billed per night, extra beds once for
    the stay. The surcharge is taxed at the room's GST slab and folded into the
    taxable amount so commission covers it too.

    Moved here from app.modules.admin.customer_care_api on migration 0042 so
    the switch / split-stay service can quote a target stay without copying
    the math. Behaviour is byte-identical — the wrapper is a thin one-liner in
    the original file now.
    """
    nights = (check_out - check_in).days
    if nights < 1:
        raise HTTPException(status_code=400, detail="Check-out must be after check-in")
    rooms = max(int(rooms_count), 1)
    adults = max(int(adults_count), 1)
    children = max(int(children_count), 0)
    beds_requested = max(int(extra_beds), 0)

    cat = (
        (
            await db.execute(
                text(
                    "SELECT id, hotel_id, category_name, base_price, total_rooms, "
                    "       base_occupancy, max_adults, max_children, max_occupancy, "
                    "       extra_bed_allowed, extra_adult_charge, extra_child_charge, "
                    "       extra_bed_charge "
                    "FROM hotel_room_categories "
                    "WHERE id = :id AND is_active = TRUE"
                ),
                {"id": room_category_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if not cat or int(cat["hotel_id"]) != int(hotel.id):
        raise HTTPException(
            status_code=404, detail="Room category not found for this hotel"
        )

    base_price = Decimal(str(cat["base_price"] or 0))

    # Rate plans overlapping the stay window (half-open [check_in, check_out)).
    plan_rows = (
        (
            await db.execute(
                text(
                    "SELECT id, plan_name, plan_type, priority, date_from, date_to, "
                    "       day_of_week_mask, rate_mode, rate_value, min_nights, is_active "
                    "FROM hotel_room_rate_plans "
                    "WHERE room_category_id = :cid AND is_active = TRUE "
                    "  AND date_from <= :dto AND date_to >= :dfrom"
                ),
                {"cid": room_category_id, "dfrom": check_in, "dto": check_out},
            )
        )
        .mappings()
        .all()
    )
    plans = [
        RatePlanInput(
            id=int(p["id"]),
            plan_name=str(p["plan_name"]),
            plan_type=str(p["plan_type"]),
            priority=int(p["priority"] or 0),
            date_from=p["date_from"],
            date_to=p["date_to"],
            day_of_week_mask=p["day_of_week_mask"],
            rate_mode=str(p["rate_mode"]),
            rate_value=Decimal(str(p["rate_value"] or 0)),
            min_nights=int(p["min_nights"] or 1),
            is_active=bool(p["is_active"]),
        )
        for p in plan_rows
    ]

    # Per-date inventory rate overrides beat every rate plan.
    ovr_rows = (
        (
            await db.execute(
                text(
                    "SELECT inventory_date, rate_override FROM hotel_inventory "
                    "WHERE room_category_id = :cid "
                    "  AND inventory_date >= :dfrom AND inventory_date < :dto "
                    "  AND rate_override IS NOT NULL"
                ),
                {"cid": room_category_id, "dfrom": check_in, "dto": check_out},
            )
        )
        .mappings()
        .all()
    )
    overrides = {
        r["inventory_date"]: Decimal(str(r["rate_override"])) for r in ovr_rows
    }

    quote = resolve_stay_rates(
        check_in=check_in,
        check_out=check_out,
        base_price=base_price,
        rate_plans=plans,
        inventory_overrides=overrides,
    )

    # GST slabs in force on the check-in date.
    gst_on = await platform_gst_enabled(db)
    slab_rows = (
        (
            await db.execute(
                text(
                    "SELECT id, slab_name, tariff_from, tariff_to, gst_percent, hsn_code "
                    "FROM hotel_gst_slabs "
                    "WHERE is_active = TRUE AND effective_from <= :d "
                    "  AND (effective_to IS NULL OR effective_to >= :d) "
                    "ORDER BY tariff_from"
                ),
                {"d": check_in},
            )
        )
        .mappings()
        .all()
    )
    slabs = [
        GstSlabInput(
            id=int(s["id"]),
            slab_name=str(s["slab_name"]),
            tariff_from=Decimal(str(s["tariff_from"])),
            tariff_to=(
                Decimal(str(s["tariff_to"])) if s["tariff_to"] is not None else None
            ),
            gst_percent=Decimal(str(s["gst_percent"])),
            hsn_code=s["hsn_code"],
        )
        for s in slab_rows
    ]

    # Tax is resolved per night (a festival rate can cross into a higher slab),
    # for a single room, then scaled by room count.
    taxable_1 = _HZERO
    gst_1 = _HZERO
    total_1 = _HZERO
    is_tax_invoice = False
    nightly: list[dict] = []
    for night in quote.nights:
        tax = compute_tax(
            amount=night.rate,
            nightly_tariff=night.rate,
            tax_mode=str(hotel.tax_mode),
            slabs=slabs,
            platform_gst_enabled=gst_on,
        )
        taxable_1 = money(taxable_1 + tax.taxable_amount)
        gst_1 = money(gst_1 + tax.gst_amount)
        total_1 = money(total_1 + tax.total_amount)
        is_tax_invoice = is_tax_invoice or tax.is_tax_invoice
        nightly.append(
            {
                "date": night.stay_date.isoformat(),
                "rate": float(night.rate),
                "source": night.source,
                "plan_name": night.plan_name,
                "gst_percent": float(tax.gst_percent),
                "gst_amount": float(tax.gst_amount),
                "total_with_tax": float(tax.total_amount),
            }
        )

    base_amount = money(quote.total * rooms)
    taxable_amount = money(taxable_1 * rooms)
    gst_amount = money(gst_1 * rooms)
    total_amount = money(total_1 * rooms)

    # ── Occupancy surcharge ──────────────────────────────────────────
    # Base price covers `base_occupancy` guests per room. Adults beyond that
    # are billed per night; children fill whatever base slots the adults leave,
    # then are billed per night; extra beds are an explicit admin add-on billed
    # once for the stay. Everything here is room-side revenue, so it is taxed at
    # the room's GST slab and folded into the taxable base (commission follows).
    base_occupancy = int(cat["base_occupancy"] or 1)
    extra_adult_charge = Decimal(str(cat["extra_adult_charge"] or 0))
    extra_child_charge = Decimal(str(cat["extra_child_charge"] or 0))
    extra_bed_charge = Decimal(str(cat["extra_bed_charge"] or 0))
    extra_bed_allowed = bool(cat["extra_bed_allowed"])

    # Occupancy caps (per room × rooms). Overflow is a hard 400 rather than a
    # silent over-book — there was no such guard at creation before.
    max_adults_total = int(cat["max_adults"] or 0) * rooms
    max_children_total = int(cat["max_children"] or 0) * rooms
    max_occ_total = int(cat["max_occupancy"] or 0) * rooms
    if max_adults_total and adults > max_adults_total:
        raise HTTPException(
            status_code=400,
            detail=f"This room allows at most {max_adults_total} adult(s) for {rooms} room(s).",
        )
    if max_children_total and children > max_children_total:
        raise HTTPException(
            status_code=400,
            detail=f"This room allows at most {max_children_total} child(ren) for {rooms} room(s).",
        )
    if max_occ_total and (adults + children) > max_occ_total:
        raise HTTPException(
            status_code=400,
            detail=f"This room allows at most {max_occ_total} guest(s) for {rooms} room(s).",
        )

    beds = beds_requested if extra_bed_allowed else 0
    base_capacity = base_occupancy * rooms
    extra_adults = max(0, adults - base_capacity)
    children_capacity_left = max(0, base_capacity - adults)
    extra_children = max(0, children - children_capacity_left)

    person_surcharge = money(
        (extra_adults * extra_adult_charge + extra_children * extra_child_charge)
        * Decimal(nights)
    )
    bed_surcharge = money(beds * extra_bed_charge)
    surcharge = money(person_surcharge + bed_surcharge)

    occupancy_block = {
        "base_occupancy": base_occupancy,
        "base_capacity": base_capacity,
        "adults": adults,
        "children": children,
        "extra_adults": extra_adults,
        "extra_children": extra_children,
        "extra_beds": beds,
        "extra_adult_charge": float(extra_adult_charge),
        "extra_child_charge": float(extra_child_charge),
        "extra_bed_charge": float(extra_bed_charge),
        "person_surcharge": float(person_surcharge),
        "bed_surcharge": float(bed_surcharge),
        "surcharge": float(surcharge),
        "person_charge_basis": "PER_NIGHT",
        "bed_charge_basis": "PER_STAY",
    }

    if surcharge > 0:
        # Tax the surcharge at the room's slab (nightly_tariff drives the slab,
        # the surcharge is the taxable amount) so INCLUSIVE/EXCLUSIVE is handled
        # exactly like the room revenue.
        s_tax = compute_tax(
            amount=surcharge,
            nightly_tariff=money(quote.average_nightly_rate),
            tax_mode=str(hotel.tax_mode),
            slabs=slabs,
            platform_gst_enabled=gst_on,
        )
        base_amount = money(base_amount + surcharge)
        taxable_amount = money(taxable_amount + s_tax.taxable_amount)
        gst_amount = money(gst_amount + s_tax.gst_amount)
        total_amount = money(total_amount + s_tax.total_amount)
        is_tax_invoice = is_tax_invoice or s_tax.is_tax_invoice
        occupancy_block["taxable_amount"] = float(money(s_tax.taxable_amount))
        occupancy_block["gst_amount"] = float(money(s_tax.gst_amount))
        occupancy_block["total_amount"] = float(money(s_tax.total_amount))

    gst_percent = (
        money(gst_amount / taxable_amount * Decimal("100"))
        if taxable_amount > 0
        else _HZERO
    )

    # Commission on the pre-tax (taxable) amount.
    config = await resolve_hotel_commission(db, hotel, check_in)
    room_nights = nights * rooms
    comm = compute_commission(taxable_amount, config, room_nights=room_nights)

    snapshot = rate_snapshot(quote)
    snapshot["occupancy"] = occupancy_block

    return {
        "hotel_id": int(hotel.id),
        "hotel_name": str(hotel.hotel_name),
        "room_category_id": room_category_id,
        "room_category_name": str(cat["category_name"]),
        "check_in_date": check_in,
        "check_out_date": check_out,
        "nights": nights,
        "rooms_count": rooms,
        "room_nights": room_nights,
        "base_amount": base_amount,
        "taxable_amount": taxable_amount,
        "gst_percent": gst_percent,
        "gst_amount": gst_amount,
        "is_tax_invoice": is_tax_invoice,
        "total_amount": total_amount,
        "platform_commission": comm.commission_amount,
        "partner_payout": comm.partner_payout,
        "average_nightly_rate": money(quote.average_nightly_rate),
        "nightly": nightly,
        "occupancy": occupancy_block,
        "rate_snapshot": snapshot,
        "commission_snapshot": commission_snapshot(comm),
    }


def quote_to_response(q: dict) -> dict:
    """Money → float for the JSON wire; keep the human-readable per-night list.

    Companion to compute_hotel_quote — the admin router strips Decimals to
    JSON-safe floats before returning. Single shared converter so the
    response shape doesn't drift between customer-care and switch endpoints.
    """
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
        "occupancy": q["occupancy"],
    }


__all__ = ["compute_hotel_quote", "quote_to_response"]
