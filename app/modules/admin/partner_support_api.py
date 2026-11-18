# ============================================================
# WAYTERO — ADMIN PARTNER SUPPORT
# File: app/modules/admin/partner_support_api.py
# Prefix: /admin/support-tickets (see app/api/router.py)
# Doc Ref: 04_FINANCE_ACCOUNT.md AC4
# ============================================================

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.branch_scope import BranchScope, get_branch_scope
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.infrastructure.cache.redis_client import RedisCache, get_redis
from app.infrastructure.email import send_event_email
from app.modules.admin.services.dashboard import action_counts_cache_key
from app.modules.notification.services import dispatch

router = APIRouter()
logger = logging.getLogger("waytero.partner_support")

_STATUS_LABELS = {
    "OPEN": "Open",
    "IN_PROGRESS": "In progress",
    "RESOLVED": "Resolved",
    "CLOSED": "Closed",
}


class SupportTicketUpdate(BaseModel):
    status: Optional[str] = Field(
        None, description="OPEN | IN_PROGRESS | RESOLVED | CLOSED"
    )
    priority: Optional[str] = Field(None, description="LOW | NORMAL | HIGH | URGENT")
    resolution_notes: Optional[str] = Field(None, max_length=4000)


class SupportMessageCreate(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)


_STATUSES = {"OPEN", "IN_PROGRESS", "RESOLVED", "CLOSED"}
_PRIORITIES = {"LOW", "NORMAL", "HIGH", "URGENT"}


def _row_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "ticket_number": row["ticket_number"],
        "partner_id": row["partner_id"],
        "partner_code": row["partner_code"],
        "business_name": row["business_name"],
        "owner_name": row["owner_name"],
        "mobile": row["mobile"],
        "category": row["category"],
        "subject": row["subject"],
        "description": row["description"],
        "booking_reference": row["booking_reference"],
        "priority": row["priority"],
        "status": row["status"],
        "resolution_notes": row["resolution_notes"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        "resolved_at": row["resolved_at"].isoformat() if row["resolved_at"] else None,
        "unread_count": int(row["unread_count"] or 0),
    }


def _message_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "ticket_id": row["ticket_id"],
        "author_type": row["author_type"],
        "author_name": row["author_name"],
        "body": row["body"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


async def _ticket_row(db: AsyncSession, ticket_id: int):
    return (
        (
            await db.execute(
                text(
                    """
                    SELECT t.id, t.ticket_number, t.partner_id,
                           p.partner_code, p.business_name, p.owner_name, p.mobile,
                           t.category, t.subject, t.description,
                           t.booking_reference, t.priority, t.status,
                           t.resolution_notes, t.created_at, t.updated_at,
                           t.resolved_at,
                           (SELECT COUNT(*)
                              FROM partner_support_ticket_messages m
                             WHERE m.ticket_id = t.id
                               AND m.author_type = 'PARTNER'
                               AND m.read_by_admin_at IS NULL) AS unread_count
                      FROM partner_support_tickets t
                      JOIN partners p ON p.id = t.partner_id
                     WHERE t.id = :id
                    """
                ),
                {"id": ticket_id},
            )
        )
        .mappings()
        .one_or_none()
    )


async def _messages(db: AsyncSession, ticket_id: int) -> list[dict]:
    rows = (
        (
            await db.execute(
                text(
                    """
                    SELECT id, ticket_id, author_type, author_name, body, created_at
                      FROM partner_support_ticket_messages
                     WHERE ticket_id = :id
                     ORDER BY created_at ASC, id ASC
                    """
                ),
                {"id": ticket_id},
            )
        )
        .mappings()
        .all()
    )
    return [_message_to_dict(row) for row in rows]


async def _notify_partner_status(
    db: AsyncSession,
    *,
    partner_user_id,
    partner_email: Optional[str],
    partner_name: Optional[str],
    ticket: dict,
) -> None:
    """Push + email a status change. Never fails the admin's save."""
    if not partner_user_id:
        return

    label = _STATUS_LABELS.get(ticket["status"], ticket["status"])
    title = f"Support ticket {ticket['ticket_number']} is now {label.lower()}"
    body = f"{ticket['subject']} — {label}."

    try:
        await dispatch(
            db,
            user_id=partner_user_id,
            event_type="partner_support_status",
            title=title,
            body=body,
            data={
                "ticket_id": ticket["id"],
                "ticket_number": ticket["ticket_number"],
                "status": ticket["status"],
                "screen": "support",
            },
        )
    except Exception as exc:
        logger.warning(
            "partner_support.push_failed ticket=%s err=%s", ticket["id"], exc
        )

    if not partner_email:
        return
    try:
        await send_event_email(
            db,
            event_type="partner_support_status",
            to_email=partner_email,
            to_name=partner_name,
            context={
                "ticket_number": ticket["ticket_number"],
                "status_label": label,
                "body": [
                    f"Your support ticket {ticket['ticket_number']} "
                    f"({ticket['subject']}) is now {label.lower()}.",
                    "Open Help & support in the partner app to see the full conversation.",
                ],
                "details": [
                    ("Ticket", ticket["ticket_number"]),
                    ("Subject", ticket["subject"]),
                    ("Status", label),
                ],
            },
            related_type="PARTNER_SUPPORT_TICKET",
            related_id=str(ticket["id"]),
        )
    except Exception as exc:
        logger.warning(
            "partner_support.email_failed ticket=%s err=%s", ticket["id"], exc
        )


@router.get("", summary="List partner support tickets")
async def list_support_tickets(
    status_filter: Optional[str] = Query(None, alias="status"),
    category: Optional[str] = Query(None),
    priority: Optional[str] = Query(None),
    search: Optional[str] = Query(None, max_length=120),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(get_current_user),
):
    where = ["1=1"]
    params: dict = {}
    if status_filter:
        where.append("t.status = :status")
        params["status"] = status_filter.strip().upper()
    if category:
        where.append("t.category = :category")
        params["category"] = category.strip().upper()
    if priority:
        where.append("t.priority = :priority")
        params["priority"] = priority.strip().upper()
    if search and search.strip():
        where.append(
            "(t.ticket_number ILIKE :search OR p.partner_code ILIKE :search "
            "OR p.business_name ILIKE :search OR p.owner_name ILIKE :search "
            "OR p.mobile ILIKE :search OR t.subject ILIKE :search)"
        )
        params["search"] = f"%{search.strip()}%"
    where_sql = " AND ".join(where)

    total = (
        await db.execute(
            text(
                "SELECT COUNT(*) FROM partner_support_tickets t "
                f"JOIN partners p ON p.id = t.partner_id WHERE {where_sql}"
            ),
            params,
        )
    ).scalar() or 0

    rows = (
        (
            await db.execute(
                text(
                    f"""
                    SELECT t.id, t.ticket_number, t.partner_id,
                           p.partner_code, p.business_name, p.owner_name, p.mobile,
                           t.category, t.subject, t.description,
                           t.booking_reference, t.priority, t.status,
                           t.resolution_notes, t.created_at, t.updated_at,
                           t.resolved_at,
                           (SELECT COUNT(*)
                              FROM partner_support_ticket_messages m
                             WHERE m.ticket_id = t.id
                               AND m.author_type = 'PARTNER'
                               AND m.read_by_admin_at IS NULL) AS unread_count
                      FROM partner_support_tickets t
                      JOIN partners p ON p.id = t.partner_id
                     WHERE {where_sql}
                     ORDER BY
                       CASE t.priority
                         WHEN 'URGENT' THEN 1 WHEN 'HIGH' THEN 2
                         WHEN 'NORMAL' THEN 3 ELSE 4
                       END,
                       t.created_at DESC, t.id DESC
                     LIMIT :limit OFFSET :offset
                    """
                ),
                {**params, "limit": page_size, "offset": (page - 1) * page_size},
            )
        )
        .mappings()
        .all()
    )
    return {
        "items": [_row_to_dict(r) for r in rows],
        "total": int(total),
        "page": page,
        "page_size": page_size,
        "pages": (int(total) + page_size - 1) // page_size,
    }


@router.get("/{ticket_id}", summary="Get a partner support ticket with its thread")
async def get_support_ticket(
    ticket_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(get_current_user),
    scope: BranchScope = Depends(get_branch_scope),
):
    # Opening the thread is the read receipt for the admin side.
    marked = await db.execute(
        text(
            "UPDATE partner_support_ticket_messages "
            "SET read_by_admin_at = NOW() "
            "WHERE ticket_id = :id AND author_type = 'PARTNER' "
            "AND read_by_admin_at IS NULL"
        ),
        {"id": ticket_id},
    )
    if marked.rowcount:
        # Do not leave a just-cleared badge in the 45-second cache.
        try:
            cache = RedisCache(await get_redis())
            await cache.redis.delete(action_counts_cache_key(scope))
        except Exception:
            pass
    row = await _ticket_row(db, ticket_id)
    if not row:
        raise HTTPException(status_code=404, detail="Support ticket not found")
    return {**_row_to_dict(row), "messages": await _messages(db, ticket_id)}


@router.post(
    "/{ticket_id}/messages",
    status_code=201,
    summary="Reply to a partner support ticket",
)
async def reply_to_support_ticket(
    ticket_id: int,
    body: SupportMessageCreate,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    ticket = await _ticket_row(db, ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Support ticket not found")

    message = body.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    admin_name = (
        await db.execute(
            text(
                "SELECT TRIM(CONCAT_WS(' ', first_name, last_name)) "
                "FROM users WHERE id = :uid"
            ),
            {"uid": current_user["sub"]},
        )
    ).scalar_one_or_none()
    row = (
        (
            await db.execute(
                text(
                    """
                    INSERT INTO partner_support_ticket_messages (
                        uuid, ticket_id, author_type, author_user_id,
                        author_name, body, created_at,
                        read_by_admin_at, read_by_partner_at
                    ) VALUES (
                        gen_random_uuid(), :ticket_id, 'ADMIN', :user_id,
                        :author_name, :body, NOW(), NOW(), NULL
                    )
                    RETURNING id, ticket_id, author_type, author_name,
                              body, created_at
                    """
                ),
                {
                    "ticket_id": ticket_id,
                    "user_id": current_user["sub"],
                    "author_name": admin_name or "WayTero Support",
                    "body": message,
                },
            )
        )
        .mappings()
        .one()
    )
    await db.execute(
        text("UPDATE partner_support_tickets SET updated_at = NOW() WHERE id = :id"),
        {"id": ticket_id},
    )
    return _message_to_dict(row)


@router.patch("/{ticket_id}", summary="Update a partner support ticket")
async def update_support_ticket(
    ticket_id: int,
    body: SupportTicketUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    existing = (
        (
            await db.execute(
                text(
                    """
                    SELECT t.id, t.status, t.ticket_number, t.subject,
                           p.user_id AS partner_user_id,
                           p.email AS partner_email,
                           COALESCE(NULLIF(p.owner_name, ''), p.business_name)
                               AS partner_name
                      FROM partner_support_tickets t
                      JOIN partners p ON p.id = t.partner_id
                     WHERE t.id = :id
                    """
                ),
                {"id": ticket_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Support ticket not found")

    next_status = (body.status or "").strip().upper() or existing["status"]
    if next_status not in _STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported status. Valid: {', '.join(sorted(_STATUSES))}",
        )
    next_priority = (body.priority or "").strip().upper()
    if next_priority and next_priority not in _PRIORITIES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported priority. Valid: {', '.join(sorted(_PRIORITIES))}",
        )

    resolved_at_sql = "NOW()" if next_status in {"RESOLVED", "CLOSED"} else "NULL"
    row = (
        (
            await db.execute(
                text(
                    f"""
                    UPDATE partner_support_tickets
                       SET status = :status,
                           priority = COALESCE(NULLIF(:priority, ''), priority),
                           resolution_notes = COALESCE(:notes, resolution_notes),
                           resolved_at = {resolved_at_sql},
                           updated_at = NOW()
                     WHERE id = :id
                    RETURNING id, ticket_number, partner_id, category, subject,
                              description, booking_reference, priority, status,
                              resolution_notes, created_at, updated_at, resolved_at
                    """
                ),
                {
                    "id": ticket_id,
                    "status": next_status,
                    "priority": next_priority,
                    "notes": body.resolution_notes,
                },
            )
        )
        .mappings()
        .one()
    )

    if next_status != existing["status"]:
        await _notify_partner_status(
            db,
            partner_user_id=existing["partner_user_id"],
            partner_email=existing["partner_email"],
            partner_name=existing["partner_name"],
            ticket={
                "id": row["id"],
                "ticket_number": row["ticket_number"],
                "subject": row["subject"],
                "status": row["status"],
            },
        )

    return {
        "id": row["id"],
        "ticket_number": row["ticket_number"],
        "partner_id": row["partner_id"],
        "category": row["category"],
        "subject": row["subject"],
        "description": row["description"],
        "booking_reference": row["booking_reference"],
        "priority": row["priority"],
        "status": row["status"],
        "resolution_notes": row["resolution_notes"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        "resolved_at": row["resolved_at"].isoformat() if row["resolved_at"] else None,
        "updated_by": current_user.get("sub"),
    }
