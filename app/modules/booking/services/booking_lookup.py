# ============================================================
# WAYTERO — BOOKING LOOKUP BY REFERENCE
# File: app/modules/booking/services/booking_lookup.py
# Doc Ref: Website Lead Capture §2; migration 0083_guest_booking_feedback
#
# One place that answers "does this booking reference belong to this
# person?". Two public surfaces need it:
#
#   GET /public/bookings/track      — the /track page
#   GET /public/bookings/feedback-lookup + POST /public/feedback
#
# Keeping it in one function matters because the answer must be the same on
# both, and because a booking can be quoted by any of five numbers: the cab
# booking number, the hotel reservation number, the tour booking number, the
# master trip number, and the older MT-/CB-/TO- formats still in the data.
#
# A mobile mismatch is reported as NOT FOUND, not as "wrong mobile" — same
# shape as an unknown number, so the endpoint cannot be used to probe whether
# a booking exists. Callers raise ResourceNotFoundException on None.
# ============================================================

from typing import Any, Dict, Optional

from sqlalchemy import text as _text
from sqlalchemy.ext.asyncio import AsyncSession

# The guest's own account name is on customers; every join below lands on
# users for the registered mobile, which is the thing being verified.
_NAME_EXPR = (
    "COALESCE(NULLIF(TRIM(COALESCE(c.first_name,'') || ' ' "
    "|| COALESCE(c.last_name,'')), ''), 'Guest') AS customer_name"
)


def _match(
    number_col: str, master_col: str, bn: Optional[str], master_id: Optional[int]
):
    """WHERE clause + bind for a lookup by reference or by master booking.

    A trip number resolves through the same child query as its own service
    number, so the traveller sees the same detail whichever they typed.
    """
    if master_id is not None:
        return f"{master_col} = :lookup_id", {"lookup_id": master_id}
    return (
        f"UPPER({number_col}) = :lookup_id",
        {"lookup_id": (bn or "").strip().upper()},
    )


async def _find_cab(
    db: AsyncSession, bn: Optional[str] = None, master_id: Optional[int] = None
) -> Optional[Dict[str, Any]]:
    where, params = _match("cb.booking_number", "cb.master_booking_id", bn, master_id)
    row = (
        (
            await db.execute(
                _text(
                    f"""
                SELECT cb.id, cb.master_booking_id, cb.booking_number,
                       cb.booking_status, cb.trip_type, cb.pickup_location,
                       cb.drop_location, cb.pickup_datetime, cb.final_amount,
                       cb.estimated_amount, mb.booking_number AS master_number,
                       mb.customer_id, mb.journey_start_date,
                       u.mobile_number AS customer_mobile, {_NAME_EXPR},
                       d.full_name  AS driver_name,
                       d.mobile     AS driver_mobile,
                       v.registration_number AS vehicle_number
                FROM cab_bookings cb
                JOIN master_bookings mb ON mb.id = cb.master_booking_id
                JOIN customers c        ON c.id = mb.customer_id
                JOIN users u            ON u.id = c.user_id
                LEFT JOIN LATERAL (
                    SELECT cba.driver_id, cba.vehicle_id
                    FROM cab_booking_assignments cba
                    WHERE cba.cab_booking_id = cb.id AND cba.closed_at IS NULL
                    ORDER BY cba.assigned_at DESC
                    LIMIT 1
                ) act ON true
                LEFT JOIN drivers d     ON d.id = act.driver_id
                LEFT JOIN vehicles v    ON v.id = act.vehicle_id
                WHERE {where}
                LIMIT 1
                """
                ),
                params,
            )
        )
        .mappings()
        .first()
    )
    if not row:
        return None
    return {
        "service_type": "CAB",
        "master_booking_id": row["master_booking_id"],
        "customer_id": row["customer_id"],
        "booking_number": row["booking_number"],
        "master_booking_number": row["master_number"],
        "status": row["booking_status"],
        "customer_name": row["customer_name"],
        "customer_mobile": row["customer_mobile"],
        "amount": row["final_amount"] or row["estimated_amount"],
        "detail": {
            "trip_type": row["trip_type"],
            "pickup_location": row["pickup_location"],
            "drop_location": row["drop_location"],
            "pickup_datetime": row["pickup_datetime"],
            "journey_start_date": row["journey_start_date"],
            "driver_name": row["driver_name"],
            "driver_mobile": row["driver_mobile"],
            "vehicle_number": row["vehicle_number"],
        },
    }


async def _find_hotel(
    db: AsyncSession, bn: Optional[str] = None, master_id: Optional[int] = None
) -> Optional[Dict[str, Any]]:
    # Legacy `hotel_bookings` was dropped in migration 0038; live hotel
    # reservations live in `hotel_reservations`.
    where, params = _match(
        "hr.reservation_number", "hr.master_booking_id", bn, master_id
    )
    row = (
        (
            await db.execute(
                _text(
                    f"""
                SELECT hr.id, hr.master_booking_id,
                       hr.reservation_number AS booking_number,
                       hr.reservation_status AS booking_status,
                       h.hotel_name, h.address AS hotel_address,
                       hr.check_in_date, hr.check_out_date,
                       hr.rooms_count AS num_rooms, hr.adults_count AS num_guests,
                       hr.total_amount AS final_amount,
                       mb.booking_number AS master_number, mb.customer_id,
                       mb.journey_start_date,
                       u.mobile_number AS customer_mobile, {_NAME_EXPR}
                FROM hotel_reservations hr
                JOIN master_bookings mb ON mb.id = hr.master_booking_id
                JOIN hotels h           ON h.id = hr.hotel_id
                JOIN customers c        ON c.id = hr.customer_id
                JOIN users u            ON u.id = c.user_id
                WHERE {where}
                LIMIT 1
                """
                ),
                params,
            )
        )
        .mappings()
        .first()
    )
    if not row:
        return None
    return {
        "service_type": "HOTEL",
        "master_booking_id": row["master_booking_id"],
        "customer_id": row["customer_id"],
        "booking_number": row["booking_number"],
        "master_booking_number": row["master_number"],
        "status": row["booking_status"],
        "customer_name": row["customer_name"],
        "customer_mobile": row["customer_mobile"],
        "amount": row["final_amount"],
        "detail": {
            "hotel_name": row["hotel_name"],
            "hotel_address": row["hotel_address"],
            "check_in_date": row["check_in_date"],
            "check_out_date": row["check_out_date"],
            "num_rooms": row["num_rooms"],
            "num_guests": row["num_guests"],
        },
    }


async def _find_tour(
    db: AsyncSession, bn: Optional[str] = None, master_id: Optional[int] = None
) -> Optional[Dict[str, Any]]:
    # tour_bookings has no customer_id of its own — the traveller is the
    # master booking's customer.
    where, params = _match("tb.booking_number", "tb.master_booking_id", bn, master_id)
    row = (
        (
            await db.execute(
                _text(
                    f"""
                SELECT tb.id, tb.master_booking_id, tb.booking_number,
                       tb.booking_status, tb.travel_start_date,
                       tb.travel_end_date, tb.persons_count, tb.total_amount,
                       tp.package_name, tp.destination,
                       tp.duration_days, tp.duration_nights,
                       mb.booking_number AS master_number, mb.customer_id,
                       mb.journey_start_date,
                       u.mobile_number AS customer_mobile, {_NAME_EXPR}
                FROM tour_bookings tb
                JOIN master_bookings mb  ON mb.id = tb.master_booking_id
                JOIN tour_packages tp    ON tp.id = tb.package_id
                JOIN customers c         ON c.id = mb.customer_id
                JOIN users u             ON u.id = c.user_id
                WHERE {where}
                LIMIT 1
                """
                ),
                params,
            )
        )
        .mappings()
        .first()
    )
    if not row:
        return None
    return {
        "service_type": "TOUR",
        "master_booking_id": row["master_booking_id"],
        "customer_id": row["customer_id"],
        "booking_number": row["booking_number"],
        "master_booking_number": row["master_number"],
        "status": row["booking_status"],
        "customer_name": row["customer_name"],
        "customer_mobile": row["customer_mobile"],
        "amount": row["total_amount"],
        "detail": {
            "package_name": row["package_name"],
            "destination": row["destination"],
            "travel_start_date": row["travel_start_date"],
            "travel_end_date": row["travel_end_date"],
            "persons_count": row["persons_count"],
            "duration_days": row["duration_days"],
            "duration_nights": row["duration_nights"],
        },
    }


async def _find_master(
    db: AsyncSession, bn: Optional[str] = None, master_id: Optional[int] = None
) -> Optional[Dict[str, Any]]:
    """The master trip number (WT-MTB-… / MT-…), which covers a whole trip.

    Feedback is stored per master booking anyway, so a trip number is a valid
    thing for a traveller to be asked for. A trip that holds exactly one
    service is reported as that service, because "Kathmandu → Pokhara Tour"
    is what the traveller recognises; only a genuinely combined trip is
    TRIP, and then the row aggregates its services rather than pretending to
    be one.
    """
    where, params = _match("mb.booking_number", "mb.id", bn, master_id)
    row = (
        (
            await db.execute(
                _text(
                    f"""
                SELECT mb.id AS master_booking_id, mb.booking_number,
                       mb.booking_status, mb.total_amount, mb.customer_id,
                       mb.journey_start_date, mb.journey_end_date,
                       u.mobile_number AS customer_mobile, {_NAME_EXPR},
                       COALESCE((
                           SELECT string_agg(DISTINCT bs.service_type, ',')
                           FROM booking_services bs
                           WHERE bs.master_booking_id = mb.id
                       ), '') AS service_types
                FROM master_bookings mb
                JOIN customers c ON c.id = mb.customer_id
                JOIN users u     ON u.id = c.user_id
                WHERE {where}
                LIMIT 1
                """
                ),
                params,
            )
        )
        .mappings()
        .first()
    )
    if not row:
        return None

    types = [t for t in (row["service_types"] or "").split(",") if t]
    if len(types) == 1:
        finder = {"CAB": _find_cab, "HOTEL": _find_hotel, "TOUR": _find_tour}.get(
            types[0]
        )
        child = await finder(db, master_id=row["master_booking_id"]) if finder else None
        if child:
            # Show the number the traveller typed, not the internal one.
            child["booking_number"] = row["booking_number"]
            return child

    return {
        "service_type": "TRIP",
        "master_booking_id": row["master_booking_id"],
        "customer_id": row["customer_id"],
        "booking_number": row["booking_number"],
        "master_booking_number": row["booking_number"],
        "status": row["booking_status"],
        "customer_name": row["customer_name"],
        "customer_mobile": row["customer_mobile"],
        "amount": row["total_amount"],
        "detail": {
            "service_types": row["service_types"],
            "journey_start_date": row["journey_start_date"],
            "journey_end_date": row["journey_end_date"],
        },
    }


async def resolve_booking_by_ref(
    db: AsyncSession, booking_number: str, mobile: str
) -> Optional[Dict[str, Any]]:
    """Return the booking a reference points at, or None.

    `mobile` must already be normalised (10 digits). None is returned for both
    "no such reference" and "reference exists, different registered mobile",
    so a caller's 404 never distinguishes them.

    Sub-booking numbers are tried first: they are what a customer is actually
    sent, and they say which service to display.
    """
    for finder in (_find_cab, _find_hotel, _find_tour, _find_master):
        found = await finder(db, booking_number)
        if found is None:
            continue
        if (found["customer_mobile"] or "") != mobile:
            return None
        return found
    return None


__all__ = ["resolve_booking_by_ref"]
