"""Cab coupon capacity. Doc Ref: BRD Part 6 §147; partner coupon participation."""

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationException


async def available_cab_counts(
    db: AsyncSession,
    partner_ids: list[int],
    city_id: int,
    vehicle_category_id: int,
    pickup_datetime: datetime,
    return_datetime: datetime | None = None,
    *,
    exclude_cab_id: int | None = None,
) -> dict[int, int]:
    if not partner_ids:
        return {}
    if not city_id or not vehicle_category_id or pickup_datetime.tzinfo is None:
        raise ValidationException(
            "City, category and a timezone-aware pickup are required"
        )
    if return_datetime is not None and (
        return_datetime.tzinfo is None or return_datetime <= pickup_datetime
    ):
        raise ValidationException("Return time must be after pickup with a timezone")
    # Unknown trip ends remain reserved until completion rather than inventing a free slot.
    rows = (
        (
            await db.execute(
                text(
                    """
        WITH fleet AS (
            SELECT v.id, v.partner_id FROM vehicles v
            JOIN partners p ON p.id = v.partner_id
            WHERE p.id = ANY(CAST(:pids AS BIGINT[]))
              AND p.status IN ('APPROVED','ACTIVE')
              AND v.city_id = :city AND v.vehicle_category_id = :category
              AND v.status IN ('APPROVED','ACTIVE') AND v.deleted_at IS NULL
              AND EXISTS (SELECT 1 FROM partner_services ps WHERE ps.partner_id = p.id
                          AND ps.service_type = 'CAB' AND ps.is_active = TRUE)
        ), commitments AS (
            SELECT cb.id, a.partner_id, a.vehicle_id
            FROM cab_bookings cb
            JOIN cab_booking_assignments a ON a.cab_booking_id = cb.id
                AND a.closed_at IS NULL AND a.rejected_at IS NULL
            WHERE cb.booking_status IN ('PENDING_PARTNER_ACCEPTANCE','ASSIGNED',
                                       'DRIVER_ASSIGNED','STARTED','BREAKDOWN_REPORTED')
              AND cb.vehicle_category_id = :category
              AND (CAST(:exclude_id AS BIGINT) IS NULL OR cb.id <> :exclude_id)
              AND cb.pickup_datetime < COALESCE(CAST(:ends AS TIMESTAMPTZ), 'infinity'::TIMESTAMPTZ)
              AND COALESCE(cb.return_datetime, 'infinity'::TIMESTAMPTZ) > :starts
        ), held_pool AS (
            SELECT cb.id, CAST(member.value AS BIGINT) AS partner_id
            FROM coupon_usages cu JOIN cab_bookings cb ON cb.id = cu.cab_booking_id
            CROSS JOIN LATERAL jsonb_array_elements_text(cu.eligible_partner_ids) member
            WHERE cb.booking_status = 'PENDING_ASSIGNMENT'
              AND cu.coupon_type IN ('PARTNER_ONLY','PLATFORM_PARTNER')
              AND cb.vehicle_category_id = :category
              AND (CAST(:exclude_id AS BIGINT) IS NULL OR cb.id <> :exclude_id)
              AND cb.pickup_datetime < COALESCE(CAST(:ends AS TIMESTAMPTZ), 'infinity'::TIMESTAMPTZ)
              AND COALESCE(cb.return_datetime, 'infinity'::TIMESTAMPTZ) > :starts
              AND NOT EXISTS (SELECT 1 FROM cab_booking_assignments r
                  WHERE r.cab_booking_id = cb.id AND r.partner_id = CAST(member.value AS BIGINT)
                    AND r.rejected_at IS NOT NULL AND r.rejection_reason_code <> 'ADMIN_REASSIGN')
        )
        SELECT f.partner_id, GREATEST(0,
            COUNT(*) FILTER (WHERE NOT EXISTS (SELECT 1 FROM commitments c WHERE c.vehicle_id = f.id))
            - (SELECT COUNT(DISTINCT c.id) FROM commitments c
               WHERE c.partner_id = f.partner_id AND c.vehicle_id IS NULL)
            - (SELECT COUNT(DISTINCT h.id) FROM held_pool h WHERE h.partner_id = f.partner_id)
        ) AS available
        FROM fleet f GROUP BY f.partner_id
    """
                ),
                {
                    "pids": sorted(set(partner_ids)),
                    "city": city_id,
                    "category": vehicle_category_id,
                    "starts": pickup_datetime,
                    "ends": return_datetime,
                    "exclude_id": exclude_cab_id,
                },
            )
        )
        .mappings()
        .all()
    )
    counts = {pid: 0 for pid in partner_ids}
    counts.update({row["partner_id"]: int(row["available"]) for row in rows})
    return counts
