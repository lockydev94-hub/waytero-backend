# ============================================================
# WAYTERO — PARTNER NOTICES API
# File: app/modules/partner/notices_api.py
# Prefix: /partners/me/notices  (registered in api/router.py)
#
# Partners fetch the notices the admin published (all-partner or
# targeted at them) and dismiss them per-partner. A dismissed
# notice stays dismissed forever for that partner.
#
#   GET  /partners/me/notices            active notices + read state
#   POST /partners/me/notices/{id}/read  mark this notice as read
# ============================================================

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user

router = APIRouter()


async def _resolve_partner_id(db: AsyncSession, user_id: str) -> Optional[int]:
    row = (
        await db.execute(
            text("SELECT id FROM partners WHERE user_id = :uid AND deleted_at IS NULL"),
            {"uid": user_id},
        )
    ).first()
    return int(row[0]) if row else None


def _row_to_item(r: Any) -> Dict[str, Any]:
    return {
        "id": int(r["id"]),
        "notice_type": r["notice_type"],
        "title": r["title"],
        "body": r["body"],
        "priority": r["priority"],
        "read": bool(r["read_at"]),
        "read_at": r["read_at"].isoformat() if r["read_at"] else None,
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
    }


@router.get(
    "/me/notices",
    tags=["Partner Notices"],
    summary="Active admin notices for this partner (all-partner + targeted)",
)
async def list_my_notices(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    partner_id = await _resolve_partner_id(db, current_user["sub"])
    if partner_id is None:
        return {"items": [], "unread": 0}

    rows = (
        (
            await db.execute(
                text(
                    """
                    SELECT n.*, r.read_at
                      FROM partner_notices n
                      LEFT JOIN partner_notice_reads r
                             ON r.notice_id = n.id AND r.partner_id = :pid
                     WHERE n.status = 'ACTIVE'
                       AND (n.audience = 'ALL_PARTNERS' OR n.partner_id = :pid)
                     ORDER BY n.priority = 'URGENT' DESC,
                              n.priority = 'HIGH' DESC,
                              n.created_at DESC
                    """
                ),
                {"pid": partner_id},
            )
        )
        .mappings()
        .all()
    )
    items = [_row_to_item(r) for r in rows]
    return {
        "items": items,
        "unread": sum(1 for i in items if not i["read"]),
    }


@router.post(
    "/me/notices/{notice_id}/read",
    tags=["Partner Notices"],
    summary="Mark a notice as read for this partner (dismisses the banner)",
)
async def mark_notice_read(
    notice_id: int,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    partner_id = await _resolve_partner_id(db, current_user["sub"])
    if partner_id is None:
        raise HTTPException(404, "Partner profile not found")

    await db.execute(
        text(
            """
            INSERT INTO partner_notice_reads (notice_id, partner_id, read_at)
            VALUES (:nid, :pid, NOW())
            ON CONFLICT (notice_id, partner_id) DO NOTHING
            """
        ),
        {"nid": notice_id, "pid": partner_id},
    )
    await db.commit()
    return {"success": True, "message": "Notice marked as read"}


__all__ = ["router"]
