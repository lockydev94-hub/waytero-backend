#!/usr/bin/env python3
"""
WayTero — Demo Partner Seeder (admin-equivalent registration).

Every other supply seeder refuses to write until an ACTIVE partner holds the
matching partner_services grant. A brand-new database has no partner at all,
so this script creates one exactly the way the admin dashboard does it:

  POST  /api/v1/admin/partners                      → users + partners (PENDING)
  PATCH /api/v1/admin/partners/{id}/approve          → APPROVED
  PATCH /api/v1/admin/partners/{id}/activate         → ACTIVE
  PATCH /api/v1/admin/partners/{id}/services         → partner_services

Rows written (mirrors admin_create_partner step by step):
  users                     — type=PARTNER, ACTIVE, default password
  partners                  — status ACTIVE, approved_at/by stamped
  partner_ratings           — 0 / 0
  wallets                   — PREPAID, ₹0, ACTIVE
  partner_gst_details       — COMPANY partners must carry GST (BRD Rule 14)
  partner_services          — only the services this dev box runs
  partner_verification_logs — one row per admin action, like the real flow

Services enabled: CAB and TOUR. HOTEL is deliberately not granted — this
installation only runs cab + tour, and granting HOTEL would let the hotel
seeders write supply the storefront cannot sell.

Partner login (partner portal):  9000000002 / Waytero@15

Idempotent: an existing partner with this mobile/partner_code is reused, and
its status + grants are re-asserted. Nothing is duplicated.

Usage (from Backend/):
    python -m scripts.seed_demo_partner
    python -m scripts.seed_demo_partner --dry-run
"""

import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings

DRY_RUN = "--dry-run" in sys.argv

PARTNER = {
    "partner_type": "COMPANY",
    "business_name": "Maa Travels Pvt. Ltd",
    "owner_name": "Rakesh Mohanty",
    "mobile": "9000000002",
    "email": "partner.demo@waytero.dev",
    "city": "Bhubaneswar",
    "gst_number": "21AABCM1234D1Z5",
    "pan_number": "AABCM1234D",
    "office_address_line_1": "Plot 42, Saheed Nagar",
    "office_postal_code": "751007",
    "default_password": "Waytero@15",
}
SERVICES = ["CAB", "TOUR"]


async def main() -> None:
    print("=" * 64)
    print("WayTero — Demo Partner Seeder")
    print("=" * 64)

    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as db:
        # ── 0. City + admin user (approver / created_by attribution) ──────
        city_id = (
            await db.execute(
                text("SELECT id FROM cities WHERE name = :n LIMIT 1"),
                {"n": PARTNER["city"]},
            )
        ).scalar()
        if city_id is None:
            print(f"FATAL: city '{PARTNER['city']}' is not in cities.")
            await engine.dispose()
            sys.exit(1)

        admin_row = (
            (
                await db.execute(
                    text(
                        "SELECT id, email FROM users "
                        "WHERE user_type IN ('ADMIN','SUPER_ADMIN') "
                        "ORDER BY created_at LIMIT 1"
                    )
                )
            )
            .mappings()
            .first()
        )
        if admin_row is None:
            print("FATAL: no ADMIN/SUPER_ADMIN user — run scripts.seed_admin first.")
            await engine.dispose()
            sys.exit(1)
        admin_id = admin_row["id"]

        existing = (
            (
                await db.execute(
                    text(
                        "SELECT p.id, p.partner_code, p.status, p.user_id "
                        "FROM partners p WHERE p.mobile = :m AND p.deleted_at IS NULL "
                        "LIMIT 1"
                    ),
                    {"m": PARTNER["mobile"]},
                )
            )
            .mappings()
            .first()
        )

        if DRY_RUN:
            state = "reuse" if existing else "create"
            print(f"DRY RUN — would {state} partner {PARTNER['business_name']}")
            print(f"          city={city_id} admin={admin_row['email']}")
            print(f"          services={SERVICES}")
            await engine.dispose()
            return

        now = datetime.now(timezone.utc)

        # ── 1. User account (users.user_type = PARTNER) ───────────────────
        if existing:
            partner_id = existing["id"]
            partner_code = existing["partner_code"]
            user_id = existing["user_id"]
            print(f"Partner : reusing id={partner_id} ({partner_code})")
        else:
            from argon2 import PasswordHasher

            # Same Argon2 parameters as app.core.security / admin_create_partner.
            hasher = PasswordHasher(
                time_cost=2, memory_cost=65536, parallelism=2, hash_len=32, salt_len=16
            )
            user_id = uuid.uuid4()
            name_parts = PARTNER["owner_name"].split(" ", 1)
            await db.execute(
                text(
                    """
                    INSERT INTO users
                        (id, user_code, first_name, last_name, mobile_number, email,
                         user_type, status, is_mobile_verified, is_email_verified,
                         is_active, failed_login_attempts, password_hash,
                         force_password_change, created_at, updated_at)
                    VALUES
                        (:id, :code, :first, :last, :mobile, :email,
                         'PARTNER', 'ACTIVE', TRUE, TRUE,
                         TRUE, 0, :pwd,
                         FALSE, NOW(), NOW())
                    """
                ),
                {
                    "id": user_id,
                    "code": f"USR{int(datetime.now().timestamp()) % 1000000:06d}",
                    "first": name_parts[0],
                    "last": name_parts[1] if len(name_parts) > 1 else None,
                    "mobile": PARTNER["mobile"],
                    "email": PARTNER["email"],
                    "pwd": hasher.hash(PARTNER["default_password"]),
                },
            )

            partner_code = f"PRT-{int(datetime.now().timestamp()) % 1000000:06d}"
            partner_id = (
                await db.execute(
                    text(
                        """
                        INSERT INTO partners
                            (uuid, user_id, partner_code, partner_type, business_name,
                             owner_name, mobile, email, city_id, status,
                             office_address_line_1, office_city_id, office_postal_code,
                             onboarding_source, created_at, updated_at)
                        VALUES
                            (:uuid, :user_id, :code, :ptype, :biz,
                             :owner, :mobile, :email, :city, 'PENDING',
                             :addr1, :city, :postal,
                             'ADMIN', NOW(), NOW())
                        RETURNING id
                        """
                    ),
                    {
                        "uuid": uuid.uuid4(),
                        "user_id": user_id,
                        "code": partner_code,
                        "ptype": PARTNER["partner_type"],
                        "biz": PARTNER["business_name"],
                        "owner": PARTNER["owner_name"],
                        "mobile": PARTNER["mobile"],
                        "email": PARTNER["email"],
                        "city": city_id,
                        "addr1": PARTNER["office_address_line_1"],
                        "postal": PARTNER["office_postal_code"],
                    },
                )
            ).scalar_one()
            print(f"Partner : created id={partner_id} ({partner_code})")

            await db.execute(
                text(
                    "INSERT INTO partner_ratings (partner_id, average_rating, "
                    "total_reviews, updated_at) VALUES (:pid, 0, 0, NOW()) "
                    "ON CONFLICT (partner_id) DO NOTHING"
                ),
                {"pid": partner_id},
            )
            await db.execute(
                text(
                    "INSERT INTO wallets (partner_id, wallet_type, available_balance, "
                    "hold_balance, credit_limit, wallet_status, created_at) "
                    "VALUES (:pid, 'PREPAID', 0, 0, 0, 'ACTIVE', NOW()) "
                    "ON CONFLICT (partner_id) DO NOTHING"
                ),
                {"pid": partner_id},
            )
            await db.execute(
                text(
                    "INSERT INTO partner_gst_details "
                    "(partner_id, gst_number, pan_number, legal_name, trade_name, created_at) "
                    "VALUES (:pid, :gst, :pan, :legal, :trade, NOW()) "
                    "ON CONFLICT (partner_id) DO UPDATE SET "
                    "gst_number = EXCLUDED.gst_number, pan_number = EXCLUDED.pan_number"
                ),
                {
                    "pid": partner_id,
                    "gst": PARTNER["gst_number"],
                    "pan": PARTNER["pan_number"],
                    "legal": PARTNER["business_name"],
                    "trade": PARTNER["business_name"],
                },
            )
            await db.execute(
                text(
                    "INSERT INTO partner_verification_logs "
                    "(partner_id, action, remarks, created_at) VALUES "
                    "(:pid, 'PARTNER_CREATED', "
                    "'Partner created by admin via admin portal', NOW())"
                ),
                {"pid": partner_id},
            )

        # ── 2. Approve + activate (same states the admin transitions walk) ──
        await db.execute(
            text(
                "UPDATE partners SET status = 'APPROVED', approved_at = :now, "
                "approved_by = :by, updated_at = NOW() "
                "WHERE id = :pid AND status <> 'ACTIVE'"
            ),
            {"pid": partner_id, "now": now, "by": admin_id},
        )
        await db.execute(
            text(
                "UPDATE partners SET status = 'ACTIVE', approved_at = "
                "COALESCE(approved_at, :now), approved_by = COALESCE(approved_by, :by), "
                "updated_at = NOW() WHERE id = :pid"
            ),
            {"pid": partner_id, "now": now, "by": admin_id},
        )
        existing_actions = set(
            (
                await db.execute(
                    text(
                        "SELECT action FROM partner_verification_logs "
                        "WHERE partner_id = :pid"
                    ),
                    {"pid": partner_id},
                )
            )
            .scalars()
            .all()
        )
        for action, remarks in (
            ("KYC_APPROVED", "KYC approved (demo seed)"),
            ("PARTNER_ACTIVATED", "Partner activated (demo seed)"),
        ):
            if action not in existing_actions:
                await db.execute(
                    text(
                        "INSERT INTO partner_verification_logs "
                        "(partner_id, action, remarks, created_at) "
                        "VALUES (:pid, :action, :remarks, NOW())"
                    ),
                    {"pid": partner_id, "action": action, "remarks": remarks},
                )

        # ── 3. Service grants (admin PATCH /partners/{id}/services) ───────
        await db.execute(
            text(
                "UPDATE partner_services SET is_active = FALSE WHERE partner_id = :pid"
            ),
            {"pid": partner_id},
        )
        for svc in SERVICES:
            await db.execute(
                text(
                    "INSERT INTO partner_services (partner_id, service_type, is_active, created_at) "
                    "VALUES (:pid, :svc, TRUE, NOW()) "
                    "ON CONFLICT (partner_id, service_type) DO UPDATE SET is_active = TRUE"
                ),
                {"pid": partner_id, "svc": svc},
            )
        await db.execute(
            text(
                "INSERT INTO partner_verification_logs (partner_id, action, remarks, created_at) "
                "VALUES (:pid, 'ADMIN_SERVICES_UPDATED', :remarks, NOW())"
            ),
            {"pid": partner_id, "remarks": f"Services updated to: {', '.join(SERVICES)}"},
        )

        await db.commit()

        print(f"Status  : ACTIVE  |  services: {' + '.join(SERVICES)}")
        print(f"Login   : {PARTNER['mobile']} / {PARTNER['default_password']}")

    await engine.dispose()
    print("Done. Partner can now own cab and tour supply.")


if __name__ == "__main__":
    asyncio.run(main())
