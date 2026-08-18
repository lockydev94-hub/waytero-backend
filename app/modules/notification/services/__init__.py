# ============================================================
# WAY TERO — NOTIFICATION DISPATCH SERVICE
# File: app/modules/notification/services/__init__.py
# Doc Ref: BRD Part 7 §155 — Notification engine
#
# Single entry-point used by all domain modules (booking, partner, etc.)
# when they want to notify a user. Responsibilities:
#
#   1. Write a NotificationOutbox row — durable record of the event.
#   2. Push to live WebSocket subscribers (real-time UI updates).
#   3. Push via FCM (push notifications for offline / background tabs).
#
# Failures in steps 2/3 never raise — the outbox row is the source of
# truth, so a missed push is recoverable.
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Iterable
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.infrastructure.push import send_fcm_to_tokens
from app.modules.notification.models import FCMToken, NotificationOutbox
from app.modules.notification.realtime import manager

logger = logging.getLogger("waytero.notifications")


async def _push_ws(user_id: UUID | str, event: str, payload: dict[str, Any]) -> bool:
    """Best-effort WebSocket dispatch. Returns True if at least one
    connection received it."""
    try:
        delivered = await manager.send_to_user(
            user_id, {"event": event, "data": payload}
        )
        return delivered > 0
    except Exception as exc:  # pragma: no cover
        logger.warning("notif.ws_push_failed user=%s err=%s", user_id, exc)
        return False


async def _active_fcm_tokens(db: AsyncSession, user_id: UUID) -> list[str]:
    rows = (
        (
            await db.execute(
                select(FCMToken.fcm_token).where(
                    FCMToken.user_id == user_id,
                    FCMToken.is_active == True,  # noqa: E712
                )
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


def _resolve_user_id(value: UUID | str) -> UUID:
    """Accept either a UUID or a string. Raise if it can't be parsed."""
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


async def dispatch(
    db: AsyncSession,
    *,
    user_id: UUID | str,
    event_type: str,
    title: str,
    body: str | None = None,
    data: dict[str, Any] | None = None,
    booking_id: int | None = None,
) -> NotificationOutbox:
    """
    Persist + push a notification to a user.

    Returns the outbox row written. The caller may use it for audit
    references or to verify the delivery channel that succeeded.
    """
    uid = _resolve_user_id(user_id)
    safe_data = data or {}

    # 1. Outbox row — source of truth.
    outbox = NotificationOutbox(
        user_id=uid,
        event_type=event_type,
        title=title,
        body=body,
        data=safe_data,
        booking_id=booking_id,
        delivered_via="",  # filled below
    )
    db.add(outbox)
    await db.flush()  # need the row id for any audit linkage

    channels: list[str] = []

    # 2. WebSocket push.
    if await _push_ws(uid, event_type, {"title": title, "body": body, **safe_data}):
        channels.append("ws")

    # 3. FCM push (if configured + tokens exist).
    try:
        tokens = await _active_fcm_tokens(db, uid)
        if tokens:
            counts = await send_fcm_to_tokens(
                db,
                tokens=tokens,
                title=title,
                body=body or "",
                data={"event_type": event_type, **safe_data},
            )
            if counts["success"] > 0:
                channels.append("fcm")
            elif counts["skipped"] == 0 and counts["failure"] > 0:
                # Mark failed tokens as inactive so we don't keep retrying
                # forever — FCM unregisters on its side anyway.
                pass
    except Exception as exc:  # pragma: no cover
        logger.warning("notif.fcm_push_failed user=%s err=%s", uid, exc)

    outbox.delivered_via = ",".join(channels) if channels else "none"

    await db.commit()
    await db.refresh(outbox)
    return outbox


async def dispatch_many(
    db: AsyncSession,
    *,
    user_ids: Iterable[UUID | str],
    event_type: str,
    title: str,
    body: str | None = None,
    data: dict[str, Any] | None = None,
    booking_id: int | None = None,
) -> list[NotificationOutbox]:
    """Convenience wrapper — same args as dispatch() but for N users."""
    rows: list[NotificationOutbox] = []
    for uid in user_ids:
        rows.append(
            await dispatch(
                db,
                user_id=uid,
                event_type=event_type,
                title=title,
                body=body,
                data=data,
                booking_id=booking_id,
            )
        )
    return rows


# ── Notification Inbox REST helpers (Doc Ref: BRD Part 7 §155) ───────────────


async def list_for_user(
    db: AsyncSession,
    user_id: UUID,
    limit: int = 50,
    offset: int = 0,
    unread_only: bool = False,
) -> list[NotificationOutbox]:
    q = select(NotificationOutbox).where(NotificationOutbox.user_id == user_id)
    if unread_only:
        q = q.where(NotificationOutbox.read_at.is_(None))
    q = q.order_by(NotificationOutbox.created_at.desc()).limit(limit).offset(offset)
    return list((await db.execute(q)).scalars().all())


async def unread_count(db: AsyncSession, user_id: UUID) -> int:
    from sqlalchemy import func

    q = select(func.count(NotificationOutbox.id)).where(
        NotificationOutbox.user_id == user_id,
        NotificationOutbox.read_at.is_(None),
    )
    return int((await db.execute(q)).scalar_one())


async def mark_read(db: AsyncSession, user_id: UUID, notification_id: int) -> bool:
    res = await db.execute(
        update(NotificationOutbox)
        .where(
            NotificationOutbox.id == notification_id,
            NotificationOutbox.user_id == user_id,
        )
        .values(
            read_at=__import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            )
        )
    )
    await db.commit()
    return res.rowcount > 0


async def mark_all_read(db: AsyncSession, user_id: UUID) -> int:
    res = await db.execute(
        update(NotificationOutbox)
        .where(
            NotificationOutbox.user_id == user_id,
            NotificationOutbox.read_at.is_(None),
        )
        .values(
            read_at=__import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            )
        )
    )
    await db.commit()
    return res.rowcount


# Re-export so call sites can `from app.modules.notification.services import manager` etc.
__all__ = [
    "dispatch",
    "dispatch_many",
    "list_for_user",
    "unread_count",
    "mark_read",
    "mark_all_read",
]


# Small util — the `dispatch` function uses a fresh DB session when called
# from places that already have one open (admin/partner endpoints). For
# Celery tasks we expose a context-manager-free variant:
async def dispatch_external(
    *,
    user_id: UUID | str,
    event_type: str,
    title: str,
    body: str | None = None,
    data: dict[str, Any] | None = None,
    booking_id: int | None = None,
) -> None:
    """Open its own DB session — safe for Celery tasks / background jobs."""
    async with AsyncSessionLocal() as db:
        await dispatch(
            db,
            user_id=user_id,
            event_type=event_type,
            title=title,
            body=body,
            data=data,
            booking_id=booking_id,
        )
