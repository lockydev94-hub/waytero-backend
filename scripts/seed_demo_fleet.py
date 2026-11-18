#!/usr/bin/env python3
"""
WayTero — Demo Fleet Seeder (cab vehicles + drivers, admin-equivalent).

A CAB grant with no fleet cannot take a booking: the customer search returns
no vehicle, auto-assign has nothing to pick, and the admin vehicle/driver
lists are empty. This script fills that gap the way the admin dashboard does:

  POST  /api/v1/admin/vehicles        → vehicles (status ACTIVE, approved)
  POST  /api/v1/admin/drivers         → drivers  (status ACTIVE, approved)
  driver_vehicle_assignments          → one active driver per vehicle
  vehicle_availability                → AVAILABLE

Everything hangs off the demo partner created by scripts.seed_demo_partner
(the lowest ACTIVE partner holding a CAB grant), so the portal shows the
fleet under the partner an admin would have registered it for.

Images and documents are deliberately NOT created — the admin uploads
vehicle photos and papers from the dashboard (vehicle_photo_uploads /
vehicle_documents stay empty).

Idempotent: rows are keyed by vehicle_code / registration_number /
driver_code / mobile and skipped when present.

Usage (from Backend/):
    python -m scripts.seed_demo_fleet
    python -m scripts.seed_demo_fleet --dry-run
"""

import asyncio
import os
import sys
import uuid
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings

DRY_RUN = "--dry-run" in sys.argv
CITY = "Bhubaneswar"

# (vehicle_code, category, registration, brand, model, year, fuel, seats)
VEHICLES = [
    ("VEH-DEMO-01", "HATCHBACK", "OD02AB1234", "Maruti Suzuki", "Wagon R", 2022, "CNG", 4),
    ("VEH-DEMO-02", "SEDAN", "OD02CD5678", "Honda", "Amaze", 2021, "PETROL", 4),
    ("VEH-DEMO-03", "SUV", "OD02EF9012", "Toyota", "Innova Crysta", 2023, "DIESEL", 6),
    ("VEH-DEMO-04", "TEMPO_TRAVELLER", "OD02GH3456", "Force", "Traveller 12D", 2022, "DIESEL", 12),
]

# (driver_code, full_name, mobile, licence, expiry, category index it drives)
DRIVERS = [
    ("DRV-DEMO-01", "Sanjay Behera", "9000000011", "OD0220150001234", "2031-06-30", 0),
    ("DRV-DEMO-02", "Prakash Jena", "9000000012", "OD0220160005678", "2030-03-15", 1),
    ("DRV-DEMO-03", "Bikash Nayak", "9000000013", "OD0220140009012", "2029-11-20", 2),
    ("DRV-DEMO-04", "Ramesh Das", "9000000014", "OD0220130003456", "2028-08-05", 3),
]


async def main() -> None:
    print("=" * 64)
    print("WayTero — Demo Fleet Seeder")
    print("=" * 64)

    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as db:
        partner = (
            (
                await db.execute(
                    text(
                        """
                        SELECT p.id, p.business_name
                        FROM partners p
                        JOIN partner_services ps
                          ON ps.partner_id = p.id AND ps.service_type = 'CAB'
                         AND ps.is_active
                        WHERE p.status = 'ACTIVE' AND p.deleted_at IS NULL
                        ORDER BY p.id LIMIT 1
                        """
                    )
                )
            )
            .mappings()
            .first()
        )
        if partner is None:
            print("FATAL: no ACTIVE partner holds a CAB grant.")
            print("       Run scripts.seed_demo_partner first.")
            await engine.dispose()
            sys.exit(1)

        city_id = (
            await db.execute(
                text("SELECT id FROM cities WHERE name = :n LIMIT 1"), {"n": CITY}
            )
        ).scalar()
        if city_id is None:
            print(f"FATAL: city '{CITY}' is not in cities.")
            await engine.dispose()
            sys.exit(1)

        admin_id = (
            await db.execute(
                text(
                    "SELECT id FROM users WHERE user_type IN ('ADMIN','SUPER_ADMIN') "
                    "ORDER BY created_at LIMIT 1"
                )
            )
        ).scalar()

        cats = dict(
            (
                await db.execute(
                    text(
                        "SELECT category_name, id FROM vehicle_categories "
                        "WHERE is_active"
                    )
                )
            )
            .all()
        )

        print(f"Partner : id={partner['id']} {partner['business_name']}")
        print(f"City    : {CITY} (id={city_id})")

        if DRY_RUN:
            for code, cat, reg, *_ in VEHICLES:
                print(f"  would insert vehicle {code} {cat} {reg}")
            for code, name, *_ in DRIVERS:
                print(f"  would insert driver {code} {name}")
            await engine.dispose()
            return

        now = datetime.now(timezone.utc)
        created_vehicles, created_drivers = 0, 0

        vehicle_ids: dict[str, int] = {}
        for code, cat, reg, brand, model, year, fuel, seats in VEHICLES:
            existing = (
                await db.execute(
                    text(
                        "SELECT id FROM vehicles WHERE vehicle_code = :c "
                        "OR registration_number = :r LIMIT 1"
                    ),
                    {"c": code, "r": reg},
                )
            ).scalar()
            if existing:
                vehicle_ids[code] = existing
                continue

            category_id = cats.get(cat)
            if category_id is None:
                print(f"  SKIP {code}: vehicle category {cat} is not active")
                continue

            vehicle_ids[code] = (
                await db.execute(
                    text(
                        """
                        INSERT INTO vehicles
                            (uuid, partner_id, vehicle_category_id, vehicle_code,
                             registration_number, vehicle_brand, vehicle_model,
                             manufacturing_year, fuel_type, seating_capacity,
                             city_id, status, approved_at, created_at, updated_at)
                        VALUES
                            (:uuid, :pid, :cat, :code,
                             :reg, :brand, :model,
                             :year, :fuel, :seats,
                             :city, 'ACTIVE', :now, NOW(), NOW())
                        RETURNING id
                        """
                    ),
                    {
                        "uuid": uuid.uuid4(),
                        "pid": partner["id"],
                        "cat": category_id,
                        "code": code,
                        "reg": reg,
                        "brand": brand,
                        "model": model,
                        "year": year,
                        "fuel": fuel,
                        "seats": seats,
                        "city": city_id,
                        "now": now,
                    },
                )
            ).scalar_one()
            await db.execute(
                text(
                    "INSERT INTO vehicle_availability "
                    "(vehicle_id, availability_status, updated_at) "
                    "VALUES (:v, 'AVAILABLE', NOW()) "
                    "ON CONFLICT (vehicle_id) DO UPDATE "
                    "SET availability_status = 'AVAILABLE', updated_at = NOW()"
                ),
                {"v": vehicle_ids[code]},
            )
            created_vehicles += 1
            print(f"  vehicle {code} {reg} ({cat}) id={vehicle_ids[code]}")

        for code, name, mobile, licence, expiry, idx in DRIVERS:
            existing = (
                await db.execute(
                    text(
                        "SELECT id FROM drivers WHERE driver_code = :c "
                        "OR mobile = :m LIMIT 1"
                    ),
                    {"c": code, "m": mobile},
                )
            ).scalar()
            if existing:
                driver_id = existing
            else:
                driver_id = (
                    await db.execute(
                        text(
                            """
                            INSERT INTO drivers
                                (uuid, partner_id, driver_code, full_name, mobile,
                                 license_number, license_expiry_date, joining_date,
                                 status, approved_at, approved_by, created_at, updated_at)
                            VALUES
                                (:uuid, :pid, :code, :name, :mobile,
                                 :licence, :expiry, :joined,
                                 'ACTIVE', :now, :by, NOW(), NOW())
                            RETURNING id
                            """
                        ),
                        {
                            "uuid": uuid.uuid4(),
                            "pid": partner["id"],
                            "code": code,
                            "name": name,
                            "mobile": mobile,
                            "licence": licence,
                            "expiry": date.fromisoformat(expiry),
                            "joined": date(2024, 1, 15),
                            "now": now,
                            "by": admin_id,
                        },
                    )
                ).scalar_one()
                created_drivers += 1
                print(f"  driver {code} {name} id={driver_id}")

            vcode = VEHICLES[idx][0]
            vehicle_id = vehicle_ids.get(vcode)
            if not vehicle_id:
                continue
            assigned = (
                await db.execute(
                    text(
                        "SELECT 1 FROM driver_vehicle_assignments "
                        "WHERE driver_id = :d AND vehicle_id = :v AND is_active"
                    ),
                    {"d": driver_id, "v": vehicle_id},
                )
            ).first()
            if not assigned:
                await db.execute(
                    text(
                        "INSERT INTO driver_vehicle_assignments "
                        "(driver_id, vehicle_id, assigned_at, is_active) "
                        "VALUES (:d, :v, NOW(), TRUE)"
                    ),
                    {"d": driver_id, "v": vehicle_id},
                )

        await db.commit()
        print(
            f"Created: {created_vehicles} vehicle(s), {created_drivers} driver(s) "
            "(existing rows reused)"
        )

    await engine.dispose()
    print("Done. Cab supply is ACTIVE, assigned and available.")


if __name__ == "__main__":
    asyncio.run(main())
