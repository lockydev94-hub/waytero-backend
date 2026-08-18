# ============================================================
# WAYTERO — ACCOUNT DELETION REQUESTS
# File: app/modules/admin/account_deletion_api.py
#
#   POST   /public/account-deletion-requests        — public form (website)
#   POST   /customers/me/account-deletion-request   — logged-in customer
#   GET    /admin/account-deletion/requests         — admin inbox (paginated)
#   GET    /admin/account-deletion/requests/counts  — sidebar badge
#   POST   /admin/account-deletion/requests/{id}/review
#          APPROVED → soft-delete + anonymise the account
#          REJECTED → mark rejected with a note
#
# Deletion semantics (GDPR-lite, compliance-safe):
#   * user.status → INACTIVE, is_active = FALSE, deleted_at = NOW()
#   * all sessions deactivated (tokens die immediately)
#   * personal identifiers anonymised (name/email; mobile made unique-safe)
#   * customer profile anonymised (no PII left for marketing/UI)
#   * booking + wallet + ledger rows are KEPT (legal/financial retention)
#     but no longer joinable to a live profile.
# ============================================================

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user, require_roles
from app.modules.auth.repositories import SessionRepository
from app.modules.customer.models import Customer

logger = logging.getLogger("waytero.account_deletion")

public_router = APIRouter()
customer_router = APIRouter()
admin_router = APIRouter()

MOBILE_RE = re.compile(r"^[6-9]\d{9}$")


# ════════════════════════════════════════════════════════════════
# SCHEMAS
# ════════════════════════════════════════════════════════════════


class PublicDeletionRequestIn(BaseModel):
    mobile: str = Field(..., description="10-digit Indian mobile number")
    email: Optional[str] = Field(None, max_length=255)
    full_name: Optional[str] = Field(None, max_length=255)
    reason: str = Field(..., min_length=10, max_length=2000)


class CustomerDeletionRequestIn(BaseModel):
    reason: str = Field(..., min_length=10, max_length=2000)


class ReviewDeletionIn(BaseModel):
    decision: str = Field(..., description="APPROVED | REJECTED")
    note: Optional[str] = Field(None, max_length=1000)


# ════════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════════


async def _find_customer_by_identity(
    db: AsyncSession, *, user_id: Optional[UUID], mobile: str
) -> Optional[Customer]:
    """Resolve the customer profile from the request identity — by user UUID
    first (logged-in), then by mobile (public form / ownership check)."""
    from app.modules.auth.models.user import User

    if user_id is not None:
        c = (
            await db.execute(select(Customer).where(Customer.user_id == user_id))
        ).scalar_one_or_none()
        if c:
            return c
    c = (
        await db.execute(
            select(Customer)
            .join(User, User.id == Customer.user_id)
            .where(User.mobile_number == mobile, User.deleted_at.is_(None))
            .order_by(Customer.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return c


async def _anonymise_account(
    db: AsyncSession, *, customer: Customer, request_id: int
) -> Dict[str, Any]:
    """Soft-delete + anonymise a user + customer profile.

    Sessions are killed so existing tokens stop working, identifiers are
    scrubbed, and the rows are flagged deleted. Financial/booking rows are
    retained for legal retention but no longer resolve to a live profile.
    """
    from app.modules.auth.models.user import User

    user = (
        await db.execute(select(User).where(User.id == customer.user_id))
    ).scalar_one_or_none()
    if user is None:
        return {"user_id": None, "anonymised": False, "note": "No user row found"}

    # Kill every session — tokens die immediately.
    try:
        await SessionRepository(db).deactivate_all_user_sessions(user.id)
    except Exception as exc:  # pragma: no cover
        logger.warning("account_deletion.sessions_failed err=%s", exc)

    # Unique-safe mobile anonymisation (mobile_number is UNIQUE NOT NULL).
    anon_code = user.user_code or f"u{uuid4().hex[:10]}"
    anon_mobile = f"DELETED-{anon_code}"[:20]
    anon_email = f"deleted-{uuid4().hex[:12]}@waytero.in"

    await db.execute(
        text(
            """
            UPDATE users
               SET first_name = '[Deleted]',
                   last_name  = NULL,
                   mobile_number = :m,
                   email         = :e,
                   is_active     = FALSE,
                   status        = 'INACTIVE',
                   deleted_at    = NOW(),
                   updated_at    = NOW()
             WHERE id = :uid
            """
        ),
        {"m": anon_mobile, "e": anon_email, "uid": user.id},
    )
    await db.execute(
        text(
            """
            UPDATE customers
               SET first_name = '[Deleted]',
                   last_name  = NULL,
                   date_of_birth = NULL,
                   is_active  = FALSE,
                   deleted_at = NOW(),
                   updated_at = NOW()
             WHERE id = :cid
            """
        ),
        {"cid": customer.id},
    )
    await db.execute(
        text(
            """
            INSERT INTO booking_timelines
                (master_booking_id, event_type, event_description, event_timestamp)
            SELECT id, 'ACCOUNT_DELETED',
                   'Customer account deleted (request #' || :rid || '). Profile anonymised.',
                   NOW()
            FROM master_bookings WHERE customer_id = :cid LIMIT 1
            """
        ),
        {"rid": str(request_id), "cid": customer.id},
    )
    return {"user_id": str(user.id), "anonymised": True, "note": "Account anonymised"}


# ════════════════════════════════════════════════════════════════
# PUBLIC — website form (logged-in or guest)
# ════════════════════════════════════════════════════════════════


@public_router.post(
    "/account-deletion-requests",
    tags=["Account Deletion"],
    summary="Submit an account-deletion request (public website form).",
)
async def submit_public_deletion_request(
    payload: PublicDeletionRequestIn,
    db: AsyncSession = Depends(get_db),
):
    mobile = payload.mobile.strip()
    if not MOBILE_RE.match(mobile):
        raise HTTPException(422, "Enter a valid 10-digit Indian mobile number")

    # Link to an existing account when the number is registered.
    cust = await _find_customer_by_identity(db, user_id=None, mobile=mobile)

    dupe = (
        await db.execute(
            text(
                "SELECT id FROM account_deletion_requests "
                "WHERE mobile = :m AND status = 'PENDING' LIMIT 1"
            ),
            {"m": mobile},
        )
    ).first()
    if dupe:
        raise HTTPException(
            409,
            f"A deletion request is already pending for this number (request #{dupe[0]}). "
            "Our team will review it within 30 days.",
        )

    row = (
        await db.execute(
            text(
                """
                INSERT INTO account_deletion_requests
                    (customer_user_id, customer_id, mobile, email, full_name,
                     reason, request_source, status, requested_at)
                VALUES (:uid, :cid, :m, :e, :name, :reason, 'PUBLIC', 'PENDING', NOW())
                RETURNING id
                """
            ),
            {
                "uid": str(cust.user_id) if cust else None,
                "cid": cust.id if cust else None,
                "m": mobile,
                "e": (payload.email or "").strip() or None,
                "name": (payload.full_name or "").strip() or None,
                "reason": payload.reason.strip(),
            },
        )
    ).first()
    await db.commit()
    return {
        "success": True,
        "message": (
            "Your account deletion request has been received. "
            "Our team will review it and complete deletion within 30 days."
        ),
        "request_id": int(row[0]),
        "account_found": cust is not None,
    }


# ════════════════════════════════════════════════════════════════
# CUSTOMER — logged-in submit (auto-fetches profile details)
# ════════════════════════════════════════════════════════════════


@customer_router.post(
    "/me/account-deletion-request",
    tags=["Account Deletion"],
    summary="Submit an account-deletion request as the logged-in customer.",
)
async def submit_customer_deletion_request(
    payload: CustomerDeletionRequestIn,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    cust = (
        await db.execute(
            select(Customer).where(Customer.user_id == UUID(current_user["sub"]))
        )
    ).scalar_one_or_none()
    if not cust:
        raise HTTPException(404, "Customer profile not found.")

    from app.modules.auth.models.user import User

    user = (
        await db.execute(select(User).where(User.id == cust.user_id))
    ).scalar_one_or_none()
    mobile = user.mobile_number if user else ""

    dupe = (
        await db.execute(
            text(
                "SELECT id FROM account_deletion_requests "
                "WHERE customer_user_id = :uid AND status = 'PENDING' LIMIT 1"
            ),
            {"uid": str(cust.user_id)},
        )
    ).first()
    if dupe:
        raise HTTPException(
            409,
            f"A deletion request is already pending for your account (request #{dupe[0]}).",
        )

    row = (
        await db.execute(
            text(
                """
                INSERT INTO account_deletion_requests
                    (customer_user_id, customer_id, mobile, email, full_name,
                     reason, request_source, status, requested_at)
                VALUES (:uid, :cid, :m, :e, :name, :reason, 'LOGGED_IN', 'PENDING', NOW())
                RETURNING id
                """
            ),
            {
                "uid": str(cust.user_id),
                "cid": cust.id,
                "m": mobile,
                "e": user.email if user else None,
                "name": " ".join(filter(None, [cust.first_name, cust.last_name]))
                or None,
                "reason": payload.reason.strip(),
            },
        )
    ).first()
    await db.commit()
    return {
        "success": True,
        "message": (
            "Your account deletion request has been received. "
            "Our team will review it and complete deletion within 30 days."
        ),
        "request_id": int(row[0]),
    }


# ════════════════════════════════════════════════════════════════
# ADMIN — inbox, counts, review
# ════════════════════════════════════════════════════════════════


@admin_router.get(
    "/requests",
    tags=["Account Deletion"],
    summary="Paginated inbox of account-deletion requests.",
)
async def list_deletion_requests(
    status: Optional[str] = Query(None, description="PENDING | APPROVED | REJECTED"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_roles("ADMIN", "SUPER_ADMIN", "CCO")),
):
    q = "SELECT * FROM account_deletion_requests WHERE 1=1"
    params: Dict[str, Any] = {}
    if status:
        q += " AND status = :s"
        params["s"] = status.upper()
    q += " ORDER BY requested_at DESC"

    total = (
        await db.execute(text(f"SELECT COUNT(*) FROM ({q}) sub"), params)
    ).scalar() or 0
    rows = (
        (
            await db.execute(
                text(q + " LIMIT :lim OFFSET :off"),
                {**params, "lim": page_size, "off": (page - 1) * page_size},
            )
        )
        .mappings()
        .all()
    )
    items = [
        {
            "id": int(r["id"]),
            "customer_user_id": (
                str(r["customer_user_id"]) if r["customer_user_id"] else None
            ),
            "customer_id": r["customer_id"],
            "mobile": r["mobile"],
            "email": r["email"],
            "full_name": r["full_name"],
            "reason": r["reason"],
            "request_source": r["request_source"],
            "status": r["status"],
            "requested_at": (
                r["requested_at"].isoformat() if r["requested_at"] else None
            ),
            "reviewed_by_user_id": (
                str(r["reviewed_by_user_id"]) if r["reviewed_by_user_id"] else None
            ),
            "reviewed_at": r["reviewed_at"].isoformat() if r["reviewed_at"] else None,
            "review_note": r["review_note"],
        }
        for r in rows
    ]
    return {
        "total": int(total),
        "page": page,
        "page_size": page_size,
        "total_pages": (int(total) + page_size - 1) // page_size if total else 1,
        "items": items,
    }


@admin_router.get(
    "/requests/counts",
    tags=["Account Deletion"],
    summary="Counts by status for the sidebar badge.",
)
async def deletion_request_counts(
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    rows = (
        (
            await db.execute(
                text(
                    "SELECT status, COUNT(*) AS c FROM account_deletion_requests GROUP BY status"
                )
            )
        )
        .mappings()
        .all()
    )
    by_status = {r["status"]: int(r["c"]) for r in rows}
    return {
        "pending": by_status.get("PENDING", 0),
        "approved": by_status.get("APPROVED", 0),
        "rejected": by_status.get("REJECTED", 0),
        "total": sum(by_status.values()),
    }


@admin_router.post(
    "/requests/{request_id}/review",
    tags=["Account Deletion"],
    summary="Approve (initiate deletion) or reject an account-deletion request.",
)
async def review_deletion_request(
    request_id: int,
    payload: ReviewDeletionIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_roles("ADMIN", "SUPER_ADMIN", "CCO")),
):
    decision = (payload.decision or "").upper()
    if decision not in {"APPROVED", "REJECTED"}:
        raise HTTPException(400, "decision must be APPROVED or REJECTED")

    req = (
        (
            await db.execute(
                text(
                    """
                    SELECT id, customer_user_id, customer_id, mobile, status
                    FROM account_deletion_requests WHERE id = :id
                    """
                ),
                {"id": request_id},
            )
        )
        .mappings()
        .first()
    )
    if not req:
        raise HTTPException(404, "Deletion request not found")
    if req["status"] != "PENDING":
        raise HTTPException(400, f"Request is already '{req['status']}'.")

    actor_uid = current_user["sub"]
    result: Dict[str, Any] = {}

    if decision == "REJECTED":
        await db.execute(
            text(
                """
                UPDATE account_deletion_requests
                   SET status = 'REJECTED', reviewed_by_user_id = :uid,
                       reviewed_at = NOW(), review_note = :note
                 WHERE id = :id
                """
            ),
            {"uid": actor_uid, "note": payload.note, "id": request_id},
        )
        result = {"deleted": False, "note": payload.note}
    else:
        # APPROVED — initiate deletion.
        customer = await _find_customer_by_identity(
            db,
            user_id=(
                UUID(str(req["customer_user_id"])) if req["customer_user_id"] else None
            ),
            mobile=req["mobile"],
        )
        if customer is None:
            result = {
                "deleted": False,
                "note": "No matching live account found — nothing to delete.",
            }
        else:
            result = await _anonymise_account(
                db, customer=customer, request_id=request_id
            )

        await db.execute(
            text(
                """
                UPDATE account_deletion_requests
                   SET status = 'APPROVED', reviewed_by_user_id = :uid,
                       reviewed_at = NOW(), review_note = :note
                 WHERE id = :id
                """
            ),
            {
                "uid": actor_uid,
                "note": payload.note or result.get("note"),
                "id": request_id,
            },
        )

    try:
        from app.modules.admin.services.audit_logger import AuditLogger

        await AuditLogger.log_event(
            db,
            module_name=AuditLogger.MODULE_AUTH,
            action_type=f"ACCOUNT_DELETION_{decision}",
            user_id=actor_uid,
            entity_name="account_deletion_request",
            entity_id=request_id,
            old_values={"status": "PENDING"},
            new_values={"status": decision, **result},
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("account_deletion.audit_failed err=%s", exc)

    await db.commit()
    return {
        "success": True,
        "message": (
            "Deletion initiated — account anonymised and sessions deactivated."
            if decision == "APPROVED"
            else "Request rejected."
        ),
        "request_id": request_id,
        "status": decision,
        **result,
    }
