#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WayTero — Direct asyncpg fix for CB-20260802192736-0B2931
Uses raw asyncpg (no SQLAlchemy ORM) to bypass any identity-map issues.
Run: python3 scripts/fix_booking_direct.py
"""
import asyncio, os, sys
from datetime import date, timezone, datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(_BACKEND_DIR, ".env"))

# Read DB creds from env
DB_HOST     = os.environ.get("DB_HOST", "localhost")
DB_PORT     = int(os.environ.get("DB_PORT", "5432"))
DB_NAME     = os.environ.get("DB_NAME", "waytero_db")
DB_USER     = os.environ.get("DB_USER", "waytero_user")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "waytero_dev_password")

BOOKING_NUMBER = "CB-20260802192736-0B2931"

async def run():
    import asyncpg
    print(f"Connecting to {DB_HOST}:{DB_PORT}/{DB_NAME} as {DB_USER}...")
    conn = await asyncpg.connect(
        host=DB_HOST, port=DB_PORT,
        database=DB_NAME, user=DB_USER, password=DB_PASSWORD,
    )
    print("Connected.\n")

    # ── STEP 1: Show current state ─────────────────────────────────────────
    row = await conn.fetchrow("""
        SELECT cb.id, cb.booking_number, cb.booking_status,
               cb.payment_mode, cb.cash_pending_at,
               cb.invoice_number, cb.final_amount,
               cb.master_booking_id,
               mb.id AS mb_id, mb.payment_status AS mb_payment_status
        FROM cab_bookings cb
        JOIN master_bookings mb ON mb.id = cb.master_booking_id
        WHERE cb.booking_number = $1
    """, BOOKING_NUMBER)

    if not row:
        print(f"ERROR: Booking {BOOKING_NUMBER} not found in database!")
        await conn.close(); return

    print("=== CURRENT STATE ===")
    for k in row.keys():
        print(f"  {k}: {row[k]}")

    # ── STEP 2: Show timeline ───────────────────────────────────────────────
    timeline = await conn.fetch("""
        SELECT bt.event_type, bt.event_description, bt.event_timestamp
        FROM booking_timelines bt
        WHERE bt.master_booking_id = $1
        ORDER BY bt.event_timestamp DESC
        LIMIT 10
    """, row["mb_id"])

    print("\n=== LAST 10 TIMELINE EVENTS ===")
    for t in timeline:
        desc = (t["event_description"] or "")[:80]
        print(f"  [{t['event_timestamp']}] {t['event_type']}")
        print(f"    {desc}")

    # ── STEP 3: Fix if needed ───────────────────────────────────────────────
    cb_id     = row["id"]
    mb_id     = row["mb_id"]
    pay_mode  = row["payment_mode"]
    invoice   = row["invoice_number"]

    if pay_mode is not None:
        print(f"\nNo fix needed — payment_mode is already '{pay_mode}'.")
        print("The UI button should be disabled. Try hard-refreshing the partner portal (Ctrl+Shift+R).")
        await conn.close(); return

    print(f"\n=== FIXING payment_mode=NULL for {BOOKING_NUMBER} ===")

    async with conn.transaction():
        # Fix cab_booking
        await conn.execute("""
            UPDATE cab_bookings
            SET payment_mode       = 'CASH',
                cash_pending_at    = 'DRIVER'
            WHERE id = $1
        """, cb_id)
        print(f"  ✓ cab_bookings.payment_mode = 'CASH', cash_pending_at = 'DRIVER'  (id={cb_id})")

        # Fix master_booking
        await conn.execute("""
            UPDATE master_bookings
            SET payment_status = 'PAID'
            WHERE id = $1
        """, mb_id)
        print(f"  ✓ master_bookings.payment_status = 'PAID'  (id={mb_id})")

        # Generate invoice if missing
        if not invoice:
            max_seq = await conn.fetchval("""
                SELECT COALESCE(
                    MAX(CAST(SPLIT_PART(invoice_number, '-', 4) AS INTEGER)), 0
                )
                FROM cab_bookings
                WHERE invoice_number IS NOT NULL
                  AND invoice_number LIKE 'WT-INV-%'
            """)
            new_invoice = f"WT-INV-{date.today().strftime('%Y%m')}-{str(max_seq + 1).zfill(5)}"
            await conn.execute("""
                UPDATE cab_bookings SET invoice_number = $1 WHERE id = $2
            """, new_invoice, cb_id)
            print(f"  ✓ invoice_number = '{new_invoice}'")
        else:
            print(f"  ✓ invoice_number already exists: '{invoice}'")

        # Log repair
        await conn.execute("""
            INSERT INTO booking_timelines
                (master_booking_id, event_type, event_description, event_timestamp)
            VALUES ($1, 'PAYMENT_STATE_REPAIRED', $2, NOW())
        """, mb_id,
            f"Direct-asyncpg repair: payment_mode=CASH, cash_pending_at=DRIVER set for {BOOKING_NUMBER}. "
            "ORM identity-map double-load caused payment_mode column not to persist despite db.commit()."
        )
        print("  ✓ PAYMENT_STATE_REPAIRED event logged to booking_timelines")

    # ── STEP 4: Confirm ────────────────────────────────────────────────────
    final = await conn.fetchrow("""
        SELECT cb.booking_number, cb.booking_status, cb.payment_mode,
               cb.cash_pending_at, cb.invoice_number,
               mb.payment_status AS mb_payment_status
        FROM cab_bookings cb
        JOIN master_bookings mb ON mb.id = cb.master_booking_id
        WHERE cb.booking_number = $1
    """, BOOKING_NUMBER)

    print("\n=== CONFIRMED FINAL STATE ===")
    for k in final.keys():
        print(f"  {k}: {final[k]}")

    print("\n✅ Fix complete!")
    print("   Partner portal: Collect Payment → DISABLED")
    print("   Partner portal: Download Invoice → ACTIVE")
    print("   Hard-refresh the partner portal page (Ctrl+Shift+R) to see the change.")

    await conn.close()

asyncio.run(run())
