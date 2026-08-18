#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WayTero — Fix Payment State Mismatch
======================================
Problem:
  Some bookings have a PAYMENT_CASH_COLLECTED or PAYMENT_WALLET_COLLECTED
  event in their timeline (meaning payment WAS recorded and the timeline
  INSERT committed) but the cab_booking.payment_mode column is still NULL.

  This happens when:
    1. The timeline INSERT committed inside a transaction
    2. The outer db.commit() silently rolled back (DB hiccup / restart mid-write)
    3. Result: timeline says "paid" but payment_mode column says nothing

  Effect on UI:
    - Partner portal shows "Collect Payment" still active (reads payment_mode)
    - Admin portal shows it correctly disabled (reads timeline for display)
    - Download Invoice stays disabled (reads invoice_number)

Fix:
  For each affected booking:
    1. Read cash/wallet from timeline event to determine payment_mode
    2. Set cab_booking.payment_mode  = CASH | WALLET
    3. Set cab_booking.cash_pending_at = DRIVER (CASH) | NONE (WALLET)
    4. Set master_booking.payment_status = PAID
    5. Auto-generate invoice_number if missing (MAX-based, race-safe)
    6. Log PAYMENT_STATE_REPAIRED timeline event

Idempotent — safe to run on every backend restart.
Doc Ref: DB Schema Part 7 §3-5, BRD Part 3 §45
"""

import asyncio
import os
import sys
from datetime import date

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(_BACKEND_DIR, ".env"))

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text
from app.core.config import settings


async def run():
    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as db:
        print("[fix_payment_mismatch] Checking for payment state mismatches...")

        # Find cab_bookings where:
        # - timeline has a PAYMENT_*_COLLECTED event  (payment was started)
        # - BUT cab_booking.payment_mode is still NULL (commit did not finish)
        result = await db.execute(text("""
            SELECT DISTINCT
                cb.id            AS cb_id,
                cb.booking_number,
                cb.booking_status,
                cb.master_booking_id,
                cb.payment_mode,
                cb.invoice_number,
                mb.id            AS mb_id,
                mb.payment_status,
                -- Determine mode from timeline event type
                CASE
                    WHEN EXISTS (
                        SELECT 1 FROM booking_timelines bte2
                        WHERE bte2.master_booking_id = mb.id
                          AND bte2.event_type = 'PAYMENT_WALLET_COLLECTED'
                    ) THEN 'WALLET'
                    ELSE 'CASH'
                END AS inferred_mode
            FROM cab_bookings cb
            JOIN master_bookings mb ON mb.id = cb.master_booking_id
            WHERE cb.payment_mode IS NULL
              AND cb.booking_status IN ('COMPLETED', 'SETTLEMENT_PENDING', 'SETTLED')
              AND EXISTS (
                  SELECT 1 FROM booking_timelines bte
                  WHERE bte.master_booking_id = mb.id
                    AND bte.event_type IN (
                        'PAYMENT_CASH_COLLECTED',
                        'PAYMENT_WALLET_COLLECTED'
                    )
              )
        """))
        rows = result.mappings().all()

        if not rows:
            print("[fix_payment_mismatch] OK — No mismatched bookings found.")
        else:
            print(f"[fix_payment_mismatch] Found {len(rows)} booking(s) with payment state mismatch (timeline-based).")

        # ── SECOND PASS: bookings where master_booking.payment_status=PAID
        # but cab_booking.payment_mode is still NULL AND no payment timeline event exists.
        # This covers cases where admin may have set payment_status directly (e.g. via
        # external DB update, or a different code path that updated master_bookings only).
        result2 = await db.execute(text("""
            SELECT
                cb.id            AS cb_id,
                cb.booking_number,
                cb.booking_status,
                cb.master_booking_id,
                cb.invoice_number,
                mb.id            AS mb_id,
                mb.payment_status
            FROM cab_bookings cb
            JOIN master_bookings mb ON mb.id = cb.master_booking_id
            WHERE cb.payment_mode IS NULL
              AND cb.booking_status IN ('COMPLETED', 'SETTLEMENT_PENDING', 'SETTLED')
              AND mb.payment_status = 'PAID'
              AND NOT EXISTS (
                  SELECT 1 FROM booking_timelines bte
                  WHERE bte.master_booking_id = mb.id
                    AND bte.event_type IN (
                        'PAYMENT_CASH_COLLECTED',
                        'PAYMENT_WALLET_COLLECTED'
                    )
              )
        """))
        rows2 = result2.mappings().all()

        if rows2:
            print(f"[fix_payment_mismatch] Found {len(rows2)} additional booking(s) where payment_status=PAID but payment_mode is NULL (no timeline).")
            for row in rows2:
                cb_id      = row["cb_id"]
                bn         = row["booking_number"]
                status     = row["booking_status"]
                mb_id      = row["mb_id"]
                invoice_no = row["invoice_number"]

                print(f"  Repairing {bn}: status={status}, inferred_mode=CASH (default for no-timeline case), invoice={invoice_no or 'NONE'}")

                # Default to CASH since there's no timeline to tell us mode
                mode = "CASH"
                cash_pending = "NONE"  # don't create cash tracking burden for old bookings

                await db.execute(text("""
                    UPDATE cab_bookings
                    SET payment_mode    = :mode,
                        payment_collected_by = 'PARTNER',
                        cash_pending_at = :cash_pending
                    WHERE id = :cb_id
                """), {"mode": mode, "cash_pending": cash_pending, "cb_id": cb_id})

                # Generate invoice if missing
                if not invoice_no:
                    max_seq_row2 = await db.execute(text(
                        "SELECT MAX(CAST(SPLIT_PART(invoice_number, '-', 4) AS INTEGER)) "
                        "FROM cab_bookings WHERE invoice_number IS NOT NULL AND invoice_number LIKE 'WT-INV-%'"
                    ))
                    base_seq2 = max_seq_row2.scalar() or 0
                    new_invoice2 = f"WT-INV-{today.strftime('%Y%m')}-{str(base_seq2 + 1).zfill(5)}"
                    await db.execute(text("""
                        UPDATE cab_bookings SET invoice_number = :inv WHERE id = :cb_id
                    """), {"inv": new_invoice2, "cb_id": cb_id})
                    print(f"    -> Invoice generated: {new_invoice2}")

                # Move to SETTLEMENT_PENDING if still COMPLETED
                if status == "COMPLETED":
                    await db.execute(text("""
                        UPDATE cab_bookings SET booking_status = 'SETTLEMENT_PENDING' WHERE id = :cb_id
                    """), {"cb_id": cb_id})

                await db.execute(text("""
                    INSERT INTO booking_timelines
                        (master_booking_id, event_type, event_description, event_timestamp)
                    VALUES (
                        :mid,
                        'PAYMENT_STATE_REPAIRED',
                        :desc,
                        NOW()
                    )
                """), {
                    "mid":  mb_id,
                    "desc": (
                        f"Startup repair: payment_mode set to CASH for {bn} (master_booking.payment_status was PAID but cab_booking.payment_mode was NULL). "
                        f"cash_pending_at=NONE. Invoice {'generated' if not invoice_no else 'already existed'}."
                    ),
                })

            await db.commit()
            print(f"[fix_payment_mismatch] Repaired {len(rows2)} additional booking(s).")
        else:
            print("[fix_payment_mismatch] OK — No payment_status=PAID / payment_mode=NULL mismatches found.")

        if not rows:
            await engine.dispose()
            return

        print(f"[fix_payment_mismatch] Found {len(rows)} booking(s) with payment state mismatch.")

        # Get MAX invoice sequence for safe generation
        max_seq_row = await db.execute(text(
            "SELECT MAX(CAST(SPLIT_PART(invoice_number, '-', 4) AS INTEGER)) "
            "FROM cab_bookings WHERE invoice_number IS NOT NULL AND invoice_number LIKE 'WT-INV-%'"
        ))
        base_seq = max_seq_row.scalar() or 0
        today = date.today()
        extra_seq = 0

        for row in rows:
            cb_id      = row["cb_id"]
            bn         = row["booking_number"]
            status     = row["booking_status"]
            mb_id      = row["mb_id"]
            mode       = row["inferred_mode"]
            invoice_no = row["invoice_number"]
            pay_status = row["payment_status"]

            cash_pending = "DRIVER" if mode == "CASH" else "NONE"

            print(f"  Repairing {bn}: status={status}, inferred_mode={mode}, invoice={invoice_no or 'NONE'}")

            # 1. Set payment_mode + cash_pending_at
            await db.execute(text("""
                UPDATE cab_bookings
                SET payment_mode       = :mode,
                    cash_pending_at    = :cash_pending
                WHERE id = :cb_id
            """), {"mode": mode, "cash_pending": cash_pending, "cb_id": cb_id})

            # 2. Set master booking payment_status = PAID
            if pay_status != "PAID":
                await db.execute(text("""
                    UPDATE master_bookings SET payment_status = 'PAID' WHERE id = :mb_id
                """), {"mb_id": mb_id})

            # 3. Auto-generate invoice_number if missing
            if not invoice_no:
                extra_seq += 1
                seq = base_seq + extra_seq
                new_invoice = f"WT-INV-{today.strftime('%Y%m')}-{str(seq).zfill(5)}"
                await db.execute(text("""
                    UPDATE cab_bookings SET invoice_number = :inv WHERE id = :cb_id
                """), {"inv": new_invoice, "cb_id": cb_id})
                print(f"    -> Invoice generated: {new_invoice}")

            # 4. Log repair event in timeline
            await db.execute(text("""
                INSERT INTO booking_timelines
                    (master_booking_id, event_type, event_description, event_timestamp)
                VALUES (
                    :mid,
                    'PAYMENT_STATE_REPAIRED',
                    :desc,
                    NOW()
                )
            """), {
                "mid":  mb_id,
                "desc": (
                    f"Startup repair: payment_mode set to {mode} for {bn}. "
                    f"cash_pending_at={cash_pending}. "
                    f"Invoice {'generated' if not invoice_no else 'already existed'}. "
                    "Mismatch was caused by a partial DB commit during payment collection."
                ),
            })

        await db.commit()
        print(f"[fix_payment_mismatch] Repaired {len(rows)} booking(s). Done.")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
