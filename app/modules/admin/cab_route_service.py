# ============================================================
# WAYTERO — CAB ROUTE SERVICE
# File: app/modules/admin/cab_route_service.py
# Doc Ref: migration 0079_cab_routes; BRD Part 3 §35-§36 (fare engine)
#
# Shared by the admin editor and the public route pages so a route's quote on
# the website is produced by exactly the same call the booking flow makes.
# Prices are never stored — only the distance and the vehicles the admin picked.
# ============================================================

import re
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationException
from app.modules.admin.cab_route_models import CabRoute, CabRouteVehicleCategory

TRIP_TYPE = "OUTSTATION"


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:150]


async def unique_slug(db: AsyncSession, base: str, exclude_id: int | None) -> str:
    slug = slugify(base) or "route"
    candidate = slug
    n = 2
    while True:
        q = select(CabRoute.id).where(CabRoute.slug == candidate)
        if exclude_id is not None:
            q = q.where(CabRoute.id != exclude_id)
        if not (await db.execute(q.limit(1))).first():
            return candidate
        candidate = f"{slug}-{n}"
        n += 1


async def assert_city_active(db: AsyncSession, city_id: int, label: str) -> None:
    if not (
        await db.execute(
            text("SELECT 1 FROM cities WHERE id = :c AND COALESCE(is_active, TRUE)"),
            {"c": city_id},
        )
    ).first():
        raise ValidationException(f"{label} is not an active city")


async def resolve_pricing_city(
    db: AsyncSession, origin_city_id: int | None, destination_city_id: int | None
) -> int:
    """Pin a route to one city's fare rules at save time.

    A per-request probe across cities would make the published "city_specific /
    default" source depend on rule-table ordering; deciding once, here, keeps a
    route's quote stable and auditable. Endpoints win when they are real cities,
    otherwise the city with the most hand-authored rules takes over — which is
    also what stops an empty test city from being chosen implicitly.
    """
    for candidate in (origin_city_id, destination_city_id):
        if candidate is None:
            continue
        if (
            await db.execute(
                text(
                    "SELECT 1 FROM cities WHERE id = :c AND COALESCE(is_active, TRUE)"
                ),
                {"c": candidate},
            )
        ).first():
            return int(candidate)
    row = (
        (
            await db.execute(
                text(
                    """
                    SELECT c.id FROM cities c
                    LEFT JOIN (
                        SELECT city_id, COUNT(*) AS n FROM vehicle_pricing_rules GROUP BY city_id
                    ) r ON r.city_id = c.id
                    WHERE COALESCE(c.is_active, TRUE)
                    ORDER BY COALESCE(r.n, 0) DESC, c.id ASC
                    LIMIT 1
                    """
                )
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        raise ValidationException(
            "No active city is available to price this route — add a city first"
        )
    return int(row)


async def assert_vehicle_categories_exist(db: AsyncSession, ids: list[int]) -> None:
    if not ids:
        return
    owned = (
        await db.execute(
            text(
                """
                SELECT COUNT(*) FROM vehicle_categories
                WHERE id = ANY(CAST(:ids AS BIGINT[])) AND is_active
                """
            ),
            {"ids": ids},
        )
    ).scalar_one()
    if owned != len(set(ids)):
        raise ValidationException("One or more vehicle categories are not active")


async def set_route_vehicles(
    db: AsyncSession, route_id: int, vehicle_category_ids: list[int]
) -> None:
    """Replace the route's vehicle list (the editor always sends the full set)."""
    await db.execute(
        text("DELETE FROM cab_route_vehicle_categories WHERE route_id = :r"),
        {"r": route_id},
    )
    for vc in sorted(set(vehicle_category_ids)):
        db.add(CabRouteVehicleCategory(route_id=route_id, vehicle_category_id=vc))
    await db.flush()


async def linked_vehicle_category_ids(db: AsyncSession, route_id: int) -> list[int]:
    return list(
        (
            await db.execute(
                text(
                    """
                    SELECT l.vehicle_category_id FROM cab_route_vehicle_categories l
                    JOIN vehicle_categories vc ON vc.id = l.vehicle_category_id
                    WHERE l.route_id = :r AND vc.is_active
                    ORDER BY vc.display_order, vc.id
                    """
                ),
                {"r": route_id},
            )
        )
        .scalars()
        .all()
    )


async def quote_vehicles(
    db: AsyncSession,
    *,
    pricing_city_id: int,
    distance_km: Decimal,
    vehicle_category_ids: list[int],
) -> list[dict]:
    """Live fare-engine breakdown per vehicle, in display order.

    A vehicle with no rule at all drops out rather than showing a zero price —
    the route page must never advertise a fare the booking flow cannot charge.
    """
    if not vehicle_category_ids:
        return []
    from app.modules.admin.services import DefaultPricingService
    from app.modules.booking.services.fare import calculate_fare_breakdown

    rows = (
        (
            await db.execute(
                text(
                    """
                    SELECT id, category_name, seating_capacity, image_url, icon_url
                    FROM vehicle_categories
                    WHERE id = ANY(CAST(:ids AS BIGINT[])) AND is_active
                    ORDER BY display_order, id
                    """
                ),
                {"ids": vehicle_category_ids},
            )
        )
        .mappings()
        .all()
    )
    km = float(distance_km)
    out: list[dict] = []
    for cat in rows:
        rule = await DefaultPricingService.get_effective_rule(
            db, pricing_city_id, cat["id"], TRIP_TYPE
        )
        if not rule:
            continue
        b = calculate_fare_breakdown(
            rule=rule,
            actual_distance_km=km,
            start_dt=None,
            end_dt=None,
            trip_type=TRIP_TYPE,
            trip_days=None,
        )
        out.append(
            {
                "vehicle_category_id": cat["id"],
                "category_name": cat["category_name"],
                "seating_capacity": cat["seating_capacity"],
                "image_url": cat["image_url"],
                "icon_url": cat["icon_url"],
                "base_fare": b["base_fare"],
                "minimum_km": b["minimum_km"],
                "per_km_rate": b["per_km_rate"],
                "driver_allowance": b["driver_allowance"],
                "night_charge_applied": b["night_charge_applied"],
                "distance_charge": b["distance_charge"],
                "is_night": b["is_night"],
                "total": b["total"],
                "source": rule.get("source", "default"),
                "actual_distance_km": b["actual_distance_km"],
                "billable_km": b["billable_km"],
                "driver_allowance_type": b["driver_allowance_type"],
                "trip_days": b["trip_days"],
            }
        )
    return out


async def assert_publishable(route: CabRoute, vehicle_count: int) -> None:
    if not (route.terms_and_conditions or "").strip():
        raise ValidationException("Add terms and conditions before publishing")
    if vehicle_count == 0:
        raise ValidationException("Select at least one vehicle type before publishing")


def endpoints_differ(origin: str, destination: str) -> bool:
    return origin.strip().lower() != destination.strip().lower()
