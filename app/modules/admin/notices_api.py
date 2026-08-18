# ============================================================
# WAYTERO — ADMIN NOTICES API
# File: app/modules/admin/notices_api.py
# Prefix: /admin/notices  (registered in api/router.py)
#
# Admin → Partner notices. Admins publish operational / policy
# notices targeted at all partners or a single partner
# (e.g. wallet insufficient balance, cab booking driver-assign
# pending, document expiry, settlement deadlines). Partners see
# them as a banner at the top of their portal and dismiss them
# per-partner (partner_notice_reads), so the same notice keeps
# working for partners who haven't seen it yet.
#
#   POST /admin/notices            create a notice
#   GET  /admin/notices            list (filter status/type/audience)
#   GET  /admin/notices/stats      counts for the page header
#   POST /admin/notices/{id}/archive   hide a notice
# ============================================================

from __future__ import annotations

import math
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import require_roles

router = APIRouter()

ADMIN_ROLES = ("ADMIN", "SUPER_ADMIN", "CCO")

NOTICE_TYPES = (
    "WALLET_LOW_BALANCE",
    "BOOKING_ACTION_REQUIRED",
    "DOCUMENT_EXPIRY",
    "SETTLEMENT",
    "POLICY_UPDATE",
    "GENERAL_ANNOUNCEMENT",
)


class NoticeCreateIn(BaseModel):
    notice_type: str = Field(..., description="One of the notice type codes")
    title: str = Field(..., min_length=3, max_length=255)
    body: Optional[str] = Field(None, max_length=4000)
    priority: str = Field("NORMAL", pattern="^(NORMAL|HIGH|URGENT)$")
    audience: str = Field("ALL_PARTNERS", pattern="^(ALL_PARTNERS|PARTNER)$")
    partner_id: Optional[int] = Field(None, gt=0)


def _row_to_item(r: Any) -> Dict[str, Any]:
    return {
        "id": int(r["id"]),
        "notice_type": r["notice_type"],
        "title": r["title"],
        "body": r["body"],
        "priority": r["priority"],
        "audience": r["audience"],
        "partner_id": int(r["partner_id"]) if r["partner_id"] is not None else None,
        "partner_name": r["partner_name"],
        "status": r["status"],
        "created_by_name": r["created_by_name"],
        "reads": int(r["reads"] or 0),
        "read_pct": (
            round(float(r["reads"] or 0) / float(r["total_partners"]) * 100, 1)
            if r["total_partners"]
            else 0.0
        ),
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
    }


@router.post(
    "", tags=["Notices"], summary="Publish a notice to all partners or one partner"
)
async def create_notice(
    payload: NoticeCreateIn,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
):
    notice_type = payload.notice_type.upper()
    if notice_type not in NOTICE_TYPES:
        raise HTTPException(
            400, f"Unknown notice_type. Allowed: {', '.join(NOTICE_TYPES)}"
        )

    if payload.audience == "PARTNER" and not payload.partner_id:
        raise HTTPException(400, "partner_id is required when audience is PARTNER")
    if payload.audience == "ALL_PARTNERS" and payload.partner_id:
        raise HTTPException(400, "partner_id must be empty for ALL_PARTNERS audience")

    if payload.audience == "PARTNER":
        exists = (
            await db.execute(
                text("SELECT 1 FROM partners WHERE id = :pid AND deleted_at IS NULL"),
                {"pid": payload.partner_id},
            )
        ).first()
        if not exists:
            raise HTTPException(404, f"Partner #{payload.partner_id} not found")

    row = (
        await db.execute(
            text(
                """
                INSERT INTO partner_notices
                    (notice_type, title, body, priority, audience, partner_id,
                     status, created_by, created_at, updated_at)
                VALUES (:t, :title, :body, :p, :aud, :pid, 'ACTIVE', :by, NOW(), NOW())
                RETURNING id
                """
            ),
            {
                "t": notice_type,
                "title": payload.title,
                "body": payload.body,
                "p": payload.priority,
                "aud": payload.audience,
                "pid": payload.partner_id,
                "by": str(current_user["sub"]),
            },
        )
    ).first()
    await db.commit()
    notice_id = int(row[0])

    # ── Real-time push: notify affected partners so the banner appears
    #    without a page refresh. Best-effort — never fails the request.
    try:
        from app.modules.notification.services import dispatch

        user_ids = (
            await db.execute(
                text(
                    "SELECT user_id FROM partners WHERE deleted_at IS NULL"
                    if payload.audience == "ALL_PARTNERS"
                    else "SELECT user_id FROM partners WHERE id = :pid AND deleted_at IS NULL"
                ),
                {"pid": payload.partner_id} if payload.audience == "PARTNER" else {},
            )
        ).all()
        for (uid,) in user_ids:
            if uid is None:
                continue
            await dispatch(
                db,
                user_id=uid,
                event_type="PARTNER_NOTICE",
                title=f"Notice: {payload.title}",
                body=payload.body,
                data={
                    "notice_id": notice_id,
                    "notice_type": notice_type,
                    "priority": payload.priority,
                },
            )
    except Exception:  # pragma: no cover — push must never break publishing
        pass

    return {"success": True, "message": "Notice published", "notice_id": notice_id}


@router.get("", tags=["Notices"], summary="List notices with filters + read stats")
async def list_notices(
    status: Optional[str] = Query(None, description="ACTIVE | ARCHIVED"),
    notice_type: Optional[str] = Query(None),
    audience: Optional[str] = Query(None, description="ALL_PARTNERS | PARTNER"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_roles(*ADMIN_ROLES)),
):
    where = ["1=1"]
    params: Dict[str, Any] = {}
    if status:
        where.append("n.status = :st")
        params["st"] = status.upper()
    if notice_type:
        where.append("n.notice_type = :nt")
        params["nt"] = notice_type.upper()
    if audience:
        where.append("n.audience = :aud")
        params["aud"] = audience.upper()
    clause = " AND ".join(where)

    total = (
        await db.execute(
            text(f"SELECT COUNT(*) FROM partner_notices n WHERE {clause}"), params
        )
    ).scalar() or 0

    rows = (
        (
            await db.execute(
                text(
                    f"""
                    SELECT n.*, p.business_name AS partner_name,
                           u.first_name || ' ' || COALESCE(u.last_name, '') AS created_by_name,
                           (SELECT COUNT(*) FROM partner_notice_reads r WHERE r.notice_id = n.id) AS reads,
                           (SELECT COUNT(*) FROM partners WHERE deleted_at IS NULL) AS total_partners
                      FROM partner_notices n
                      LEFT JOIN partners p      ON p.id = n.partner_id
                      LEFT JOIN users u         ON u.id = n.created_by
                     WHERE {clause}
                     ORDER BY n.created_at DESC
                     LIMIT :lim OFFSET :off
                    """
                ),
                {**params, "lim": page_size, "off": (page - 1) * page_size},
            )
        )
        .mappings()
        .all()
    )

    return {
        "total": int(total),
        "page": page,
        "page_size": page_size,
        "total_pages": math.ceil(int(total) / page_size) if total else 1,
        "items": [_row_to_item(r) for r in rows],
    }


@router.get("/stats", tags=["Notices"], summary="Counts for the page header")
async def notice_stats(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_roles(*ADMIN_ROLES)),
):
    rows = (
        (
            await db.execute(
                text(
                    "SELECT status, COUNT(*) AS c FROM partner_notices GROUP BY status"
                )
            )
        )
        .mappings()
        .all()
    )
    by_status = {r["status"]: int(r["c"]) for r in rows}
    return {
        "active": by_status.get("ACTIVE", 0),
        "archived": by_status.get("ARCHIVED", 0),
        "total": sum(by_status.values()),
    }


@router.post(
    "/{notice_id}/archive", tags=["Notices"], summary="Archive (hide) a notice"
)
async def archive_notice(
    notice_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_roles(*ADMIN_ROLES)),
):
    res = await db.execute(
        text(
            """
            UPDATE partner_notices
               SET status = 'ARCHIVED', updated_at = NOW()
             WHERE id = :id
            """
        ),
        {"id": notice_id},
    )
    await db.commit()
    if res.rowcount == 0:
        raise HTTPException(404, f"Notice #{notice_id} not found")
    return {"success": True, "message": "Notice archived"}


__all__ = ["router", "NOTICE_TYPES"]
