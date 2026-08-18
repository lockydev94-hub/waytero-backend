# ============================================================
# WAYTERO — HOTEL INVENTORY EXTENDER
# File: app/workers/inventory.py
# Doc Ref:
#   Docs/21_Hotel_Module_Implementation/00_HOTEL_MODULE_MASTER_PLAN.md §3.3
#   BRD Part 4 §67 — Inventory must always cover the search horizon
#   SRS Part 5 §161-162, §192 — availability from materialised rows
#
# Periodic task: every hour, for every ACTIVE hotel, ensure inventory
# rows exist up to TODAY + HOTEL_INVENTORY_HORIZON_DAYS (config). New
# rows copy the category's current total_rooms and start with 0 booked/
# blocked/held. Existing rows are NEVER overwritten — a row may carry
# bookings, blocks or a rate override, and regenerating would silently
# destroy them. This is the same idempotent guarantee
# InventoryService.generate gives to the manual /admin/hotels/{id}/
# inventory/generate endpoint; the only difference is that we walk every
# ACTIVE hotel instead of one.
#
# On every successful run we write a single platform_audit_logs row so
# the audit page can show "inventory extended for N hotels, M rows
# created, K skipped" — the row count is the only signal the user has
# that the sweeper is actually doing its job.
# ============================================================

from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.modules.hotel.constants import CONFIG_HOTEL_INVENTORY_HORIZON_DAYS
from app.modules.hotel.services import get_config_int
from app.modules.hotel.models import Hotel, HotelInventory, HotelRoomCategory
from app.workers.celery_app import celery_app

logger = logging.getLogger("waytero.inventory")


@celery_app.task(name="waytero.inventory.extend_horizon")
def extend_hotel_inventory_horizon() -> dict[str, int]:
    """Extend hotel_inventory rows forward to TODAY + horizon_days.

    Returns a small stats dict — Celery stores it as the task result so an
    operator can inspect with `celery result`. Numbers reflect one run
    across the whole platform, not per-hotel.
    """
    import asyncio

    return asyncio.run(_extend_async())


async def _extend_async() -> dict[str, int]:
    today = date.today()
    horizon = await _resolve_horizon_days()
    target_end = today + timedelta(days=horizon)

    stats = {"hotels_scanned": 0, "rows_created": 0, "rows_skipped": 0}

    async with AsyncSessionLocal() as db:
        hotels = (
            await db.execute(
                select(Hotel.id, Hotel.hotel_code, Hotel.hotel_name).where(
                    Hotel.deleted_at.is_(None),
                    Hotel.status == "ACTIVE",
                )
            )
        ).all()

        for hid, code, name in hotels:
            stats["hotels_scanned"] += 1
            categories = (
                (
                    await db.execute(
                        select(HotelRoomCategory).where(
                            HotelRoomCategory.hotel_id == hid,
                            HotelRoomCategory.is_active.is_(True),
                        )
                    )
                )
                .scalars()
                .all()
            )

            for cat in categories:
                # Existing rows in [today, target_end] are kept as-is. We
                # only add rows for dates that don't yet have one.
                existing_dates = set(
                    (
                        await db.execute(
                            select(HotelInventory.inventory_date).where(
                                HotelInventory.room_category_id == int(cat.id),
                                HotelInventory.inventory_date >= today,
                                HotelInventory.inventory_date <= target_end,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )

                total = int(cat.total_rooms or 0)
                rows = []
                for offset in range(horizon + 1):
                    day = today + timedelta(days=offset)
                    if day in existing_dates:
                        stats["rows_skipped"] += 1
                        continue
                    rows.append(
                        HotelInventory(
                            hotel_id=int(hid),
                            room_category_id=int(cat.id),
                            inventory_date=day,
                            total_rooms=total,
                            booked_rooms=0,
                            blocked_rooms=0,
                            held_rooms=0,
                            available_rooms=total,
                        )
                    )

                if rows:
                    db.add_all(rows)
                    await db.flush()
                    stats["rows_created"] += len(rows)

            logger.info(
                "inventory.extend hotel_id=%s code=%s created_total_running=%s",
                hid,
                code,
                stats["rows_created"],
            )

        await _write_audit_row(db, stats)

    logger.info(
        "inventory.extend.done hotels=%s created=%s skipped=%s",
        stats["hotels_scanned"],
        stats["rows_created"],
        stats["rows_skipped"],
    )
    return stats


async def _resolve_horizon_days() -> int:
    """Read HOTEL_INVENTORY_HORIZON_DAYS from system_configurations; default 365.

    Hardcoded default must match the constant InventoryService.generate
    uses (app.modules.hotel.constants.CONFIG_HOTEL_INVENTORY_HORIZON_DAYS)
    so the scheduled job never extends further than the manual endpoint
    allows.
    """
    async with AsyncSessionLocal() as db:
        try:
            value = await get_config_int(db, CONFIG_HOTEL_INVENTORY_HORIZON_DAYS, 365)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("inventory.horizon_config_read_failed err=%s", exc)
            return 365
        # The same MAX cap the service enforces; honour it here so the
        # scheduled job can't violate the constraint.
        return max(1, min(int(value), 730))


async def _write_audit_row(db, stats: dict[str, int]) -> None:
    """Single platform_audit_logs row per run. Best-effort."""
    try:
        from sqlalchemy import text

        await db.execute(
            text(
                "INSERT INTO platform_audit_logs "
                "(module_name, action, entity_type, entity_id, "
                " details, created_at) "
                "VALUES ('HOTEL', 'INVENTORY_HORIZON_EXTENDED', 'system', NULL, "
                "        CAST(:details AS jsonb), NOW())"
            ),
            {"details": _json_dumps(stats)},
        )
        await db.commit()
    except Exception as exc:  # pragma: no cover - audit is best-effort
        logger.warning("inventory.audit_write_failed err=%s", exc)


def _json_dumps(payload: dict[str, int]) -> str:
    import json

    return json.dumps(payload)
