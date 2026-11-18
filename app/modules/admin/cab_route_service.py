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


async def assert_endpoints_free(
    db: AsyncSession, origin: str, destination: str, exclude_id: int | None
) -> None:
    """Same corridor, second time — say so instead of letting Postgres raise.

    `uq_cab_routes_endpoints` (migration 0079) is unique on the lower-cased
    pair, so an INSERT of a corridor that already exists would surface as a
    500. Checked here to turn that into a message the admin can act on.
    """
    sql = """
        SELECT 1 FROM cab_routes
        WHERE lower(origin_label) = lower(:o)
          AND lower(destination_label) = lower(:d)
    """
    params: dict = {"o": origin, "d": destination}
    if exclude_id is not None:
        # Only bound when there is a value — asyncpg cannot type a bare NULL.
        sql += " AND id <> :exclude"
        params["exclude"] = exclude_id
    if (await db.execute(text(sql), params)).first():
        raise ValidationException(
            f"A route from {origin} to {destination} already exists — edit that one"
        )


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


async def quote_route_vehicles(
    db: AsyncSession,
    *,
    pricing_city_id: int,
    distance_km: Decimal,
    vehicle_category_ids: list[int],
) -> list[dict]:
    """What the booking flow charges for this corridor, per vehicle type.

    Priced from the city's rate card, because that is what a search for this
    route returns: an outstation package with its included kilometres, driver
    allowance and extra-km rate. The earlier per-km rule quote was a second,
    cheaper price for the same journey — the route page said ₹1,584 for a
    Bhubaneswar→Puri sedan while checkout charged ₹4,150.

    Where several rate classes share one bookable vehicle category (MUV and
    Premium MUV are both MUV), the cheapest is shown: the results page does the
    same, so the card here is the card there. A vehicle with no card row at all
    drops out rather than showing a zero price.
    """
    if not vehicle_category_ids:
        return []
    from app.modules.booking.services.rental_booking import load_rate_card
    from app.modules.booking.services.rental_rates import calculate_rental_fare

    by_class, card_source = await load_rate_card(db, pricing_city_id)
    if not by_class:
        return []

    categories = (
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
    cheapest: dict[int, dict] = {}
    for vehicle_class, class_rows in by_class.items():
        if not any(
            str(row.get("rental_type") or "").upper() == TRIP_TYPE for row in class_rows
        ):
            continue
        category_id = class_rows[0].get("vehicle_category_id")
        if category_id is None:
            continue
        category_id = int(category_id)
        if category_id not in {int(c["id"]) for c in categories}:
            continue
        quote = calculate_rental_fare(
            class_rows, rental_type=TRIP_TYPE, hours=0, km=km, days=1
        )
        if category_id in cheapest and cheapest[category_id]["total"] <= quote.total:
            continue
        cheapest[category_id] = {
            "vehicle_class": vehicle_class,
            "vehicle_label": class_rows[0].get("vehicle_label") or vehicle_class,
            "vehicle_models": class_rows[0].get("vehicle_models"),
            "rental_type": TRIP_TYPE,
            "included_hours": quote.package_hours,
            "included_km": quote.package_km,
            "package_price": quote.package_price,
            "extra_km": quote.extra_km,
            "extra_km_rate": quote.extra_km_rate,
            "extra_km_charge": quote.extra_km_charge,
            "extra_hour_rate": quote.extra_hour_rate,
            "driver_allowance": quote.driver_allowance,
            "midnight_charge": quote.midnight_charge,
            "total": quote.total,
            "rate_source": card_source.get(vehicle_class, "PLATFORM"),
            "toll_included": quote.toll_included,
            "parking_included": quote.parking_included,
            "night_halt_included": quote.night_halt_included,
            "actual_distance_km": km,
        }

    out: list[dict] = []
    for cat in categories:
        priced = cheapest.get(int(cat["id"]))
        if not priced:
            continue
        out.append(
            {
                "vehicle_category_id": cat["id"],
                "category_name": cat["category_name"],
                "seating_capacity": cat["seating_capacity"],
                "image_url": cat["image_url"],
                "icon_url": cat["icon_url"],
                **priced,
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
