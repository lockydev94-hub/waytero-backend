# ============================================================
# WAYTERO — ADMIN EMAIL API
# File: app/modules/admin/email_api.py
# Prefix: /admin/email
#
#   GET  /admin/email/settings              — SMTP config (masked)
#   PUT  /admin/email/settings              — save SMTP config
#   POST /admin/email/test                  — send a test email
#   GET  /admin/email/events                — supported event types
#   GET  /admin/email/logs                  — paginated email log
#   GET  /admin/email/logs/stats            — counts by status
#   GET  /admin/email/logs/{id}             — single log detail
#   POST /admin/email/logs/{id}/resend      — re-send a failed email
#
# Every outbound attempt is recorded in `email_logs` by
# app.infrastructure.email — nothing is sent until EMAIL_ENABLED
# and SMTP credentials exist in system_configurations (Settings →
# Email in the admin portal).
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
from app.infrastructure.email import (
    EVENT_LABELS,
    public_config,
    resend_email,
    save_config,
    send_test_email,
)

router = APIRouter()

ADMIN_ROLES = ("SUPER_ADMIN", "ADMIN")


class EmailSettingsIn(BaseModel):
    enabled: Optional[bool] = None
    host: Optional[str] = Field(None, max_length=255)
    port: Optional[int] = Field(None, ge=1, le=65535)
    user: Optional[str] = Field(None, max_length=255)
    password: Optional[str] = Field(None, max_length=500)
    from_email: Optional[str] = Field(None, max_length=255)
    from_name: Optional[str] = Field(None, max_length=255)
    use_tls: Optional[bool] = None


class TestEmailIn(BaseModel):
    to_email: str = Field(..., max_length=255)


@router.get(
    "/settings",
    tags=["Email"],
    summary="SMTP configuration (password masked) + platform branding used in emails",
)
async def get_email_settings(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_roles(*ADMIN_ROLES)),
):
    return await public_config(db)


@router.put(
    "/settings",
    tags=["Email"],
    summary="Save SMTP configuration",
)
async def update_email_settings(
    payload: EmailSettingsIn,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_roles(*ADMIN_ROLES)),
):
    values = payload.model_dump(exclude_none=True)
    if not values:
        raise HTTPException(400, "Nothing to update")
    return await save_config(db, values)


@router.post(
    "/test",
    tags=["Email"],
    summary="Send a test email to verify the SMTP configuration",
)
async def send_test(
    payload: TestEmailIn,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_roles(*ADMIN_ROLES)),
):
    try:
        return await send_test_email(db, payload.to_email.strip())
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))


@router.get(
    "/events",
    tags=["Email"],
    summary="Supported transactional email event types",
)
async def email_events(
    _: dict = Depends(require_roles(*ADMIN_ROLES)),
):
    return {"events": [{"code": k, "label": v} for k, v in EVENT_LABELS.items()]}


# ── Logs ──────────────────────────────────────────────────────────


def _row_to_item(r: Any) -> Dict[str, Any]:
    return {
        "id": int(r["id"]),
        "event_type": r["event_type"],
        "event_label": EVENT_LABELS.get(r["event_type"], r["event_type"]),
        "recipient": r["recipient"],
        "recipient_name": r["recipient_name"],
        "subject": r["subject"],
        "status": r["status"],
        "error_message": r["error_message"],
        "attempt_count": int(r["attempt_count"] or 0),
        "related_type": r["related_type"],
        "related_id": str(r["related_id"]) if r["related_id"] is not None else None,
        "sent_at": r["sent_at"].isoformat() if r["sent_at"] else None,
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
    }


@router.get(
    "/logs",
    tags=["Email"],
    summary="Paginated email log with status / event / recipient filters",
)
async def list_email_logs(
    status: Optional[str] = Query(None, description="SENT | FAILED"),
    event_type: Optional[str] = Query(None),
    search: Optional[str] = Query(None, description="Search recipient / subject"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_roles(*ADMIN_ROLES)),
):
    where = ["1=1"]
    params: Dict[str, Any] = {}
    if status:
        where.append("status = :st")
        params["st"] = status.upper()
    if event_type:
        where.append("event_type = :evt")
        params["evt"] = event_type
    if search and search.strip():
        where.append(
            "(recipient ILIKE :s OR subject ILIKE :s OR recipient_name ILIKE :s)"
        )
        params["s"] = f"%{search.strip()}%"
    clause = " AND ".join(where)

    total = (
        await db.execute(
            text(f"SELECT COUNT(*) FROM email_logs WHERE {clause}"), params
        )
    ).scalar() or 0
    rows = (
        (
            await db.execute(
                text(
                    f"SELECT * FROM email_logs WHERE {clause} "
                    "ORDER BY created_at DESC LIMIT :lim OFFSET :off"
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


@router.get(
    "/logs/stats",
    tags=["Email"],
    summary="Counts by status for the sidebar badges",
)
async def email_log_stats(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_roles(*ADMIN_ROLES)),
):
    rows = (
        (
            await db.execute(
                text("SELECT status, COUNT(*) AS c FROM email_logs GROUP BY status")
            )
        )
        .mappings()
        .all()
    )
    by_status = {r["status"]: int(r["c"]) for r in rows}
    return {
        "sent": by_status.get("SENT", 0),
        "failed": by_status.get("FAILED", 0),
        "total": sum(by_status.values()),
    }


@router.get(
    "/logs/{log_id}",
    tags=["Email"],
    summary="Full detail of one email log (including render payload)",
)
async def get_email_log(
    log_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_roles(*ADMIN_ROLES)),
):
    row = (
        (
            await db.execute(
                text("SELECT * FROM email_logs WHERE id = :id"), {"id": log_id}
            )
        )
        .mappings()
        .first()
    )
    if not row:
        raise HTTPException(404, f"Email log #{log_id} not found")
    return {**_row_to_item(row), "payload": row["payload"]}


@router.post(
    "/logs/{log_id}/resend",
    tags=["Email"],
    summary="Re-send an email (failed or wrong address) from its stored payload",
)
async def resend_email_log(
    log_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_roles(*ADMIN_ROLES)),
):
    try:
        return await resend_email(db, log_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))
