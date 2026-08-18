# ============================================================
# WAYTERO — ADMIN LEADS API
# File: app/modules/admin/leads_api.py
# Doc Ref: Website Lead Capture §3 — Admin API
# Prefix: /admin/leads  (registered in api/router.py)
#
# Admin review surfaces for website-submitted leads:
#   GET   /admin/leads/partner-applications           — list applications
#   GET   /admin/leads/partner-applications/stats     — counts by status
#   PATCH /admin/leads/partner-applications/{id}      — status + admin notes
#   GET   /admin/leads/contact-messages               — list messages
#   GET   /admin/leads/contact-messages/stats         — read / unread counts
#   PATCH /admin/leads/contact-messages/{id}/read     — mark read / unread
# ============================================================

from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import text as _text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import require_roles
from app.core.exceptions import ResourceNotFoundException, ValidationException

router = APIRouter()

APPLICATION_STATUSES = ("NEW", "CONTACTED", "CONVERTED", "REJECTED")


class PartnerApplicationUpdate(BaseModel):
    status: Optional[str] = Field(
        None, description="NEW / CONTACTED / CONVERTED / REJECTED"
    )
    admin_notes: Optional[str] = Field(None, max_length=2000)

    def validate_all(self) -> None:
        if self.status is not None and self.status.upper() not in APPLICATION_STATUSES:
            raise ValidationException(
                f"status must be one of {', '.join(APPLICATION_STATUSES)}"
            )


# ── Partner applications ─────────────────────────────────────────


@router.get(
    "/partner-applications",
    tags=["Admin Leads"],
    summary="List website partner applications",
)
async def list_partner_applications(
    status: Optional[str] = Query(None),
    business_type: Optional[str] = Query(None),
    search: Optional[str] = Query(
        None, description="Search business name / contact person / mobile / email"
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(require_roles("SUPER_ADMIN", "ADMIN", "CCO")),
    db: AsyncSession = Depends(get_db),
):
    filters = ["1 = 1"]
    params: dict = {}

    if status:
        filters.append("pa.status = :status")
        params["status"] = status.upper()
    if business_type:
        filters.append("pa.business_type = :business_type")
        params["business_type"] = business_type
    if search:
        filters.append(
            "(pa.business_name ILIKE :search OR pa.contact_person ILIKE :search "
            "OR pa.mobile ILIKE :search OR pa.email ILIKE :search)"
        )
        params["search"] = f"%{search.strip()}%"

    where = " AND ".join(filters)
    offset = (page - 1) * page_size

    total = (
        await db.execute(
            _text(f"SELECT COUNT(*) AS c FROM partner_applications pa WHERE {where}"),
            params,
        )
    ).scalar()

    rows = (
        (
            await db.execute(
                _text(
                    f"""
                SELECT pa.id, pa.business_name, pa.business_type, pa.contact_person,
                       pa.mobile, pa.email, pa.city, pa.details, pa.status,
                       pa.admin_notes, pa.reviewed_at, pa.created_at,
                       u.mobile_number AS reviewer_mobile
                FROM partner_applications pa
                LEFT JOIN users u ON u.id = pa.reviewed_by
                WHERE {where}
                ORDER BY pa.created_at DESC
                LIMIT :limit OFFSET :offset
                """
                ),
                {**params, "limit": page_size, "offset": offset},
            )
        )
        .mappings()
        .all()
    )

    return {
        "items": [
            {
                "id": r["id"],
                "business_name": r["business_name"],
                "business_type": r["business_type"],
                "contact_person": r["contact_person"],
                "mobile": r["mobile"],
                "email": r["email"],
                "city": r["city"],
                "details": r["details"],
                "status": r["status"],
                "admin_notes": r["admin_notes"],
                "reviewed_at": (
                    r["reviewed_at"].isoformat() if r["reviewed_at"] else None
                ),
                "created_at": r["created_at"].isoformat(),
                "reviewer_mobile": r["reviewer_mobile"],
            }
            for r in rows
        ],
        "total": int(total or 0),
        "page": page,
        "page_size": page_size,
        "total_pages": (int(total or 0) + page_size - 1) // page_size,
    }


@router.get(
    "/partner-applications/stats",
    tags=["Admin Leads"],
    summary="Count partner applications by status",
)
async def partner_application_stats(
    current_user: dict = Depends(require_roles("SUPER_ADMIN", "ADMIN", "CCO")),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        (
            await db.execute(
                _text(
                    "SELECT status, COUNT(*) AS c FROM partner_applications GROUP BY status"
                )
            )
        )
        .mappings()
        .all()
    )
    stats = {r["status"]: int(r["c"]) for r in rows}
    return {
        "new": stats.get("NEW", 0),
        "contacted": stats.get("CONTACTED", 0),
        "converted": stats.get("CONVERTED", 0),
        "rejected": stats.get("REJECTED", 0),
        "total": sum(stats.values()),
    }


@router.patch(
    "/partner-applications/{application_id}",
    tags=["Admin Leads"],
    summary="Update partner application status / notes",
)
async def update_partner_application(
    application_id: int,
    body: PartnerApplicationUpdate,
    current_user: dict = Depends(require_roles("SUPER_ADMIN", "ADMIN", "CCO")),
    db: AsyncSession = Depends(get_db),
):
    body.validate_all()

    existing = (
        await db.execute(
            _text("SELECT id FROM partner_applications WHERE id = :id FOR UPDATE"),
            {"id": application_id},
        )
    ).first()
    if not existing:
        raise ResourceNotFoundException("Partner application", application_id)

    sets: list[str] = []
    params: dict = {"id": application_id}

    if body.status is not None:
        sets.append("status = :status")
        params["status"] = body.status.upper()
    if body.admin_notes is not None:
        sets.append("admin_notes = :admin_notes")
        params["admin_notes"] = body.admin_notes.strip()

    # `reviewed` only transitions to a terminal/active review state
    if body.status is not None:
        sets.append("reviewed_by = :reviewed_by")
        sets.append("reviewed_at = now()")
        params["reviewed_by"] = current_user["sub"]

    if sets:
        sets.append("updated_at = now()")
        await db.execute(
            _text(f"UPDATE partner_applications SET {', '.join(sets)} WHERE id = :id"),
            params,
        )

    row = (
        (
            await db.execute(
                _text(
                    "SELECT id, status, admin_notes, updated_at FROM partner_applications WHERE id = :id"
                ),
                {"id": application_id},
            )
        )
        .mappings()
        .one()
    )

    return {
        "id": row["id"],
        "status": row["status"],
        "admin_notes": row["admin_notes"],
        "updated_at": row["updated_at"].isoformat(),
    }


# ── Contact messages ─────────────────────────────────────────────


@router.get(
    "/contact-messages",
    tags=["Admin Leads"],
    summary="List website contact messages",
)
async def list_contact_messages(
    read: Optional[bool] = Query(None, description="Filter by read state"),
    search: Optional[str] = Query(
        None, description="Search name / email / subject / message"
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(require_roles("SUPER_ADMIN", "ADMIN", "CCO")),
    db: AsyncSession = Depends(get_db),
):
    filters = ["1 = 1"]
    params: dict = {}

    if read is not None:
        filters.append("cm.is_read = :read")
        params["read"] = read
    if search:
        filters.append(
            "(cm.name ILIKE :search OR cm.email ILIKE :search OR cm.subject ILIKE :search "
            "OR cm.message ILIKE :search)"
        )
        params["search"] = f"%{search.strip()}%"

    where = " AND ".join(filters)
    offset = (page - 1) * page_size

    total = (
        await db.execute(
            _text(f"SELECT COUNT(*) AS c FROM contact_messages cm WHERE {where}"),
            params,
        )
    ).scalar()

    rows = (
        (
            await db.execute(
                _text(
                    f"""
                SELECT cm.id, cm.name, cm.email, cm.mobile, cm.subject, cm.message,
                       cm.is_read, cm.created_at
                FROM contact_messages cm
                WHERE {where}
                ORDER BY cm.created_at DESC
                LIMIT :limit OFFSET :offset
                """
                ),
                {**params, "limit": page_size, "offset": offset},
            )
        )
        .mappings()
        .all()
    )

    return {
        "items": [
            {
                "id": r["id"],
                "name": r["name"],
                "email": r["email"],
                "mobile": r["mobile"],
                "subject": r["subject"],
                "message": r["message"],
                "is_read": r["is_read"],
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
        ],
        "total": int(total or 0),
        "page": page,
        "page_size": page_size,
        "total_pages": (int(total or 0) + page_size - 1) // page_size,
    }


@router.get(
    "/contact-messages/stats",
    tags=["Admin Leads"],
    summary="Read / unread counts for contact messages",
)
async def contact_message_stats(
    current_user: dict = Depends(require_roles("SUPER_ADMIN", "ADMIN", "CCO")),
    db: AsyncSession = Depends(get_db),
):
    unread = (
        await db.execute(
            _text("SELECT COUNT(*) AS c FROM contact_messages WHERE is_read = false")
        )
    ).scalar()
    total = (
        await db.execute(_text("SELECT COUNT(*) AS c FROM contact_messages"))
    ).scalar()
    return {"unread": int(unread or 0), "total": int(total or 0)}


@router.patch(
    "/contact-messages/{message_id}/read",
    tags=["Admin Leads"],
    summary="Mark contact message read / unread",
)
async def mark_contact_message_read(
    message_id: int,
    read: bool = Query(True),
    current_user: dict = Depends(require_roles("SUPER_ADMIN", "ADMIN", "CCO")),
    db: AsyncSession = Depends(get_db),
):
    existing = (
        await db.execute(
            _text("SELECT id FROM contact_messages WHERE id = :id"),
            {"id": message_id},
        )
    ).first()
    if not existing:
        raise ResourceNotFoundException("Contact message", message_id)

    await db.execute(
        _text("UPDATE contact_messages SET is_read = :read WHERE id = :id"),
        {"read": read, "id": message_id},
    )
    return {"id": message_id, "is_read": read}
