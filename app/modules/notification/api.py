# ============================================================
# WAY TERO — NOTIFICATION MODULE API
# File: app/modules/notification/api.py
# Doc Ref: BRD Part 7 §155 — Notification engine
#
# Public endpoints (mounted under /notifications by router.py):
#   POST   /me/fcm-token             register or refresh a device token
#   DELETE /me/fcm-token             deactivate a token (logout / uninstall)
#   GET    /me/notifications         inbox for the topbar dropdown
#   POST   /me/notifications/{id}/read  mark one read
#   POST   /me/notifications/read-all   mark all read
#   GET    /me/notifications/unread-count   badge counter
# ============================================================

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.modules.notification.models import FCMToken, NotificationOutbox
from app.modules.notification.services import (
    list_for_user,
    mark_all_read,
    mark_read,
    unread_count,
)

router = APIRouter()


# ── Schemas ──────────────────────────────────────────────────────────────────


class FCMTokenRegister(BaseModel):
    fcm_token: str = Field(..., min_length=10)
    platform: str = Field("WEB", pattern="^(WEB|ANDROID|IOS)$")
    user_agent: Optional[str] = None


class FCMTokenOut(BaseModel):
    id: int
    platform: str
    is_active: bool
    created_at: datetime


class NotificationOut(BaseModel):
    id: int
    event_type: str
    title: str
    body: Optional[str] = None
    data: Optional[dict] = None
    booking_id: Optional[int] = None
    delivered_via: str
    read_at: Optional[datetime] = None
    created_at: datetime


class NotificationListResponse(BaseModel):
    items: list[NotificationOut]
    unread_count: int


# ── FCM token endpoints ──────────────────────────────────────────────────────


@router.post(
    "/me/fcm-token",
    response_model=FCMTokenOut,
    status_code=status.HTTP_201_CREATED,
    tags=["Notifications"],
    summary="Register a device FCM token for the authenticated user",
)
async def register_fcm_token(
    payload: FCMTokenRegister,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Idempotent: if (user_id, fcm_token) already exists we refresh
    `last_seen_at` and `is_active` rather than creating a duplicate.
    """
    from sqlalchemy import select

    user_id = UUID(current_user["sub"])

    existing = (
        await db.execute(
            select(FCMToken).where(
                FCMToken.user_id == user_id,
                FCMToken.fcm_token == payload.fcm_token,
            )
        )
    ).scalar_one_or_none()

    now = datetime.now(timezone.utc)
    if existing:
        existing.platform = payload.platform
        existing.user_agent = payload.user_agent
        existing.is_active = True
        existing.last_seen_at = now
        await db.commit()
        await db.refresh(existing)
        return _token_out(existing)

    row = FCMToken(
        user_id=user_id,
        fcm_token=payload.fcm_token,
        platform=payload.platform,
        user_agent=payload.user_agent,
        is_active=True,
        last_seen_at=now,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _token_out(row)


@router.delete(
    "/me/fcm-token",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["Notifications"],
    summary="Deactivate a registered FCM token (logout / uninstall)",
)
async def deactivate_fcm_token(
    payload: FCMTokenRegister,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user_id = UUID(current_user["sub"])
    await db.execute(
        delete(FCMToken).where(
            FCMToken.user_id == user_id,
            FCMToken.fcm_token == payload.fcm_token,
        )
    )
    await db.commit()
    return None


# ── Inbox endpoints ──────────────────────────────────────────────────────────


@router.get(
    "/me/notifications",
    response_model=NotificationListResponse,
    tags=["Notifications"],
    summary="List the authenticated user's notification inbox",
)
async def list_notifications(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    unread_only: bool = Query(False),
):
    user_id = UUID(current_user["sub"])
    rows = await list_for_user(
        db, user_id, limit=limit, offset=offset, unread_only=unread_only
    )
    count = await unread_count(db, user_id)
    return NotificationListResponse(
        items=[_notif_out(r) for r in rows],
        unread_count=count,
    )


@router.get(
    "/me/notifications/unread-count",
    tags=["Notifications"],
    summary="Unread badge count for the topbar bell",
)
async def get_unread_count(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user_id = UUID(current_user["sub"])
    return {"unread_count": await unread_count(db, user_id)}


@router.post(
    "/me/notifications/{notification_id}/read",
    tags=["Notifications"],
    summary="Mark a single notification read",
)
async def post_mark_read(
    notification_id: int,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user_id = UUID(current_user["sub"])
    ok = await mark_read(db, user_id, notification_id)
    if not ok:
        raise HTTPException(404, "Notification not found")
    return {"success": True}


@router.post(
    "/me/notifications/read-all",
    tags=["Notifications"],
    summary="Mark every unread notification read",
)
async def post_mark_all_read(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user_id = UUID(current_user["sub"])
    n = await mark_all_read(db, user_id)
    return {"success": True, "marked": n}


# ── helpers ─────────────────────────────────────────────────────────────────


def _token_out(row: FCMToken) -> FCMTokenOut:
    return FCMTokenOut(
        id=row.id,
        platform=row.platform,
        is_active=row.is_active,
        created_at=row.created_at,
    )


def _notif_out(row: NotificationOutbox) -> NotificationOut:
    return NotificationOut(
        id=row.id,
        event_type=row.event_type,
        title=row.title,
        body=row.body,
        data=row.data,
        booking_id=row.booking_id,
        delivered_via=row.delivered_via,
        read_at=row.read_at,
        created_at=row.created_at,
    )
