# ============================================================
# WAY TERO — LIVE CHAT: ADMIN CUSTOMER-CONTEXT AGGREGATION
# File: app/modules/chat/services/customer_context.py
# Doc Ref: Website Chat §4 — Admin customer context panel
#
# When an admin opens a conversation they need the full picture before
# replying: is this an existing customer or a brand-new one, what did
# they book recently, have they paid, do they have wallet balance.
# This module answers those questions in a few indexed queries.
# ============================================================

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text as _text
from sqlalchemy.ext.asyncio import AsyncSession

_CAB_SQL = _text(
    """
    SELECT cb.booking_number, cb.booking_status, cb.trip_type,
           cb.pickup_location, cb.drop_location, cb.pickup_datetime,
           cb.final_amount, cb.created_at
    FROM cab_bookings cb
    JOIN master_bookings mb ON mb.id = cb.master_booking_id
    WHERE mb.customer_id = :customer_id
    ORDER BY cb.created_at DESC
    LIMIT 5
    """
)

_HOTEL_SQL = _text(
    """
    SELECT hr.reservation_number, hr.reservation_status,
           h.hotel_name, hr.check_in_date, hr.check_out_date,
           hr.total_amount, hr.created_at
    FROM hotel_reservations hr
    LEFT JOIN hotels h ON h.id = hr.hotel_id
    WHERE hr.customer_id = :customer_id
    ORDER BY hr.created_at DESC
    LIMIT 5
    """
)

_PAYMENTS_SQL = _text(
    """
    SELECT p.payment_number, p.payment_type, p.payment_method,
           p.payment_status, p.amount, p.payment_datetime
    FROM payments p
    WHERE p.customer_id = :customer_id
    ORDER BY p.created_at DESC
    LIMIT 5
    """
)

_WALLET_SQL = _text(
    """
    SELECT w.available_balance, w.hold_balance, w.wallet_status
    FROM customer_wallets w
    WHERE w.customer_id = :customer_id
    """
)


async def _find_customer(
    db: AsyncSession, customer_user_id: UUID | None, mobile: str | None
):
    """Locate the customers row for a conversation identity.

    Prefer the registered user link; fall back to matching the provided
    mobile against the user account. Returns (customer_id, profile) or
    (None, None) when the person is brand-new.
    """
    if customer_user_id is not None:
        row = (
            (
                await db.execute(
                    _text(
                        """
                        SELECT c.id, c.customer_code, c.first_name, c.last_name,
                               c.created_at, u.mobile_number, u.email
                        FROM customers c
                        JOIN users u ON u.id = c.user_id
                        WHERE c.user_id = :uid AND c.deleted_at IS NULL
                        LIMIT 1
                        """
                    ),
                    {"uid": str(customer_user_id)},
                )
            )
            .mappings()
            .first()
        )
        if row:
            return row["id"], dict(row)
        return None, None

    if mobile:
        row = (
            (
                await db.execute(
                    _text(
                        """
                        SELECT c.id, c.customer_code, c.first_name, c.last_name,
                               c.created_at, u.mobile_number, u.email
                        FROM customers c
                        JOIN users u ON u.id = c.user_id
                        WHERE u.mobile_number = :mobile AND c.deleted_at IS NULL
                        LIMIT 1
                        """
                    ),
                    {"mobile": mobile},
                )
            )
            .mappings()
            .first()
        )
        if row:
            return row["id"], dict(row)
    return None, None


async def customer_context(
    db: AsyncSession,
    *,
    customer_user_id: UUID | None,
    guest_mobile: str | None,
    guest_email: str | None,
) -> dict:
    """Assemble the context payload shown in the admin chat panel."""
    customer_id, profile = await _find_customer(db, customer_user_id, guest_mobile)

    ctx: dict = {
        "is_existing": customer_id is not None,
        "profile": profile,
        "recent_bookings": [],
        "recent_payments": [],
        "wallet": None,
    }

    if customer_id is None:
        # Brand-new person — fall back to whatever the guest told us.
        ctx["profile"] = {
            "first_name": None,
            "last_name": None,
            "customer_code": None,
            "mobile_number": guest_mobile,
            "email": guest_email,
        }
        return ctx

    ctx["recent_bookings"] = [
        dict(r)
        for r in (await db.execute(_CAB_SQL, {"customer_id": customer_id}))
        .mappings()
        .all()
    ] + [
        dict(r)
        for r in (await db.execute(_HOTEL_SQL, {"customer_id": customer_id}))
        .mappings()
        .all()
    ]
    ctx["recent_bookings"].sort(
        key=lambda b: b.get("created_at") or b.get("pickup_datetime") or 0, reverse=True
    )

    ctx["recent_payments"] = [
        dict(r)
        for r in (await db.execute(_PAYMENTS_SQL, {"customer_id": customer_id}))
        .mappings()
        .all()
    ]

    wallet = (
        (await db.execute(_WALLET_SQL, {"customer_id": customer_id})).mappings().first()
    )
    if wallet:
        ctx["wallet"] = dict(wallet)

    return ctx
