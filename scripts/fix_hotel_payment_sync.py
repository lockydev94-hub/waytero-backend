#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WayTero — Fix Hotel Payment Sync (startup repair)
===================================================

Problem:
  Hotel-side endpoints (partner and admin alike) used to update
  ``hotel_reservations.total_amount`` and insert ``hotel_advance_payments``
  rows but never re-rolled those values onto the umbrella
  ``master_bookings.payment_status`` / ``total_paid_amount`` columns.

  Result: a fully-paid hotel stay (reservation SETTLED, advances summing to
  the grand total) still showed the master row as ``PENDING`` / ``₹0`` paid on
  the admin /bookings list — the exact bug Anita Sethi's Bhubaneswar booking
  exposed.

Fix:
  On every backend restart, walk every master that has at least one
  ``booking_services`` row with ``service_type='HOTEL'`` and re-roll
  payment fields from the live ``hotel_reservations`` + ``hotel_advance_payments``
  rows. Idempotent — already-healthy rows are recomputed to the same values.

The same logic also lives in the live API as
``POST /admin/bookings/backfill-hotel-payment-sync`` for ad-hoc re-runs.

This script uses raw SQL (not the ORM) for the same reason
``fix_payment_state_mismatch.py`` does: a stand-alone subprocess has no
reason to load the full SQLAlchemy metadata graph, and raw SQL avoids the
``NoReferencedTableError`` that the ORM emits when an FK target table isn't
in the metadata.

Doc Ref: BRD Part 3 §35-§45 (cab/hotel fare engine + settlement custody),
        BRD Part 7 §155 (operational backfill endpoints).
"""

import asyncio
import os
import sys
from decimal import Decimal

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(_BACKEND_DIR, ".env"))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from app.core.config import settings


def _classify(paid: Decimal, total: Decimal) -> str:
    if paid <= Decimal("0"):
        return "PENDING"
    if paid < total:
        return "PARTIAL"
    return "PAID"


async def run() -> None:
    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    masters_examined = 0
    masters_synced = 0
    masters_skipped = 0

    async with async_session() as db:
        print("[fix_hotel_payment_sync] Scanning master bookings with HOTEL service…")

        # Distinct master ids that have at least one HOTEL BookingService row.
        master_ids = (
            await db.execute(
                text(
                    """
                    SELECT DISTINCT master_booking_id
                    FROM booking_services
                    WHERE service_type = 'HOTEL'
                    """
                )
            )
        ).scalars().all()
        masters_examined = len(master_ids)
        print(f"[fix_hotel_payment_sync] Found {masters_examined} master booking(s) to examine.")

        for mb_id in master_ids:
            # Sum ACTIVE hotel reservation totals on this master (excluding
            # cancelled / rejected / no-show).
            total_row = (
                await db.execute(
                    text(
                        """
                        SELECT COALESCE(SUM(total_amount), 0)
                        FROM hotel_reservations
                        WHERE master_booking_id = :mbid
                          AND reservation_status NOT IN ('CANCELLED', 'REJECTED', 'NO_SHOW')
                        """
                    ),
                    {"mbid": mb_id},
                )
            ).scalar() or Decimal("0")
            new_total = Decimal(str(total_row))

            # Sum ACTIVE advance + final payment rows on this master, net of refunds.
            paid_row = (
                await db.execute(
                    text(
                        """
                        SELECT COALESCE(
                            SUM(amount - COALESCE(refunded_amount, 0)),
                            0
                        )
                        FROM hotel_advance_payments
                        WHERE master_booking_id = :mbid
                          AND status = 'ACTIVE'
                        """
                    ),
                    {"mbid": mb_id},
                )
            ).scalar() or Decimal("0")
            new_paid = Decimal(str(paid_row))

            new_status = _classify(new_paid, new_total)

            # Fetch current master values to compare.
            mb_row = (
                await db.execute(
                    text(
                        """
                        SELECT total_amount, total_paid_amount, payment_status
                        FROM master_bookings
                        WHERE id = :mbid
                        """
                    ),
                    {"mbid": mb_id},
                )
            ).mappings().one_or_none()
            if mb_row is None:
                masters_skipped += 1
                continue

            before_total = Decimal(str(mb_row["total_amount"] or 0))
            before_paid = Decimal(str(mb_row["total_paid_amount"] or 0))
            before_status = mb_row["payment_status"]

            changed = (
                before_total != new_total
                or before_paid != new_paid
                or before_status != new_status
            )

            if not changed:
                masters_skipped += 1
                continue

            await db.execute(
                text(
                    """
                    UPDATE master_bookings
                    SET total_amount      = :total,
                        total_paid_amount = :paid,
                        payment_status    = :status,
                        updated_at        = NOW()
                    WHERE id = :mbid
                    """
                ),
                {
                    "total": new_total,
                    "paid": new_paid,
                    "status": new_status,
                    "mbid": mb_id,
                },
            )

            await db.execute(
                text(
                    """
                    INSERT INTO booking_timelines
                        (master_booking_id, event_type, event_description, event_timestamp)
                    VALUES
                        (:mbid, 'PAYMENT_SYNC_BACKFILL', :desc, NOW())
                    """
                ),
                {
                    "mbid": mb_id,
                    "desc": (
                        f"Startup repair: hotel payment sync re-rolled. "
                        f"total {before_total:.2f}→{new_total:.2f}, "
                        f"paid {before_paid:.2f}→{new_paid:.2f}, "
                        f"status {before_status}→{new_status}."
                    ),
                },
            )
            masters_synced += 1

        await db.commit()
        print(
            f"[fix_hotel_payment_sync] Done. "
            f"Examined {masters_examined}, updated {masters_synced}, "
            f"unchanged {masters_skipped}."
        )

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())