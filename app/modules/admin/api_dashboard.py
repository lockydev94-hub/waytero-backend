# ============================================================
# WAYTERO — ADMIN DASHBOARD & ANALYTICS API ROUTER
# File: app/modules/admin/api_dashboard.py
# Doc Ref:
#   Admin API §3  — Dashboard KPIs   → GET /admin/dashboard
#   Admin API §4  — System Summary   → GET /admin/analytics
#   Admin API §5  — User Management
#   Admin API §8  — Partner Management
#   Admin API §9  — Driver Management
#   Admin API §10 — Vehicle Management
#   Admin API §11 — Booking Operations
#   Admin API §16 — Settlement Management
#   Admin API §22 — Notifications Broadcast
#   Admin API §23 — Reports
#   Admin API §24 — Audit Logs
# Prefix: /admin  (registered in api/router.py)
# All routes: async, AsyncSession, SUPER_ADMIN scope
# ============================================================

from typing import Optional, Any, List
from datetime import date as _date, datetime as _datetime, timedelta
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    UploadFile,
    File,
    Form,
    Request,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import require_admin, require_roles
from app.modules.admin.services.dashboard import AdminDashboardService
from app.modules.admin.services.audit_logger import AuditLogger
from app.modules.admin.schemas.dashboard import (
    DashboardStats,
    SystemSummary,
    CityPerformanceItem,
    RevenueTrendItem,
    RecentActivityItem,
    AdminUserListResponse,
    AdminUserOut,
    UserStatusUpdate,
    AdminPartnerListResponse,
    PartnerActionRequest,
    AdminDriverListResponse,
    AdminDriverCreate,
    AdminDriverDocumentCreate,
    DriverActionRequest,
    AdminVehicleListResponse,
    VehicleActionRequest,
    AdminSettlementListResponse,
    BroadcastRequest,
    BroadcastResponse,
    RevenueReport,
    BookingReport,
    PartnerReport,
    SettlementReport,
    AuditLogListResponse,
    AuditLogSummary,
    AdminActionResponse,
)

# ── Section-scoped imports (kept aliased at module level so the big
#    section blocks below stay self-contained) ──────────────────────────────
import hashlib as _hashlib
import httpx as _httpx
import random as _random
import time as _time
import uuid as _uuid

from pydantic import (
    BaseModel as _BM2,
    BaseModel as _BM3,
    BaseModel as _BM_svc_v2,
    BaseModel as _BaseModel,
    BaseModel as _VBM,
    Field as _Field,
    Field as _VField,
)
from typing import Optional as _Optional
from typing import Optional as _Opt2
from typing import Optional as _VOpt

from sqlalchemy import (
    select as _select,
    select as _vsel,
    text as _text,
    text as _vtext,
    update as _update,
)

from app.modules.admin.models import ApiIntegration as _ApiIntegration
from app.modules.admin.schemas.dashboard import (
    StaffUserCreate,
    StaffUserUpdate,
    StaffUserOut,
    StaffUserListResponse,
    ResetPasswordRequest,
    StaffIdCardGenerateRequest,
    StaffProfileCreate,
    STAFF_DOC_TYPES,
)
from app.modules.auth.models.user import User as _User
from app.modules.partner.models import (
    Partner as _Partner,
    PartnerService as _PartnerSvc,
)
from app.modules.vehicle.models import (
    Vehicle as _Vehicle,
    VehicleDocument as _VehicleDoc,
    VehiclePricingRule as _VPricingRule,
    VehicleCategory as _VehicleCat,
    VehicleAvailability as _VehicleAvail,
    DefaultVehiclePricingRule as _DefaultPricing,
)

try:
    from app.modules.vehicle.models import (
        VehiclePhotoUpload as _VPhoto,
        VehicleVerificationAssignment as _VVA,
    )
except ImportError:
    _VPhoto = None
    _VVA = None

router = APIRouter()


# ════════════════════════════════════════════════════════════════
#  §3  DASHBOARD — live operational KPIs
# ════════════════════════════════════════════════════════════════


@router.get(
    "/dashboard",
    response_model=DashboardStats,
    tags=["Admin – Dashboard"],
    summary="Today's operational KPIs",
    description=(
        "Returns today's bookings, active trips, active partners/drivers, "
        "today's revenue and pending settlements. "
        "Refresh every 30 s on the frontend."
    ),
)
async def get_dashboard(db: AsyncSession = Depends(get_db)):
    data = await AdminDashboardService.get_dashboard(db)
    return DashboardStats(**data)


# ════════════════════════════════════════════════════════════════
#  §4  ANALYTICS — platform summary totals
# ════════════════════════════════════════════════════════════════


@router.get(
    "/analytics",
    response_model=SystemSummary,
    tags=["Admin – Dashboard"],
    summary="Platform-wide summary totals",
    description=(
        "Returns lifetime totals: customers, partners, drivers, vehicles, bookings, "
        "monthly revenue, and pending approval counts. "
        "Used in the Dashboard Platform Summary KPI row."
    ),
)
async def get_analytics(db: AsyncSession = Depends(get_db)):
    data = await AdminDashboardService.get_system_summary(db)
    return SystemSummary(**data)


# Doc Ref: Admin API §4 — also exposed as /system-summary per doc spec
@router.get(
    "/system-summary",
    response_model=SystemSummary,
    tags=["Admin – Dashboard"],
    summary="Platform-wide summary totals (alias)",
    include_in_schema=False,
)
async def get_system_summary(db: AsyncSession = Depends(get_db)):
    data = await AdminDashboardService.get_system_summary(db)
    return SystemSummary(**data)


@router.get(
    "/dashboard/city-performance",
    response_model=List[CityPerformanceItem],
    tags=["Admin – Dashboard"],
    summary="Top 5 cities by bookings this month (cab + hotel)",
)
async def get_city_performance(db: AsyncSession = Depends(get_db)):

    rows = await AdminDashboardService.get_city_performance(db)
    return [CityPerformanceItem(**r) for r in rows]


@router.get(
    "/dashboard/revenue-trend",
    response_model=List[RevenueTrendItem],
    tags=["Admin – Dashboard"],
    summary="Monthly revenue + booking count for the last 6 months",
)
async def get_revenue_trend(db: AsyncSession = Depends(get_db)):
    rows = await AdminDashboardService.get_revenue_trend(db)
    return [RevenueTrendItem(**r) for r in rows]


@router.get(
    "/dashboard/recent-activity",
    response_model=List[RecentActivityItem],
    tags=["Admin – Dashboard"],
    summary="Latest 10 platform events (bookings + partner actions)",
)
async def get_recent_activity(
    limit: int = Query(10, ge=1, le=30),
    db: AsyncSession = Depends(get_db),
):
    rows = await AdminDashboardService.get_recent_activity(db, limit=limit)
    return [RecentActivityItem(**r) for r in rows]


# ════════════════════════════════════════════════════════════════
#  §5  USER MANAGEMENT
# ════════════════════════════════════════════════════════════════


@router.get(
    "/users",
    response_model=AdminUserListResponse,
    tags=["Admin – Users"],
    summary="List all platform users with filters",
)
async def list_users(
    role: Optional[str] = Query(
        None, description="Filter by user_type: CUSTOMER | SUPER_ADMIN | STAFF"
    ),
    status: Optional[str] = Query(
        None, description="Filter by status: ACTIVE | SUSPENDED | PENDING"
    ),
    mobile: Optional[str] = Query(None, description="Partial mobile search"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    result = await AdminDashboardService.list_users(
        db, role=role, status=status, mobile=mobile, page=page, page_size=page_size
    )
    items = [AdminUserOut.model_validate(i) for i in result["items"]]
    return AdminUserListResponse(
        items=items,
        total=result["total"],
        page=result["page"],
        page_size=result["page_size"],
    )


@router.get(
    "/users/{user_id}",
    response_model=AdminUserOut,
    tags=["Admin – Users"],
    summary="Get a single user by UUID",
)
async def get_user(user_id: UUID, db: AsyncSession = Depends(get_db)):
    user = await AdminDashboardService.get_user(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return AdminUserOut.model_validate(user)


@router.patch(
    "/users/{user_id}/activate",
    response_model=AdminActionResponse,
    tags=["Admin – Users"],
    summary="Activate a user account",
)
async def activate_user(
    user_id: UUID, payload: UserStatusUpdate, db: AsyncSession = Depends(get_db)
):
    ok = await AdminDashboardService.set_user_status(
        db, user_id, is_active=True, status="ACTIVE"
    )
    if not ok:
        raise HTTPException(status_code=404, detail="User not found")
    return AdminActionResponse(message="User activated successfully")


@router.patch(
    "/users/{user_id}/suspend",
    response_model=AdminActionResponse,
    tags=["Admin – Users"],
    summary="Suspend a user account",
)
async def suspend_user(
    user_id: UUID, payload: UserStatusUpdate, db: AsyncSession = Depends(get_db)
):
    ok = await AdminDashboardService.set_user_status(
        db, user_id, is_active=False, status="SUSPENDED"
    )
    if not ok:
        raise HTTPException(status_code=404, detail="User not found")
    return AdminActionResponse(message="User suspended successfully")


# ════════════════════════════════════════════════════════════════
#  §8  PARTNER MANAGEMENT
# ════════════════════════════════════════════════════════════════
# Status flow (DB Schema Part 2 §6):
#   PENDING → UNDER_REVIEW → DOCUMENT_PENDING → APPROVED → ACTIVE → SUSPENDED → BLOCKED
# Admin actions:
#   review          : PENDING              → UNDER_REVIEW
#   document_pending: UNDER_REVIEW         → DOCUMENT_PENDING
#   approve         : UNDER_REVIEW | DOCUMENT_PENDING → APPROVED
#   activate        : APPROVED             → ACTIVE
#   suspend         : ACTIVE               → SUSPENDED
#   unsuspend       : SUSPENDED            → ACTIVE
#   block           : ACTIVE | SUSPENDED   → BLOCKED
# ════════════════════════════════════════════════════════════════

_ADMIN_PARTNER_TRANSITIONS: dict = {
    "PENDING": ["UNDER_REVIEW", "BLOCKED"],
    "UNDER_REVIEW": ["DOCUMENT_PENDING", "APPROVED", "BLOCKED"],
    "DOCUMENT_PENDING": ["UNDER_REVIEW", "APPROVED", "BLOCKED"],
    "APPROVED": ["ACTIVE", "SUSPENDED", "BLOCKED"],
    "ACTIVE": ["SUSPENDED", "BLOCKED"],
    "SUSPENDED": ["ACTIVE", "BLOCKED"],
    "BLOCKED": [],
}


async def _transition_partner(
    db: AsyncSession,
    partner_id: int,
    target_status: str,
    reason: Optional[str],
    action_label: str,
) -> AdminActionResponse:
    from sqlalchemy import text as _text

    row = (
        (
            await db.execute(
                _text(
                    "SELECT id, status FROM partners WHERE id = :id AND deleted_at IS NULL"
                ),
                {"id": partner_id},
            )
        )
        .mappings()
        .first()
    )

    if not row:
        raise HTTPException(status_code=404, detail="Partner not found")

    current_status: str = row["status"]
    allowed = _ADMIN_PARTNER_TRANSITIONS.get(current_status, [])
    if target_status not in allowed:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Cannot change partner status from '{current_status}' to '{target_status}'. "
                f"Allowed next statuses: {allowed or ['none — terminal state']}"
            ),
        )

    extra_cols = ", approved_at = NOW()" if target_status == "APPROVED" else ""
    await db.execute(
        _text(
            f"UPDATE partners SET status = :status, updated_at = NOW(){extra_cols} WHERE id = :id"
        ),
        {"status": target_status, "id": partner_id},
    )
    await db.execute(
        _text(
            "INSERT INTO partner_verification_logs (partner_id, action, remarks, created_at) "
            "VALUES (:pid, :action, :remarks, NOW())"
        ),
        {
            "pid": partner_id,
            "action": f"ADMIN_{target_status}",
            "remarks": reason
            or f"Admin action: {action_label} — {current_status} → {target_status}",
        },
    )
    await db.commit()
    return AdminActionResponse(message=f"Partner {action_label} successfully")


@router.get(
    "/partners",
    response_model=AdminPartnerListResponse,
    tags=["Admin – Partners"],
    summary="List all partners with optional status / type filter",
)
async def list_partners(
    status: Optional[str] = Query(
        None,
        description="PENDING | UNDER_REVIEW | DOCUMENT_PENDING | APPROVED | ACTIVE | SUSPENDED | BLOCKED",
    ),
    partner_type: Optional[str] = Query(None, description="INDIVIDUAL | COMPANY"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    result = await AdminDashboardService.list_partners(
        db, status=status, partner_type=partner_type, page=page, page_size=page_size
    )
    from app.modules.admin.schemas.dashboard import AdminPartnerOut

    items = [AdminPartnerOut.model_validate(i) for i in result["items"]]
    return AdminPartnerListResponse(
        items=items,
        total=result["total"],
        page=result["page"],
        page_size=result["page_size"],
    )


@router.get(
    "/partners/{partner_id}",
    tags=["Admin – Partners"],
    summary="Get full partner detail including documents, bank accounts and verification log",
)
async def get_partner_detail(partner_id: int, db: AsyncSession = Depends(get_db)):
    from sqlalchemy import text as _text

    row = (
        (
            await db.execute(
                _text(
                    """
            SELECT p.id, p.uuid::text, p.partner_code, p.partner_type,
                   p.business_name, p.owner_name, p.mobile, p.email,
                   p.city_id, p.logo_url, p.status, p.onboarding_source, p.approved_at,
                   p.created_at, p.updated_at,
                   -- Office address — Doc Ref: BRD Part 2 §20 | Migration 0016
                   p.office_address_line_1, p.office_address_line_2,
                   p.office_city_id, p.office_state_id, p.office_postal_code
            FROM partners p
            WHERE p.id = :id AND p.deleted_at IS NULL
        """
                ),
                {"id": partner_id},
            )
        )
        .mappings()
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Partner not found")

    docs = (
        (
            await db.execute(
                _text(
                    """
            SELECT id, document_type, COALESCE(document_number, NULL) AS document_number,
                   file_url, verification_status,
                   remarks, uploaded_at, verified_at, expiry_date
            FROM partner_documents WHERE partner_id = :pid ORDER BY uploaded_at DESC
        """
                ),
                {"pid": partner_id},
            )
        )
        .mappings()
        .all()
    )

    logs = (
        (
            await db.execute(
                _text(
                    """
            SELECT id, action, remarks, created_at
            FROM partner_verification_logs WHERE partner_id = :pid
            ORDER BY created_at DESC LIMIT 30
        """
                ),
                {"pid": partner_id},
            )
        )
        .mappings()
        .all()
    )

    banks = (
        (
            await db.execute(
                _text(
                    """
            SELECT id, account_holder_name, ifsc_code, bank_name,
                   branch_name, is_primary, verification_status, created_at
            FROM partner_bank_accounts WHERE partner_id = :pid
        """
                ),
                {"pid": partner_id},
            )
        )
        .mappings()
        .all()
    )

    # ── GST details (for COMPANY partners) ───────────────────────────────────
    gst_details = (
        (
            await db.execute(
                _text(
                    """
            SELECT id, gst_number, pan_number, legal_name, trade_name,
                   registration_date, gst_status, verified_at, created_at
            FROM partner_gst_details WHERE partner_id = :pid LIMIT 1
        """
                ),
                {"pid": partner_id},
            )
        )
        .mappings()
        .first()
    )

    # ── Commission group currently assigned (most recent assignment) ─────────
    commission_group = (
        (
            await db.execute(
                _text(
                    """
            SELECT cg.id, cg.group_name, cg.description, cg.is_active,
                   pcg.assigned_at
            FROM partner_commission_groups pcg
            JOIN commission_groups cg ON cg.id = pcg.commission_group_id
            WHERE pcg.partner_id = :pid
            ORDER BY pcg.assigned_at DESC
            LIMIT 1
        """
                ),
                {"pid": partner_id},
            )
        )
        .mappings()
        .first()
    )

    # ── Services (CAB / HOTEL / TOUR) ───────────────────────────────────────────
    from sqlalchemy import text as _text_svc

    services_rows = (
        (
            await db.execute(
                _text_svc(
                    "SELECT service_type, is_active FROM partner_services WHERE partner_id = :pid ORDER BY service_type"
                ),
                {"pid": partner_id},
            )
        )
        .mappings()
        .all()
    )

    return {
        **dict(row),
        "documents": [dict(d) for d in docs],
        "verification_logs": [dict(entry) for entry in logs],
        "bank_accounts": [dict(b) for b in banks],
        "gst_details": dict(gst_details) if gst_details else None,
        "commission_group": dict(commission_group) if commission_group else None,
        "services": [dict(s) for s in services_rows],
        "allowed_transitions": _ADMIN_PARTNER_TRANSITIONS.get(row["status"], []),
    }


# ── Status transition endpoints (one per action for clear audit trail) ────────


@router.patch(
    "/partners/{partner_id}/review",
    response_model=AdminActionResponse,
    tags=["Admin – Partners"],
    summary="Start KYC review — PENDING → UNDER_REVIEW",
)
async def review_partner(
    partner_id: int, payload: PartnerActionRequest, db: AsyncSession = Depends(get_db)
):
    return await _transition_partner(
        db, partner_id, "UNDER_REVIEW", payload.reason, "moved to KYC review"
    )


@router.patch(
    "/partners/{partner_id}/document-pending",
    response_model=AdminActionResponse,
    tags=["Admin – Partners"],
    summary="Request additional documents — UNDER_REVIEW → DOCUMENT_PENDING",
)
async def document_pending_partner(
    partner_id: int, payload: PartnerActionRequest, db: AsyncSession = Depends(get_db)
):
    return await _transition_partner(
        db, partner_id, "DOCUMENT_PENDING", payload.reason, "marked document pending"
    )


@router.patch(
    "/partners/{partner_id}/approve",
    response_model=AdminActionResponse,
    tags=["Admin – Partners"],
    summary="Approve KYC — UNDER_REVIEW | DOCUMENT_PENDING → APPROVED",
)
async def approve_partner(
    partner_id: int, payload: PartnerActionRequest, db: AsyncSession = Depends(get_db)
):
    return await _transition_partner(
        db, partner_id, "APPROVED", payload.reason, "KYC approved"
    )


@router.patch(
    "/partners/{partner_id}/activate",
    response_model=AdminActionResponse,
    tags=["Admin – Partners"],
    summary="Activate an approved partner — APPROVED → ACTIVE",
)
async def activate_partner(
    partner_id: int, payload: PartnerActionRequest, db: AsyncSession = Depends(get_db)
):
    return await _transition_partner(
        db, partner_id, "ACTIVE", payload.reason, "activated"
    )


@router.patch(
    "/partners/{partner_id}/suspend",
    response_model=AdminActionResponse,
    tags=["Admin – Partners"],
    summary="Suspend an active partner — ACTIVE → SUSPENDED",
)
async def suspend_partner(
    partner_id: int, payload: PartnerActionRequest, db: AsyncSession = Depends(get_db)
):
    return await _transition_partner(
        db, partner_id, "SUSPENDED", payload.reason, "suspended"
    )


@router.patch(
    "/partners/{partner_id}/unsuspend",
    response_model=AdminActionResponse,
    tags=["Admin – Partners"],
    summary="Re-activate a suspended partner — SUSPENDED → ACTIVE",
)
async def unsuspend_partner(
    partner_id: int, payload: PartnerActionRequest, db: AsyncSession = Depends(get_db)
):
    return await _transition_partner(
        db, partner_id, "ACTIVE", payload.reason, "unsuspended"
    )


@router.patch(
    "/partners/{partner_id}/block",
    response_model=AdminActionResponse,
    tags=["Admin – Partners"],
    summary="Block a partner permanently — any active state → BLOCKED",
)
async def block_partner(
    partner_id: int, payload: PartnerActionRequest, db: AsyncSession = Depends(get_db)
):
    return await _transition_partner(
        db, partner_id, "BLOCKED", payload.reason, "blocked"
    )


# ════════════════════════════════════════════════════════════════
#  ADMIN: RESET PARTNER PASSWORD TO DEFAULT
#  Resets partner password to "Waytero@15" and sets force_password_change=TRUE
#  Partner must then login with Waytero@15 and set a new password
# ════════════════════════════════════════════════════════════════


@router.post(
    "/partners/{partner_id}/reset-password",
    response_model=AdminActionResponse,
    tags=["Admin – Partners"],
    summary="Admin: Reset partner password to default (Waytero@15)",
    description=(
        "Resets the partner's password to the default 'Waytero@15' and sets "
        "force_password_change=TRUE. Partner must login with Waytero@15 then "
        "set a new password before accessing the portal. "
        "Doc Ref: Partner Portal — admin password reset flow"
    ),
)
async def admin_reset_partner_password(
    partner_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin()),
):
    from sqlalchemy import text as _rtext
    from argon2 import PasswordHasher as _RPH

    # Verify partner exists
    row = (
        (
            await db.execute(
                _rtext(
                    "SELECT user_id FROM partners WHERE id = :id AND deleted_at IS NULL"
                ),
                {"id": partner_id},
            )
        )
        .mappings()
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Partner not found")

    user_id = row["user_id"]

    # Hash default password
    _rph = _RPH(time_cost=2, memory_cost=65536, parallelism=2, hash_len=32, salt_len=16)
    _default_hash = _rph.hash("Waytero@15")

    # Update user: set default password hash + force_password_change = TRUE + revoke sessions
    await db.execute(
        _rtext(
            """
            UPDATE users
            SET password_hash = :pwd_hash,
                force_password_change = TRUE,
                failed_login_attempts = 0,
                locked_until = NULL,
                updated_at = NOW()
            WHERE id = :uid
        """
        ),
        {"pwd_hash": _default_hash, "uid": str(user_id)},
    )

    # Revoke all active sessions for this partner
    await db.execute(
        _rtext(
            """
            UPDATE user_sessions
            SET is_active = FALSE
            WHERE user_id = :uid
        """
        ),
        {"uid": str(user_id)},
    )

    # Audit log
    await db.execute(
        _rtext(
            """
            INSERT INTO partner_verification_logs (partner_id, action, remarks, created_at)
            VALUES (:pid, 'PASSWORD_RESET', 'Password reset to default by admin. Partner must change on next login.', NOW())
        """
        ),
        {"pid": partner_id},
    )

    # Platform audit log (audit_logs table)
    actor_id = _audit_user_id(current_user)
    await AuditLogger.log_partner_event(
        db,
        action_type="PASSWORD_RESET",
        user_id=actor_id,
        partner_id=partner_id,
        new_values={"force_password_change": True, "default_password": "Waytero@15"},
        ip_address=_client_ip(request),
        user_agent=_user_agent(request),
        request_id=_request_id(request),
    )

    await db.commit()
    return AdminActionResponse(
        message="Partner password reset to default. Partner must login with Waytero@15 and set a new password."
    )


# ── Commission Group assignment ────────────────────────────────────────────────
# Doc Ref: Partner API §15 | DB Schema Part 2 §17


class _AssignCommissionGroupPayload(_BM3):
    commission_group_id: int


@router.post(
    "/partners/{partner_id}/commission-group",
    tags=["Admin – Partners"],
    summary="Assign commission group to a partner",
    description="Inserts into partner_commission_groups. Replaces any existing assignment (keeps history). Doc Ref: Partner API §15",
)
async def assign_commission_group(
    partner_id: int,
    payload: _AssignCommissionGroupPayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin()),
):
    from sqlalchemy import text as _t2

    # Verify partner exists
    partner_row = (
        await db.execute(
            _t2("SELECT id FROM partners WHERE id = :id AND deleted_at IS NULL"),
            {"id": partner_id},
        )
    ).fetchone()
    if not partner_row:
        raise HTTPException(status_code=404, detail="Partner not found")

    # Verify commission group exists and is active
    group_row = (
        (
            await db.execute(
                _t2(
                    "SELECT id, group_name FROM commission_groups WHERE id = :gid AND is_active = TRUE"
                ),
                {"gid": payload.commission_group_id},
            )
        )
        .mappings()
        .first()
    )
    if not group_row:
        raise HTTPException(
            status_code=404, detail="Commission group not found or inactive"
        )

    # Insert new assignment (history is preserved — previous rows remain)
    await db.execute(
        _t2(
            """
            INSERT INTO partner_commission_groups (partner_id, commission_group_id, assigned_at)
            VALUES (:pid, :gid, NOW())
        """
        ),
        {"pid": partner_id, "gid": payload.commission_group_id},
    )

    # Audit log
    await db.execute(
        _t2(
            """
            INSERT INTO partner_verification_logs (partner_id, action, remarks, created_at)
            VALUES (:pid, 'COMMISSION_GROUP_ASSIGNED',
                    :remarks, NOW())
        """
        ),
        {
            "pid": partner_id,
            "remarks": f"Commission group assigned: {group_row['group_name']} (id={payload.commission_group_id})",
        },
    )

    # Platform audit log (audit_logs table)
    actor_id = _audit_user_id(current_user)
    await AuditLogger.log_partner_event(
        db,
        action_type="COMMISSION_GROUP_ASSIGNED",
        user_id=actor_id,
        partner_id=partner_id,
        new_values={
            "commission_group_id": payload.commission_group_id,
            "commission_group_name": group_row["group_name"],
        },
        ip_address=_client_ip(request),
        user_agent=_user_agent(request),
        request_id=_request_id(request),
    )

    await db.commit()

    return {
        "success": True,
        "message": f"Commission group '{group_row['group_name']}' assigned successfully",
        "data": {
            "partner_id": partner_id,
            "commission_group_id": payload.commission_group_id,
            "group_name": group_row["group_name"],
        },
    }


@router.delete(
    "/partners/{partner_id}/commission-group",
    tags=["Admin – Partners"],
    summary="Remove all commission group assignments from a partner",
)
async def remove_commission_group(
    partner_id: int,
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import text as _t3

    partner_row = (
        await db.execute(
            _t3("SELECT id FROM partners WHERE id = :id AND deleted_at IS NULL"),
            {"id": partner_id},
        )
    ).fetchone()
    if not partner_row:
        raise HTTPException(status_code=404, detail="Partner not found")

    await db.execute(
        _t3("DELETE FROM partner_commission_groups WHERE partner_id = :pid"),
        {"pid": partner_id},
    )
    await db.execute(
        _t3(
            """
            INSERT INTO partner_verification_logs (partner_id, action, remarks, created_at)
            VALUES (:pid, 'COMMISSION_GROUP_REMOVED', 'Commission group assignment removed by admin', NOW())
        """
        ),
        {"pid": partner_id},
    )
    await db.commit()
    return {"success": True, "message": "Commission group assignment removed"}


class AdminPartnerEditPayload(_BaseModel):
    owner_name: _Optional[str] = None
    business_name: _Optional[str] = None
    mobile: _Optional[str] = None
    email: _Optional[str] = None
    city_id: _Optional[int] = None
    partner_type: _Optional[str] = None
    onboarding_source: _Optional[str] = None
    logo_url: _Optional[str] = None
    # Office address — Doc Ref: BRD Part 2 §20 | Migration 0016
    office_address_line_1: _Optional[str] = None
    office_address_line_2: _Optional[str] = None
    office_city_id: _Optional[int] = None
    office_state_id: _Optional[int] = None
    office_postal_code: _Optional[str] = None
    # Tax info (updatable separately; here for convenience)
    gst_number: _Optional[str] = None
    pan_number: _Optional[str] = None
    gst_legal_name: _Optional[str] = None
    gst_trade_name: _Optional[str] = None


@router.patch(
    "/partners/{partner_id}",
    response_model=AdminActionResponse,
    tags=["Admin – Partners"],
    summary="Admin: Edit partner profile details",
    description=(
        "Allows admin to update editable fields of a partner profile. "
        "All fields are optional — only supplied fields are updated. "
        "Doc Ref: DB Schema Part 2 §4 — partners table"
    ),
)
async def admin_edit_partner(
    partner_id: int,
    payload: AdminPartnerEditPayload,
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import text as _text2

    # ── Separate GST fields from partner fields ───────────────────────────────
    GST_FIELDS = {"gst_number", "pan_number", "gst_legal_name", "gst_trade_name"}
    all_data = payload.model_dump(exclude_unset=True)

    gst_data = {k: v for k, v in all_data.items() if k in GST_FIELDS and v is not None}
    partner_data = {
        k: v for k, v in all_data.items() if k not in GST_FIELDS and v is not None
    }

    if not partner_data and not gst_data:
        raise HTTPException(status_code=422, detail="No fields provided to update")

    row = (
        (
            await db.execute(
                _text2(
                    "SELECT id, city_id, partner_type FROM partners WHERE id = :id AND deleted_at IS NULL"
                ),
                {"id": partner_id},
            )
        )
        .mappings()
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Partner not found")

    # ── Enforce: office_city_id must match city_id (BRD §20) ─────────────────
    effective_city = partner_data.get("city_id", row["city_id"])
    if (
        "office_city_id" in partner_data
        and partner_data["office_city_id"] != effective_city
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                f"office_city_id ({partner_data['office_city_id']}) must match the partner's "
                f"operating city_id ({effective_city})."
            ),
        )

    # ── Enforce: COMPANY partners must have GST number (BRD Rule 14) ─────────
    effective_type = partner_data.get("partner_type", row["partner_type"])
    if effective_type == "COMPANY":
        # Check if GST already exists or is being provided now
        existing_gst = (
            await db.execute(
                _text2(
                    "SELECT gst_number FROM partner_gst_details WHERE partner_id = :pid LIMIT 1"
                ),
                {"pid": partner_id},
            )
        ).fetchone()
        has_gst = (existing_gst and existing_gst[0]) or gst_data.get("gst_number")
        if not has_gst:
            raise HTTPException(
                status_code=422,
                detail="Company partners must have a GST Number. Please provide gst_number.",
            )

    # ── Update partners table ─────────────────────────────────────────────────
    if partner_data:
        set_parts = ", ".join(f"{k} = :{k}" for k in partner_data)
        partner_data["id"] = partner_id
        await db.execute(
            _text2(
                f"UPDATE partners SET {set_parts}, updated_at = NOW() WHERE id = :id"
            ),
            partner_data,
        )

    # ── Update/insert GST details ─────────────────────────────────────────────
    if gst_data:
        # Remap ONLY gst_legal_name → legal_name and gst_trade_name → trade_name.
        # gst_number and pan_number keep their names (match actual DB columns).
        RENAME_MAP = {
            "gst_legal_name": "legal_name",
            "gst_trade_name": "trade_name",
        }
        gst_insert = {RENAME_MAP.get(k, k): v for k, v in gst_data.items()}
        # Build upsert
        gst_insert["pid"] = partner_id
        cols = [c for c in gst_insert if c != "pid"]
        set_clause = ", ".join(
            f"{c} = COALESCE(EXCLUDED.{c}, partner_gst_details.{c})" for c in cols
        )
        col_list = ", ".join(cols)
        val_list = ", ".join(f":{c}" for c in cols)
        await db.execute(
            _text2(
                f"""
                INSERT INTO partner_gst_details (partner_id, {col_list}, created_at)
                VALUES (:pid, {val_list}, NOW())
                ON CONFLICT (partner_id) DO UPDATE SET {set_clause}
            """
            ),
            gst_insert,
        )

    await db.execute(
        _text2(
            "INSERT INTO partner_verification_logs (partner_id, action, remarks, created_at) "
            "VALUES (:pid, 'ADMIN_PROFILE_EDIT', 'Partner profile edited by admin', NOW())"
        ),
        {"pid": partner_id},
    )
    await db.commit()
    return AdminActionResponse(message="Partner details updated successfully")


class _PartnerSvcPayload(_BM_svc_v2):
    services: list  # ["CAB", "HOTEL", "TOUR"]


@router.patch(
    "/partners/{partner_id}/services",
    response_model=AdminActionResponse,
    tags=["Admin – Partners"],
    summary="Admin: Update partner service links (CAB / HOTEL / TOUR)",
    description="Replaces partner service links. Pass services=['CAB'] to activate only CAB. Pass [] to deactivate all. Doc Ref: DB Schema Part 2 §7",
)
async def admin_update_partner_services(
    partner_id: int,
    payload: _PartnerSvcPayload,
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import text as _tsvc2
    from app.modules.admin.services import ServiceTypeService as _STS2

    valid = await _STS2.get_valid_codes(db)
    requested = {s.upper() for s in (payload.services or []) if s.upper() in valid}
    invalid = [s for s in (payload.services or []) if s.upper() not in valid]
    if invalid:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid service types: {invalid}. Allowed: {sorted(valid)}",
        )

    p = (
        await db.execute(
            _tsvc2("SELECT id FROM partners WHERE id = :id AND deleted_at IS NULL"),
            {"id": partner_id},
        )
    ).fetchone()
    if not p:
        raise HTTPException(status_code=404, detail="Partner not found")

    # Deactivate all existing services first
    await db.execute(
        _tsvc2("UPDATE partner_services SET is_active = FALSE WHERE partner_id = :pid"),
        {"pid": partner_id},
    )

    # Re-activate / insert requested services
    for svc in requested:
        await db.execute(
            _tsvc2(
                """
            INSERT INTO partner_services (partner_id, service_type, is_active, created_at)
            VALUES (:pid, :svc, TRUE, NOW())
            ON CONFLICT (partner_id, service_type)
            DO UPDATE SET is_active = TRUE
        """
            ),
            {"pid": partner_id, "svc": svc},
        )

    summary = ", ".join(sorted(requested)) if requested else "none"
    await db.execute(
        _tsvc2(
            "INSERT INTO partner_verification_logs (partner_id, action, remarks, created_at) "
            "VALUES (:pid, 'ADMIN_SERVICES_UPDATED', :remarks, NOW())"
        ),
        {"pid": partner_id, "remarks": f"Services updated to: {summary}"},
    )

    await db.commit()
    return AdminActionResponse(
        message=f"Partner services updated successfully. Active: {summary}"
    )


@router.patch(
    "/partners/{partner_id}/verify-document/{document_id}",
    response_model=AdminActionResponse,
    tags=["Admin – Partners"],
    summary="Approve or reject an individual KYC document",
)
async def verify_partner_document(
    partner_id: int,
    document_id: int,
    verification_status: str = Query(..., description="APPROVED | REJECTED"),
    remarks: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import text as _text

    if verification_status not in ("APPROVED", "REJECTED"):
        raise HTTPException(
            status_code=422, detail="verification_status must be APPROVED or REJECTED"
        )
    r = await db.execute(
        _text(
            """
            UPDATE partner_documents
            SET verification_status = :vs, remarks = :remarks, verified_at = NOW()
            WHERE id = :did AND partner_id = :pid
        """
        ),
        {
            "vs": verification_status,
            "remarks": remarks,
            "did": document_id,
            "pid": partner_id,
        },
    )
    if r.rowcount == 0:
        raise HTTPException(
            status_code=404, detail="Document not found for this partner"
        )
    await db.commit()
    return AdminActionResponse(
        message=f"Document {verification_status.lower()} successfully"
    )


# ════════════════════════════════════════════════════════════════
#  §9  DRIVER MANAGEMENT
# ════════════════════════════════════════════════════════════════


@router.get(
    "/drivers",
    response_model=AdminDriverListResponse,
    tags=["Admin – Drivers"],
    summary="List all drivers with status filter",
)
async def list_drivers(
    status: Optional[str] = Query(
        None, description="ACTIVE | PENDING | SUSPENDED | BLOCKED"
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    result = await AdminDashboardService.list_drivers(
        db, status=status, page=page, page_size=page_size
    )
    from app.modules.admin.schemas.dashboard import AdminDriverOut

    items = [AdminDriverOut.model_validate(i) for i in result["items"]]
    return AdminDriverListResponse(
        items=items,
        total=result["total"],
        page=result["page"],
        page_size=result["page_size"],
    )


def _parse_date(value):
    """Convert ISO date string (YYYY-MM-DD) or date object to datetime.date. Returns None if empty."""
    if not value:
        return None
    if isinstance(value, _date):
        return value
    try:
        return _date.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


@router.post(
    "/drivers",
    tags=["Admin – Drivers"],
    summary="Admin: Create a driver under a partner",
)
async def admin_create_driver(
    payload: AdminDriverCreate, db: AsyncSession = Depends(get_db)
):
    """
    Admin creates a driver on behalf of a partner.
    Uses raw SQL to avoid SQLAlchemy lazy-load greenlet errors with async sessions.
    Inserts driver + initialises driver_availability + driver_performance_summary.
    """
    import uuid as _uuid
    import random
    from sqlalchemy import text

    # Check mobile uniqueness
    existing = (
        await db.execute(
            text(
                "SELECT id FROM drivers WHERE mobile = :mobile AND deleted_at IS NULL"
            ),
            {"mobile": payload.mobile},
        )
    ).scalar()
    if existing:
        raise HTTPException(
            status_code=409, detail="A driver with this mobile number already exists"
        )

    # Generate unique driver code
    for _ in range(10):
        code = f"DRV-{random.randint(100000, 999999)}"
        taken = (
            await db.execute(
                text("SELECT id FROM drivers WHERE driver_code = :code"),
                {"code": code},
            )
        ).scalar()
        if not taken:
            break

    driver_uuid = str(_uuid.uuid4())

    # Insert driver record
    row = (
        (
            await db.execute(
                text(
                    """
            INSERT INTO drivers (
                uuid, partner_id, driver_code, full_name, mobile, email,
                license_number, license_expiry_date, date_of_birth, joining_date,
                status, created_at, updated_at
            ) VALUES (
                CAST(:uuid AS uuid), :partner_id, :code, :full_name, :mobile, :email,
                :license_number,
                CAST(:license_expiry_date AS date),
                CAST(:date_of_birth AS date),
                CAST(:joining_date AS date),
                'PENDING', NOW(), NOW()
            )
            RETURNING id, driver_code, status, created_at
        """
                ),
                {
                    "uuid": driver_uuid,
                    "partner_id": payload.partner_id,
                    "code": code,
                    "full_name": payload.full_name,
                    "mobile": payload.mobile,
                    "email": payload.email or None,
                    "license_number": payload.license_number,
                    "license_expiry_date": _parse_date(payload.license_expiry_date),
                    "date_of_birth": _parse_date(payload.date_of_birth),
                    "joining_date": _parse_date(payload.joining_date),
                },
            )
        )
        .mappings()
        .first()
    )

    driver_id = row["id"]

    # Initialise availability record
    await db.execute(
        text(
            """
            INSERT INTO driver_availability (driver_id, availability_status, updated_at)
            VALUES (:driver_id, 'OFFLINE', NOW())
            ON CONFLICT (driver_id) DO NOTHING
        """
        ),
        {"driver_id": driver_id},
    )

    # Initialise performance summary
    await db.execute(
        text(
            """
            INSERT INTO driver_performance_summary
                (driver_id, completed_trips, cancelled_trips, acceptance_rate, average_rating, updated_at)
            VALUES (:driver_id, 0, 0, 0.00, 0.00, NOW())
            ON CONFLICT (driver_id) DO NOTHING
        """
        ),
        {"driver_id": driver_id},
    )

    await db.commit()

    # ── Email: driver registration received (when an email is provided) ──
    try:
        from app.infrastructure.email import send_event_email

        await send_event_email(
            db,
            event_type="driver_registered",
            to_email=payload.email or "",
            to_name=payload.full_name or None,
            context={
                "name": payload.full_name or "driver",
                "message": (
                    "Your driver application has been received. Our team will verify "
                    "your documents and activate your profile shortly."
                ),
                "body": [
                    "Keep your documents ready — you'll be notified once verification "
                    "is complete and trips start flowing to you.",
                ],
                "details": [
                    ("Driver code", row["driver_code"]),
                    ("Mobile", payload.mobile),
                    ("Status", "Pending verification"),
                ],
            },
            related_type="DRIVER",
            related_id=driver_id,
        )
    except Exception:  # pragma: no cover — email must never break registration
        pass

    return {
        "success": True,
        "message": "Driver registered successfully",
        "data": {
            "driver_id": driver_id,
            "driver_code": row["driver_code"],
            "status": row["status"],
            "created_at": str(row["created_at"]),
        },
    }


@router.get(
    "/drivers/{driver_id}",
    tags=["Admin – Drivers"],
    summary="Admin: Get full driver detail",
)
async def admin_get_driver(driver_id: int, db: AsyncSession = Depends(get_db)):
    """
    Returns full driver profile with documents and availability for admin detail drawer.
    """
    from sqlalchemy import text

    row = (
        (
            await db.execute(
                text(
                    """
            SELECT d.id, d.uuid::text AS uuid, d.driver_code, d.partner_id, d.full_name, d.mobile AS mobile_number,
                   d.email, d.license_number,
                   d.license_expiry_date::text AS license_expiry_date,
                   d.date_of_birth::text AS date_of_birth,
                   d.joining_date::text AS joining_date,
                   d.status, d.approved_at, d.created_at,
                   da.availability_status,
                   dps.completed_trips, dps.average_rating
            FROM drivers d
            LEFT JOIN driver_availability da ON da.driver_id = d.id
            LEFT JOIN driver_performance_summary dps ON dps.driver_id = d.id
            WHERE d.id = :driver_id AND d.deleted_at IS NULL
        """
                ),
                {"driver_id": driver_id},
            )
        )
        .mappings()
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Driver not found")

    docs_rows = (
        (
            await db.execute(
                text(
                    """
            SELECT id, document_type, file_url, verification_status,
                   expiry_date::text AS expiry_date, remarks, uploaded_at
            FROM driver_documents WHERE driver_id = :driver_id
            ORDER BY uploaded_at DESC
        """
                ),
                {"driver_id": driver_id},
            )
        )
        .mappings()
        .all()
    )

    result = dict(row)
    result["documents"] = [dict(r) for r in docs_rows]
    return result


@router.patch(
    "/drivers/{driver_id}/verify-document/{document_id}",
    tags=["Admin – Drivers"],
    summary="Admin: Verify a driver document",
)
async def admin_verify_driver_document(
    driver_id: int,
    document_id: int,
    verification_status: str = Query(..., description="APPROVED | REJECTED"),
    remarks: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import text

    r = await db.execute(
        text(
            """
            UPDATE driver_documents
            SET verification_status = :status, remarks = :remarks, verified_at = NOW()
            WHERE id = :doc_id AND driver_id = :driver_id
        """
        ),
        {
            "status": verification_status,
            "remarks": remarks,
            "doc_id": document_id,
            "driver_id": driver_id,
        },
    )
    await db.commit()
    if r.rowcount == 0:
        raise HTTPException(status_code=404, detail="Document not found")
    return AdminActionResponse(
        message=f"Document {verification_status.lower()} successfully"
    )


@router.post(
    "/drivers/{driver_id}/documents",
    tags=["Admin – Drivers"],
    summary="Admin: Upload a document (including PHOTO) for a driver",
)
async def admin_upload_driver_document(
    driver_id: int,
    payload: AdminDriverDocumentCreate,
    db: AsyncSession = Depends(get_db),
):
    """
    Admin uploads a document record for a driver (file already uploaded to Cloudinary).
    Supports all document types: DRIVING_LICENSE | AADHAAR | PAN | PHOTO | POLICE_VERIFICATION | MEDICAL_CERTIFICATE
    """
    from sqlalchemy import text

    # Verify driver exists
    row = (
        await db.execute(
            text("SELECT id FROM drivers WHERE id = :driver_id AND deleted_at IS NULL"),
            {"driver_id": driver_id},
        )
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Driver not found")

    await db.execute(
        text(
            """
            INSERT INTO driver_documents
                (driver_id, document_type, file_url, expiry_date, verification_status, uploaded_at)
            VALUES
                (:driver_id, :document_type, :file_url, :expiry_date, 'PENDING', NOW())
        """
        ),
        {
            "driver_id": driver_id,
            "document_type": payload.document_type,
            "file_url": payload.file_url,
            "expiry_date": payload.expiry_date or None,
        },
    )
    await db.commit()
    return AdminActionResponse(message=f"{payload.document_type} uploaded successfully")


@router.patch(
    "/drivers/{driver_id}/approve",
    response_model=AdminActionResponse,
    tags=["Admin – Drivers"],
    summary="Approve a driver",
)
async def approve_driver(
    driver_id: int, payload: DriverActionRequest, db: AsyncSession = Depends(get_db)
):
    ok = await AdminDashboardService.set_driver_status(db, driver_id, "ACTIVE")
    if not ok:
        raise HTTPException(status_code=404, detail="Driver not found")
    return AdminActionResponse(message="Driver approved successfully")


@router.patch(
    "/drivers/{driver_id}/suspend",
    response_model=AdminActionResponse,
    tags=["Admin – Drivers"],
    summary="Suspend a driver",
)
async def suspend_driver(
    driver_id: int, payload: DriverActionRequest, db: AsyncSession = Depends(get_db)
):
    ok = await AdminDashboardService.set_driver_status(db, driver_id, "SUSPENDED")
    if not ok:
        raise HTTPException(status_code=404, detail="Driver not found")
    return AdminActionResponse(message="Driver suspended successfully")


@router.patch(
    "/drivers/{driver_id}/block",
    response_model=AdminActionResponse,
    tags=["Admin – Drivers"],
    summary="Block a driver",
)
async def block_driver(
    driver_id: int, payload: DriverActionRequest, db: AsyncSession = Depends(get_db)
):
    ok = await AdminDashboardService.set_driver_status(db, driver_id, "BLOCKED")
    if not ok:
        raise HTTPException(status_code=404, detail="Driver not found")
    return AdminActionResponse(message="Driver blocked successfully")


# ════════════════════════════════════════════════════════════════
#  §10  VEHICLE MANAGEMENT
# ════════════════════════════════════════════════════════════════


@router.get(
    "/vehicles",
    response_model=AdminVehicleListResponse,
    tags=["Admin – Vehicles"],
    summary="List all vehicles with status filter",
)
async def list_vehicles(
    status: Optional[str] = Query(
        None, description="ACTIVE | PENDING | SUSPENDED | REJECTED"
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    result = await AdminDashboardService.list_vehicles(
        db, status=status, page=page, page_size=page_size
    )
    from app.modules.admin.schemas.dashboard import AdminVehicleOut

    items = [AdminVehicleOut.model_validate(i) for i in result["items"]]
    return AdminVehicleListResponse(
        items=items,
        total=result["total"],
        page=result["page"],
        page_size=result["page_size"],
    )


@router.patch(
    "/vehicles/{vehicle_id}/approve",
    response_model=AdminActionResponse,
    tags=["Admin – Vehicles"],
    summary="Approve a vehicle",
)
async def approve_vehicle(
    vehicle_id: int, payload: VehicleActionRequest, db: AsyncSession = Depends(get_db)
):
    ok = await AdminDashboardService.set_vehicle_status(db, vehicle_id, "ACTIVE")
    if not ok:
        raise HTTPException(status_code=404, detail="Vehicle not found")
    return AdminActionResponse(message="Vehicle approved successfully")


@router.patch(
    "/vehicles/{vehicle_id}/suspend",
    response_model=AdminActionResponse,
    tags=["Admin – Vehicles"],
    summary="Suspend a vehicle",
)
async def suspend_vehicle(
    vehicle_id: int, payload: VehicleActionRequest, db: AsyncSession = Depends(get_db)
):
    ok = await AdminDashboardService.set_vehicle_status(db, vehicle_id, "SUSPENDED")
    if not ok:
        raise HTTPException(status_code=404, detail="Vehicle not found")
    return AdminActionResponse(message="Vehicle suspended successfully")


# ════════════════════════════════════════════════════════════════
#  §16  SETTLEMENT MANAGEMENT
# ════════════════════════════════════════════════════════════════


@router.get(
    "/settlements",
    response_model=AdminSettlementListResponse,
    tags=["Admin – Settlements"],
    summary="List all settlements with status filter",
)
async def list_settlements(
    status: Optional[str] = Query(
        None, description="PENDING | PROCESSING | PAID | FAILED"
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    result = await AdminDashboardService.list_settlements(
        db, status=status, page=page, page_size=page_size
    )
    from app.modules.admin.schemas.dashboard import AdminSettlementOut

    items = [AdminSettlementOut.model_validate(i) for i in result["items"]]
    return AdminSettlementListResponse(
        items=items,
        total=result["total"],
        page=result["page"],
        page_size=result["page_size"],
    )


@router.patch(
    "/settlements/{settlement_id}/mark-paid",
    response_model=AdminActionResponse,
    tags=["Admin – Settlements"],
    summary="Mark a settlement as PAID",
)
async def mark_settlement_paid(
    settlement_id: int,
    current_user: dict = Depends(
        require_roles("ADMIN", "SUPER_ADMIN", "FINANCE_MANAGER")
    ),
    db: AsyncSession = Depends(get_db),
):
    ok = await AdminDashboardService.update_settlement_status(db, settlement_id, "PAID")
    if not ok:
        raise HTTPException(status_code=404, detail="Settlement not found")
    return AdminActionResponse(message="Settlement marked as PAID")


@router.patch(
    "/settlements/{settlement_id}/mark-processing",
    response_model=AdminActionResponse,
    tags=["Admin – Settlements"],
    summary="Mark a settlement as PROCESSING",
)
async def mark_settlement_processing(
    settlement_id: int,
    current_user: dict = Depends(
        require_roles("ADMIN", "SUPER_ADMIN", "FINANCE_MANAGER")
    ),
    db: AsyncSession = Depends(get_db),
):
    ok = await AdminDashboardService.update_settlement_status(
        db, settlement_id, "PROCESSING"
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Settlement not found")
    return AdminActionResponse(message="Settlement marked as PROCESSING")


# ════════════════════════════════════════════════════════════════
#  §22  NOTIFICATIONS BROADCAST
# ════════════════════════════════════════════════════════════════


@router.post(
    "/notifications/broadcast",
    response_model=BroadcastResponse,
    tags=["Admin – Notifications"],
    summary="Broadcast a message to all active users",
    description=(
        "Queues a broadcast notification to all active users via the specified channel "
        "(SMS | WHATSAPP | EMAIL | PUSH | IN_APP). "
        "Returns the count of users queued."
    ),
)
async def broadcast_notification(
    payload: BroadcastRequest, db: AsyncSession = Depends(get_db)
):
    count = await AdminDashboardService.broadcast(
        db, payload.channel, payload.message, payload.title
    )
    return BroadcastResponse(
        success=True,
        message=f"Broadcast queued for {count} user(s) via {payload.channel}",
        queued_count=count,
    )


# ════════════════════════════════════════════════════════════════
#  §23  REPORTS
# ════════════════════════════════════════════════════════════════


@router.get(
    "/reports/revenue",
    response_model=RevenueReport,
    tags=["Admin – Reports"],
    summary="Revenue report for the current period",
)
async def report_revenue(
    period: str = Query("monthly", description="daily | weekly | monthly"),
    db: AsyncSession = Depends(get_db),
):
    return RevenueReport(**(await AdminDashboardService.report_revenue(db, period)))


@router.get(
    "/reports/bookings",
    response_model=BookingReport,
    tags=["Admin – Reports"],
    summary="Booking statistics for the current period",
)
async def report_bookings(
    period: str = Query("monthly", description="daily | weekly | monthly"),
    db: AsyncSession = Depends(get_db),
):
    return BookingReport(**(await AdminDashboardService.report_bookings(db, period)))


@router.get(
    "/reports/partners",
    response_model=PartnerReport,
    tags=["Admin – Reports"],
    summary="Partner status breakdown",
)
async def report_partners(db: AsyncSession = Depends(get_db)):
    return PartnerReport(**(await AdminDashboardService.report_partners(db)))


@router.get(
    "/reports/settlements",
    response_model=SettlementReport,
    tags=["Admin – Reports"],
    summary="Settlement financial summary",
)
async def report_settlements(db: AsyncSession = Depends(get_db)):
    return SettlementReport(**(await AdminDashboardService.report_settlements(db)))


# ════════════════════════════════════════════════════════════════
#  §24  AUDIT LOGS
#  Doc Ref: Docs/04_API_Documentation/12_ADMIN_API.md §24
#           Docs/05_Database/09_DATABASE_SCHEMA_PART_8_AUDIT_NOTIFICATION.md
# ════════════════════════════════════════════════════════════════


def _client_ip(request: Request) -> Optional[str]:
    """
    Best-effort IP extraction. Honor X-Forwarded-For when behind a proxy,
    otherwise fall back to the direct client host. None if we genuinely
    don't know.
    """
    if not request or not request.client:
        return None
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        # X-Forwarded-For: client, proxy1, proxy2 — leftmost is the originator.
        return forwarded.split(",")[0].strip()
    return request.client.host


def _user_agent(request: Request) -> Optional[str]:
    return request.headers.get("user-agent") if request else None


def _request_id(request: Request) -> Optional[str]:
    # Honour upstream request-id headers when present so audit rows can be
    # correlated with application logs.
    return (
        (request.headers.get("x-request-id") or request.headers.get("x-correlation-id"))
        if request
        else None
    )


def _audit_user_id(current_user: Optional[dict]) -> Optional[int]:
    """
    Pull the actor's integer id out of the JWT payload, if present.
    The token typically carries `sub` as a UUID string; the audit_logs
    user_id column is BIGINT, so we only log an id when one was encoded
    as such (rare — most admin actors are UUIDs).
    """
    if not current_user:
        return None
    raw = current_user.get("user_id") or current_user.get("actor_id")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _uuid_to_int(value: Any) -> Optional[int]:
    """
    Best-effort coercion of a UUID/string id into the BIGINT audit_logs
    entity_id column. Returns None when the value isn't a plain integer.
    UUID strings are intentionally rejected — we don't want to lose
    information by hashing them.
    """
    if value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


@router.get(
    "/audit-logs",
    response_model=AuditLogListResponse,
    tags=["Admin – Audit"],
    summary="List platform-wide audit trail with filters",
    description=(
        "Paginated read of audit_logs. Filters: user_id, action_type "
        "(e.g. PARTNER_APPROVED, LOGIN_SUCCESS), module (ILIKE), entity_name, "
        "and a date_from / date_to window on created_at."
    ),
)
async def list_audit_logs(
    user_id: Optional[int] = Query(None, description="Filter by acting user id"),
    action_type: Optional[str] = Query(
        None,
        description="Exact match on action_type (e.g. PARTNER_APPROVED, LOGIN_SUCCESS)",
    ),
    module: Optional[str] = Query(None, description="Partial match on module_name"),
    entity_name: Optional[str] = Query(None, description="Exact match on entity_name"),
    date_from: Optional[_datetime] = Query(
        None, description="Inclusive lower bound on created_at (ISO 8601)"
    ),
    date_to: Optional[_datetime] = Query(
        None, description="Inclusive upper bound on created_at (ISO 8601)"
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin()),
):
    result = await AdminDashboardService.list_audit_logs(
        db,
        user_id=user_id,
        action_type=action_type,
        module=module,
        entity_name=entity_name,
        date_from=date_from,
        date_to=date_to,
        page=page,
        page_size=page_size,
    )
    from app.modules.admin.schemas.dashboard import AuditLogOut

    items = [AuditLogOut.model_validate(i) for i in result["items"]]
    return AuditLogListResponse(
        items=items,
        total=result["total"],
        page=result["page"],
        page_size=result["page_size"],
    )


@router.get(
    "/audit-logs/stats",
    response_model=AuditLogSummary,
    tags=["Admin – Audit"],
    summary="Aggregate audit-log counts for the dashboard header strip",
)
async def audit_log_stats(
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin()),
):
    return AuditLogSummary(**(await AdminDashboardService.list_audit_logs_summary(db)))


# ════════════════════════════════════════════════════════════════
#  STAFF USER MANAGEMENT (Admin creates internal office staff)
#  Doc Ref: BRD Part 2 §12 — Admin, CCO, Verification Officer, Finance Manager
#  Doc Ref: BRD Part 8 §196 — Admin User Types (RBAC)
#  Endpoint prefix: /admin/staff
#  Separate from /admin/users which lists ALL user types
# ════════════════════════════════════════════════════════════════


@router.get(
    "/staff",
    response_model=StaffUserListResponse,
    tags=["Admin – Staff"],
    summary="List internal office staff (Admin, CCO, Verification Officer, Finance Manager)",
    description=(
        "Returns paginated staff members only — excludes Customer, Partner, Driver. "
        "Filterable by role and status. "
        "Doc Ref: BRD Part 2 §12 | BRD Part 8 §196"
    ),
)
async def list_staff(
    role: Optional[str] = Query(
        None,
        description="ADMIN | CCO | VERIFICATION_OFFICER | FINANCE_MANAGER | SUPER_ADMIN",
    ),
    status: Optional[str] = Query(None, description="ACTIVE | SUSPENDED | INACTIVE"),
    search: Optional[str] = Query(None, description="Search name, email, or mobile"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    result = await AdminDashboardService.list_staff_users(
        db, role=role, status=status, search=search, page=page, page_size=page_size
    )
    items = [StaffUserOut.model_validate(i) for i in result["items"]]
    return StaffUserListResponse(
        items=items,
        total=result["total"],
        page=result["page"],
        page_size=result["page_size"],
    )


@router.post(
    "/staff",
    response_model=StaffUserOut,
    status_code=201,
    tags=["Admin – Staff"],
    summary="Create a new staff member account",
    description=(
        "Super Admin creates an internal staff user with email + password authentication. "
        "profile_image_url is optional (upload via /admin/settings/upload-media first). "
        "Doc Ref: BRD Part 2 §14 — Email + Password login for internal staff."
    ),
)
async def create_staff(
    payload: StaffUserCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin()),
):
    try:
        user = await AdminDashboardService.create_staff_user(db, payload)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    if not user:
        raise HTTPException(status_code=500, detail="Failed to create staff user")

    # Audit — staff creation
    actor_id = _audit_user_id(current_user)
    new_user_id = _uuid_to_int(user.get("id"))
    await AuditLogger.log_staff_event(
        db,
        action_type="STAFF_CREATED",
        user_id=actor_id,
        staff_id=new_user_id,
        new_values={
            "user_type": payload.user_type,
            "email": payload.email,
            "mobile": payload.mobile_number,
        },
        ip_address=_client_ip(request),
        user_agent=_user_agent(request),
        request_id=_request_id(request),
    )
    await db.commit()
    return StaffUserOut.model_validate(user)


@router.get(
    "/staff/{user_id}",
    response_model=StaffUserOut,
    tags=["Admin – Staff"],
    summary="Get a single staff member by UUID",
)
async def get_staff(user_id: UUID, db: AsyncSession = Depends(get_db)):
    user = await AdminDashboardService.get_staff_user(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Staff user not found")
    return StaffUserOut.model_validate(user)


@router.patch(
    "/staff/{user_id}",
    response_model=StaffUserOut,
    tags=["Admin – Staff"],
    summary="Update a staff member's profile",
)
async def update_staff(
    user_id: UUID, payload: StaffUserUpdate, db: AsyncSession = Depends(get_db)
):
    user = await AdminDashboardService.update_staff_user(db, user_id, payload)
    if not user:
        raise HTTPException(status_code=404, detail="Staff user not found")
    return StaffUserOut.model_validate(user)


@router.patch(
    "/staff/{user_id}/activate",
    response_model=AdminActionResponse,
    tags=["Admin – Staff"],
    summary="Activate a staff account",
)
async def activate_staff(
    user_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin()),
):
    ok = await AdminDashboardService.set_user_status(
        db, user_id, is_active=True, status="ACTIVE"
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Staff user not found")

    actor_id = _audit_user_id(current_user)
    await AuditLogger.log_staff_event(
        db,
        action_type="STAFF_ACTIVATED",
        user_id=actor_id,
        staff_id=_uuid_to_int(user_id),
        new_values={"is_active": True, "status": "ACTIVE"},
        ip_address=_client_ip(request),
        user_agent=_user_agent(request),
        request_id=_request_id(request),
    )
    await db.commit()
    return AdminActionResponse(message="Staff member activated")


@router.patch(
    "/staff/{user_id}/suspend",
    response_model=AdminActionResponse,
    tags=["Admin – Staff"],
    summary="Suspend a staff account",
)
async def suspend_staff(
    user_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin()),
):
    ok = await AdminDashboardService.set_user_status(
        db, user_id, is_active=False, status="SUSPENDED"
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Staff user not found")

    actor_id = _audit_user_id(current_user)
    await AuditLogger.log_staff_event(
        db,
        action_type="STAFF_SUSPENDED",
        user_id=actor_id,
        staff_id=_uuid_to_int(user_id),
        new_values={"is_active": False, "status": "SUSPENDED"},
        ip_address=_client_ip(request),
        user_agent=_user_agent(request),
        request_id=_request_id(request),
    )
    await db.commit()
    return AdminActionResponse(message="Staff member suspended")


@router.post(
    "/staff/{user_id}/reset-password",
    response_model=AdminActionResponse,
    tags=["Admin – Staff"],
    summary="Reset a staff member's password",
    description="Super Admin can reset any staff member's password. Doc Ref: BRD Part 8 §210 Password Policy.",
)
async def reset_staff_password(
    user_id: UUID,
    payload: ResetPasswordRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin()),
):
    ok = await AdminDashboardService.reset_staff_password(
        db, user_id, payload.new_password
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Staff user not found")

    actor_id = _audit_user_id(current_user)
    await AuditLogger.log_staff_event(
        db,
        action_type="STAFF_PASSWORD_RESET",
        user_id=actor_id,
        staff_id=_uuid_to_int(user_id),
        new_values={"password_reset": True},
        ip_address=_client_ip(request),
        user_agent=_user_agent(request),
        request_id=_request_id(request),
    )
    await db.commit()
    return AdminActionResponse(message="Password reset successfully")


@router.post(
    "/staff/{user_id}/generate-id-card",
    response_model=StaffUserOut,
    tags=["Admin – Staff"],
    summary="Generate / regenerate staff ID card",
    description=(
        "Marks the staff member's ID card as GENERATED and records the timestamp. "
        "Optionally stores printing instructions in id_card_notes. "
        "The frontend then renders the ID card layout for print using the returned staff data. "
        "Doc Ref: DB 0014_staff_profiles — id_card_status | Employee Lifecycle §13"
    ),
)
async def generate_staff_id_card(
    user_id: UUID,
    payload: StaffIdCardGenerateRequest,
    db: AsyncSession = Depends(get_db),
):
    user = await AdminDashboardService.generate_staff_id_card(
        db, user_id, notes=payload.notes
    )
    if not user:
        raise HTTPException(
            status_code=404,
            detail="Staff user not found or profile not created yet. Please save profile first.",
        )
    return StaffUserOut.model_validate(user)


@router.patch(
    "/staff/{user_id}/profile",
    response_model=StaffUserOut,
    tags=["Admin – Staff"],
    summary="Upsert extended staff profile (address, bank, nominee, documents, ID card)",
    description=(
        "Creates or updates the staff_profiles row for the given user. "
        "All fields are optional — send only what needs updating. "
        "Doc Ref: DB 0014_staff_profiles"
    ),
)
async def upsert_staff_profile(
    user_id: UUID,
    payload: StaffProfileCreate,
    db: AsyncSession = Depends(get_db),
):
    user = await AdminDashboardService.get_staff_user(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Staff user not found")
    await AdminDashboardService._upsert_staff_profile(db, user_id, payload)
    await db.commit()
    return StaffUserOut.model_validate(
        await AdminDashboardService.get_staff_user(db, user_id)
    )


# ════════════════════════════════════════════════════════════════
#  STAFF PHOTO UPLOAD
#  POST /admin/staff/{user_id}/upload-photo
#  Uploads to Cloudinary folder waytero/staff/photos
#  Server-side transformation: c_fill,w_400,h_400,g_face (square crop, face-aware)
#  Updates users.profile_image_url with the returned secure_url
#  Doc Ref: DB 0014_staff_profiles — staff_profiles.id_card_*
#           api_integrations CLOUDINARY row for credentials
# ════════════════════════════════════════════════════════════════


def _cld_sign(params: dict, api_secret: str) -> str:
    excluded = {"file", "api_key", "resource_type", "cloud_name"}
    s = "&".join(f"{k}={v}" for k, v in sorted(params.items()) if k not in excluded)
    return _hashlib.sha1((s + api_secret).encode()).hexdigest()


async def _get_cloudinary_creds(db: AsyncSession) -> tuple[str, str, str]:
    res = await db.execute(
        _select(_ApiIntegration).where(
            _ApiIntegration.service_type == "CLOUDINARY",
            _ApiIntegration.is_active == True,  # noqa: E712
        )
    )
    intg = res.scalar_one_or_none()
    if not intg or not intg.configuration:
        raise HTTPException(
            status_code=400,
            detail="Cloudinary not configured. Go to Settings → API Integrations → Cloudinary.",
        )
    cfg = intg.configuration
    cn, ak, sk = (
        cfg.get("cloud_name", "").strip(),
        cfg.get("api_key", "").strip(),
        cfg.get("api_secret", "").strip(),
    )
    if not cn or not ak or not sk:
        raise HTTPException(
            status_code=400,
            detail="Cloudinary cloud_name / api_key / api_secret missing.",
        )
    return cn, ak, sk


async def _upload_to_cloudinary(
    cloud_name: str,
    api_key: str,
    api_secret: str,
    content: bytes,
    filename: str,
    content_type: str,
    folder: str,
    public_id: str,
    transformation: str | None = None,
    resource_type: str = "image",
) -> dict:
    ts = int(_time.time())
    params: dict = {"timestamp": ts, "folder": folder, "public_id": public_id}
    if transformation:
        params["transformation"] = transformation
    params["signature"] = _cld_sign(params, api_secret)
    params["api_key"] = api_key
    url = f"https://api.cloudinary.com/v1_1/{cloud_name}/{resource_type}/upload"
    async with _httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            url,
            data=params,
            files={
                "file": (
                    filename or "upload",
                    content,
                    content_type or "application/octet-stream",
                )
            },
        )
    if resp.status_code not in (200, 201):
        try:
            detail = resp.json().get("error", {}).get("message", resp.text)
        except Exception:
            detail = resp.text
        raise HTTPException(status_code=502, detail=f"Cloudinary error: {detail}")
    return resp.json()


@router.post(
    "/staff/{user_id}/upload-photo",
    response_model=StaffUserOut,
    tags=["Admin – Staff"],
    summary="Upload staff profile photo to Cloudinary (square crop, face-aware)",
    description=(
        "Uploads the photo to Cloudinary with transformation c_fill,w_400,h_400,g_face "
        "to produce a square face-cropped image suitable for ID cards. "
        "Updates users.profile_image_url with the resulting secure_url. "
        "Reads Cloudinary credentials from the active CLOUDINARY api_integration row. "
        "Doc Ref: DB 0014_staff_profiles | api_integrations CLOUDINARY"
    ),
)
async def upload_staff_photo(
    user_id: UUID,
    file: UploadFile = File(..., description="Profile photo (JPG/PNG/WebP, max 5 MB)"),
    db: AsyncSession = Depends(get_db),
):
    user = await AdminDashboardService.get_staff_user(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Staff user not found")

    content = await file.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Photo too large — maximum 5 MB.")

    cloud_name, api_key, api_secret = await _get_cloudinary_creds(db)

    ts = int(_time.time())
    data = await _upload_to_cloudinary(
        cloud_name,
        api_key,
        api_secret,
        content,
        file.filename or "photo.jpg",
        file.content_type or "image/jpeg",
        folder="waytero/staff/photos",
        public_id=f"staff_{user_id}_{ts}",
        transformation="c_fill,w_400,h_400,g_face",
    )

    secure_url = data.get("secure_url")
    if not secure_url:
        raise HTTPException(status_code=502, detail="Cloudinary did not return a URL.")

    # Persist to users.profile_image_url
    await db.execute(
        _update(_User).where(_User.id == user_id).values(profile_image_url=secure_url)
    )
    await db.commit()

    updated = await AdminDashboardService.get_staff_user(db, user_id)
    return StaffUserOut.model_validate(updated)


# ════════════════════════════════════════════════════════════════
#  STAFF DOCUMENT UPLOAD
#  POST /admin/staff/{user_id}/upload-document
#  Uploads any document (PAN, Aadhaar scan, offer letter, etc.)
#  to Cloudinary folder waytero/staff/documents
#  Stores record in staff_documents table
#  Returns list of all documents for this staff member
# ════════════════════════════════════════════════════════════════


@router.post(
    "/staff/{user_id}/upload-document",
    tags=["Admin – Staff"],
    summary="Upload a staff document (PAN, Aadhaar, offer letter, ID card scan, etc.)",
    description=(
        "Uploads the file to Cloudinary under waytero/staff/documents. "
        "Supports images and PDFs. Stores a row in staff_documents with document_type. "
        "document_type must be one of: PHOTO | ID_CARD_FRONT | ID_CARD_BACK | "
        "PAN_CARD | AADHAR_CARD | OFFER_LETTER | DRIVING_LICENSE | OTHER. "
        "Reads Cloudinary credentials from the active CLOUDINARY api_integration row. "
        "Doc Ref: DB 0014_staff_profiles — staff_documents table"
    ),
)
async def upload_staff_document(
    user_id: UUID,
    file: UploadFile = File(..., description="Document file (JPG/PNG/PDF, max 10 MB)"),
    document_type: str = Form(
        "OTHER",
        description="PHOTO|ID_CARD_FRONT|ID_CARD_BACK|PAN_CARD|AADHAR_CARD|OFFER_LETTER|DRIVING_LICENSE|OTHER",
    ),
    document_name: str | None = Form(None, description="Human-readable label"),
    notes: str | None = Form(None, description="Optional notes"),
    db: AsyncSession = Depends(get_db),
):
    import uuid as _uuid

    user = await AdminDashboardService.get_staff_user(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Staff user not found")

    if document_type not in STAFF_DOC_TYPES:
        raise HTTPException(
            status_code=422, detail=f"document_type must be one of {STAFF_DOC_TYPES}"
        )

    # Ensure staff_profile exists (needed for FK)
    res = await db.execute(
        _text(f"SELECT id FROM staff_profiles WHERE user_id = '{user_id}'")
    )
    profile_row = res.fetchone()
    if not profile_row:
        # Auto-create a minimal profile row
        profile_id = str(_uuid.uuid4())
        await db.execute(
            _text(
                f"""
            INSERT INTO staff_profiles (id, user_id, created_at, updated_at)
            VALUES ('{profile_id}', '{user_id}', now(), now())
            ON CONFLICT DO NOTHING
        """
            )
        )
        await db.commit()
        res2 = await db.execute(
            _text(f"SELECT id FROM staff_profiles WHERE user_id = '{user_id}'")
        )
        profile_row = res2.fetchone()

    profile_id = str(profile_row[0])

    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File too large — maximum 10 MB.")

    cloud_name, api_key, api_secret = await _get_cloudinary_creds(db)

    ts = int(_time.time())
    ct = file.content_type or "application/octet-stream"
    resource_type = "raw" if ct == "application/pdf" else "image"

    data = await _upload_to_cloudinary(
        cloud_name,
        api_key,
        api_secret,
        content,
        file.filename or "document",
        ct,
        folder="waytero/staff/documents",
        public_id=f"staff_{user_id}_{document_type.lower()}_{ts}",
        resource_type=resource_type,
    )

    secure_url = data.get("secure_url")
    if not secure_url:
        raise HTTPException(status_code=502, detail="Cloudinary did not return a URL.")

    doc_id = str(_uuid.uuid4())
    label = document_name or document_type.replace("_", " ").title()
    await db.execute(
        _text(
            f"""
        INSERT INTO staff_documents
            (id, staff_profile_id, document_type, document_name, file_url, file_type, notes, uploaded_at)
        VALUES
            ('{doc_id}', '{profile_id}', '{document_type}', :name, :url, :ftype, :notes, now())
    """
        ),
        {"name": label, "url": secure_url, "ftype": ct, "notes": notes},
    )
    await db.commit()

    # Return all documents for this staff member
    rows = await db.execute(
        _text(
            f"""
        SELECT id, document_type, document_name, file_url, file_type, is_verified, notes, uploaded_at
        FROM staff_documents WHERE staff_profile_id = '{profile_id}' ORDER BY uploaded_at DESC
    """
        )
    )
    docs = [
        {
            "id": str(r[0]),
            "document_type": r[1],
            "document_name": r[2],
            "file_url": r[3],
            "file_type": r[4],
            "is_verified": r[5],
            "notes": r[6],
            "created_at": r[7].isoformat() if r[7] else None,
        }
        for r in rows.fetchall()
    ]
    return {"documents": docs, "uploaded": {"id": doc_id, "file_url": secure_url}}


@router.get(
    "/staff/{user_id}/documents",
    tags=["Admin – Staff"],
    summary="List all uploaded documents for a staff member",
)
async def list_staff_documents(user_id: UUID, db: AsyncSession = Depends(get_db)):
    res = await db.execute(
        _text(f"SELECT id FROM staff_profiles WHERE user_id = '{user_id}'")
    )
    row = res.fetchone()
    if not row:
        return {"documents": []}
    profile_id = str(row[0])
    rows = await db.execute(
        _text(
            f"""
        SELECT id, document_type, document_name, file_url, file_type, is_verified, notes, uploaded_at
        FROM staff_documents WHERE staff_profile_id = '{profile_id}' ORDER BY uploaded_at DESC
    """
        )
    )
    docs = [
        {
            "id": str(r[0]),
            "document_type": r[1],
            "document_name": r[2],
            "file_url": r[3],
            "file_type": r[4],
            "is_verified": r[5],
            "notes": r[6],
            "created_at": r[7].isoformat() if r[7] else None,
        }
        for r in rows.fetchall()
    ]
    return {"documents": docs}


# ════════════════════════════════════════════════════════════════
#  ADMIN PARTNER — BANK ACCOUNT & LOGO MANAGEMENT
#  Doc Ref: DB Schema Part 2 §12 — partner_bank_accounts
#           DB Schema Part 2 §4  — partners.logo_url
#           API Doc §11          — POST /partners/{id}/bank-accounts
# Admin can add bank accounts and upload logo on behalf of partner
# ════════════════════════════════════════════════════════════════


class AdminBankAccountCreate(_BaseModel):
    account_holder_name: str = _Field(..., max_length=255)
    account_number_encrypted: str  # plain text from admin — stored as-is (no client-side encrypt required for admin)
    ifsc_code: str = _Field(..., max_length=20)
    bank_name: str = _Field(..., max_length=255)
    branch_name: _Optional[str] = None
    account_type: str = _Field("SAVINGS", pattern="^(SAVINGS|CURRENT)$")
    is_primary: bool = False


@router.post(
    "/partners/{partner_id}/bank-accounts",
    response_model=AdminActionResponse,
    status_code=201,
    tags=["Admin – Partners"],
    summary="Admin: Add a bank account for a partner",
    description=(
        "Allows admin to add a bank account on behalf of a partner. "
        "Doc Ref: DB Schema Part 2 §12 — partner_bank_accounts. "
        "If is_primary=True, all other accounts for this partner are unset as primary first."
    ),
)
async def admin_add_partner_bank_account(
    partner_id: int,
    payload: AdminBankAccountCreate,
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import text as _text2

    # Verify partner exists
    row = (
        await db.execute(
            _text2("SELECT id FROM partners WHERE id = :id AND deleted_at IS NULL"),
            {"id": partner_id},
        )
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Partner not found")

    # If setting as primary, unset all existing primary accounts
    if payload.is_primary:
        await db.execute(
            _text2(
                "UPDATE partner_bank_accounts SET is_primary = FALSE WHERE partner_id = :pid"
            ),
            {"pid": partner_id},
        )

    await db.execute(
        _text2(
            """
            INSERT INTO partner_bank_accounts
                (partner_id, account_holder_name, account_number_encrypted,
                 ifsc_code, bank_name, branch_name, account_type, is_primary,
                 verification_status, created_at)
            VALUES
                (:pid, :holder, :acc_num, :ifsc, :bank, :branch, :acc_type, :primary,
                 'PENDING', NOW())
        """
        ),
        {
            "pid": partner_id,
            "holder": payload.account_holder_name,
            "acc_num": payload.account_number_encrypted,
            "ifsc": payload.ifsc_code,
            "bank": payload.bank_name,
            "branch": payload.branch_name,
            "acc_type": payload.account_type,
            "primary": payload.is_primary,
        },
    )
    # Log the action
    await db.execute(
        _text2(
            """
            INSERT INTO partner_verification_logs (partner_id, action, remarks, created_at)
            VALUES (:pid, 'BANK_ACCOUNT_ADDED', 'Bank account added by admin', NOW())
        """
        ),
        {"pid": partner_id},
    )
    await db.commit()
    return AdminActionResponse(message="Bank account added successfully")


@router.patch(
    "/partners/{partner_id}/bank-accounts/{account_id}/verify",
    response_model=AdminActionResponse,
    tags=["Admin – Partners"],
    summary="Admin: Verify or reject a partner bank account",
)
async def admin_verify_partner_bank_account(
    partner_id: int,
    account_id: int,
    verification_status: str = Query(..., description="VERIFIED | REJECTED"),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import text as _text2

    if verification_status not in ("VERIFIED", "REJECTED"):
        raise HTTPException(
            status_code=422, detail="verification_status must be VERIFIED or REJECTED"
        )
    r = await db.execute(
        _text2(
            """
            UPDATE partner_bank_accounts
            SET verification_status = :vs
            WHERE id = :aid AND partner_id = :pid
        """
        ),
        {"vs": verification_status, "aid": account_id, "pid": partner_id},
    )
    if r.rowcount == 0:
        raise HTTPException(
            status_code=404, detail="Bank account not found for this partner"
        )
    await db.commit()
    return AdminActionResponse(
        message=f"Bank account {verification_status.lower()} successfully"
    )


@router.patch(
    "/partners/{partner_id}/logo",
    response_model=AdminActionResponse,
    tags=["Admin – Partners"],
    summary="Admin: Update partner logo URL after Cloudinary upload",
    description=(
        "After uploading via POST /admin/settings/upload-media?asset_type=general, "
        "call this endpoint with the returned secure_url to save it to partners.logo_url. "
        "Doc Ref: DB Schema Part 2 §4 — partners.logo_url"
    ),
)
async def admin_update_partner_logo(
    partner_id: int,
    logo_url: str = Query(
        ..., description="Cloudinary secure_url of the uploaded logo"
    ),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import text as _text2

    r = await db.execute(
        _text2(
            "UPDATE partners SET logo_url = :url, updated_at = NOW() WHERE id = :id AND deleted_at IS NULL"
        ),
        {"url": logo_url, "id": partner_id},
    )
    if r.rowcount == 0:
        raise HTTPException(status_code=404, detail="Partner not found")
    await db.commit()
    return AdminActionResponse(message="Partner logo updated successfully")


@router.post(
    "/partners/{partner_id}/upload-logo",
    response_model=AdminActionResponse,
    tags=["Admin – Partners"],
    summary="Admin: Upload partner logo directly to Cloudinary and save",
    description=(
        "Uploads logo to Cloudinary (waytero/partners/logos, c_fill w=400 h=400) "
        "and saves secure_url to partners.logo_url in one step. "
        "Doc Ref: DB Schema Part 2 §4 | api_integrations CLOUDINARY"
    ),
)
async def admin_upload_partner_logo(
    partner_id: int,
    file: UploadFile = File(..., description="Logo image (PNG/JPG/WebP, max 5 MB)"),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import text as _text2

    row = (
        await db.execute(
            _text2("SELECT id FROM partners WHERE id = :id AND deleted_at IS NULL"),
            {"id": partner_id},
        )
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Partner not found")

    content = await file.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Logo too large — maximum 5 MB.")

    cloud_name, api_key, api_secret = await _get_cloudinary_creds(db)
    ts = int(_time.time())
    data = await _upload_to_cloudinary(
        cloud_name,
        api_key,
        api_secret,
        content,
        file.filename or "logo.png",
        file.content_type or "image/png",
        folder="waytero/partners/logos",
        public_id=f"partner_{partner_id}_logo_{ts}",
        transformation="c_fill,w_400,h_400",
    )
    secure_url = data.get("secure_url")
    if not secure_url:
        raise HTTPException(status_code=502, detail="Cloudinary did not return a URL.")

    await db.execute(
        _text2(
            "UPDATE partners SET logo_url = :url, updated_at = NOW() WHERE id = :id"
        ),
        {"url": secure_url, "id": partner_id},
    )
    await db.commit()
    return AdminActionResponse(
        message="Partner logo uploaded successfully",
        **{"logo_url": secure_url} if False else {},
    )


@router.post(
    "/partners/{partner_id}/documents",
    response_model=AdminActionResponse,
    status_code=201,
    tags=["Admin – Partners"],
    summary="Admin: Upload a KYC document for a partner (Cloudinary + record)",
    description=(
        "Upload a document file to Cloudinary and create the partner_documents record in one step. "
        "Supports AADHAAR, PAN, GST_CERTIFICATE, TRADE_LICENSE, BUSINESS_REGISTRATION, BANK_PROOF, AGREEMENT. "
        "Doc Ref: DB Schema Part 2 §9-10 | partner_documents"
    ),
)
async def admin_upload_partner_document(
    partner_id: int,
    file: UploadFile = File(..., description="Document file (JPG/PNG/PDF, max 10 MB)"),
    document_type: str = Form(
        ...,
        description="AADHAAR|PAN|GST_CERTIFICATE|TRADE_LICENSE|BUSINESS_REGISTRATION|BANK_PROOF|AGREEMENT",
    ),
    document_number: _Optional[str] = Form(
        None, description="Aadhaar number, PAN, GST number, etc."
    ),
    expiry_date: _Optional[str] = Form(
        None, description="Expiry date YYYY-MM-DD (optional)"
    ),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import text as _text2

    VALID_DOC_TYPES = {
        "AADHAAR",
        "PAN",
        "GST_CERTIFICATE",
        "TRADE_LICENSE",
        "BUSINESS_REGISTRATION",
        "BANK_PROOF",
        "AGREEMENT",
    }
    if document_type not in VALID_DOC_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"document_type must be one of {sorted(VALID_DOC_TYPES)}",
        )

    row = (
        await db.execute(
            _text2("SELECT id FROM partners WHERE id = :id AND deleted_at IS NULL"),
            {"id": partner_id},
        )
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Partner not found")

    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File too large — maximum 10 MB.")

    cloud_name, api_key, api_secret = await _get_cloudinary_creds(db)
    ts = int(_time.time())
    ct = file.content_type or "application/octet-stream"
    resource_type = "raw" if "pdf" in ct else "image"
    data = await _upload_to_cloudinary(
        cloud_name,
        api_key,
        api_secret,
        content,
        file.filename or "document",
        ct,
        folder="waytero/partners/documents",
        public_id=f"partner_{partner_id}_{document_type.lower()}_{ts}",
        resource_type=resource_type,
    )
    secure_url = data.get("secure_url")
    if not secure_url:
        raise HTTPException(status_code=502, detail="Cloudinary did not return a URL.")

    # Insert document record — use document_number if the column exists
    try:
        await db.execute(
            _text2(
                """
                INSERT INTO partner_documents
                    (partner_id, document_type, document_number, file_url,
                     verification_status, expiry_date, uploaded_at)
                VALUES
                    (:pid, :dtype, :dnum, :url, 'PENDING', :expiry, NOW())
            """
            ),
            {
                "pid": partner_id,
                "dtype": document_type,
                "dnum": document_number,
                "url": secure_url,
                "expiry": expiry_date,
            },
        )
    except Exception:
        # Fallback if document_number column missing in old DB
        await db.rollback()
        await db.execute(
            _text2(
                """
                INSERT INTO partner_documents
                    (partner_id, document_type, file_url, verification_status, expiry_date, uploaded_at)
                VALUES
                    (:pid, :dtype, :url, 'PENDING', :expiry, NOW())
            """
            ),
            {
                "pid": partner_id,
                "dtype": document_type,
                "url": secure_url,
                "expiry": expiry_date,
            },
        )

    await db.execute(
        _text2(
            """
            INSERT INTO partner_verification_logs (partner_id, action, remarks, created_at)
            VALUES (:pid, 'DOCUMENT_UPLOADED', :remarks, NOW())
        """
        ),
        {"pid": partner_id, "remarks": f"Admin uploaded {document_type} document"},
    )
    await db.commit()
    return AdminActionResponse(
        message=f"{document_type} document uploaded successfully"
    )


# ════════════════════════════════════════════════════════════════
#  ADMIN: CREATE PARTNER  (POST /admin/partners)
#  Doc Ref: Partner API §3 | DB Schema Part 2 §4-5
#
#  The partner-self-registration route (/partners/register) binds
#  to the caller's JWT user_id, so admin cannot use it (409 if
#  admin already has any partner record tied to their user).
#  This endpoint creates a fresh User (type=PARTNER) +
#  Partner record without touching the admin's identity.
# ════════════════════════════════════════════════════════════════


class AdminCreatePartnerPayload(_BM2):
    partner_type: str  # INDIVIDUAL | COMPANY
    owner_name: str
    mobile: str
    business_name: _Opt2[str] = None
    email: _Opt2[str] = None
    city_id: int
    onboarding_source: _Opt2[str] = "ADMIN"
    # Logo — uploaded separately to Cloudinary before registration, URL provided here
    logo_url: _Opt2[str] = None
    # Office address — Doc Ref: BRD Part 2 §20 | Migration 0016
    # office_city_id MUST match city_id (enforced below) — BRD §20
    office_address_line_1: _Opt2[str] = None
    office_address_line_2: _Opt2[str] = None
    office_city_id: _Opt2[int] = None
    office_state_id: _Opt2[int] = None
    office_postal_code: _Opt2[str] = None
    # Tax / GST — REQUIRED for COMPANY (BRD §18, Rule 14: Company partners must provide GST details)
    gst_number: _Opt2[str] = None  # COMPANY: required | INDIVIDUAL: optional
    pan_number: _Opt2[str] = None  # PAN always recommended
    gst_legal_name: _Opt2[str] = None
    gst_trade_name: _Opt2[str] = None
    services: _Opt2[list] = None  # ["CAB","HOTEL","TOUR"] — saved to partner_services
    commission_group_id: _Opt2[int] = None  # Assign to commission group at registration


@router.post(
    "/partners",
    tags=["Admin – Partners"],
    summary="Admin: Create a new partner (user + partner record)",
    description=(
        "Creates a new PARTNER-type user account and a partner record in a single "
        "transaction. This is the correct admin-side registration flow — the "
        "self-registration route (/partners/register) ties to the caller's JWT "
        "and cannot be used by admins. "
        "Doc Ref: Partner API §3 | DB Schema Part 2 §4"
    ),
)
async def admin_create_partner(
    payload: AdminCreatePartnerPayload,
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import text as _t

    # ── 0. Enforce GST for COMPANY partners (BRD §18, Rule 14) ──────────────
    if payload.partner_type == "COMPANY" and not payload.gst_number:
        raise HTTPException(
            status_code=422,
            detail="GST Number is required for Company partners (BRD Rule 14)",
        )

    # ── 0b. Enforce office_city_id matches city_id (BRD §20) ─────────────────
    if payload.office_city_id and payload.office_city_id != payload.city_id:
        raise HTTPException(
            status_code=422,
            detail=(
                f"office_city_id ({payload.office_city_id}) must match the partner's "
                f"operating city_id ({payload.city_id}). Use the same city."
            ),
        )

    # ── 1. Validate mobile uniqueness (users table) ───────────────────────────
    exists = (
        await db.execute(
            _t("SELECT id FROM users WHERE mobile_number = :m LIMIT 1"),
            {"m": payload.mobile},
        )
    ).fetchone()
    if exists:
        raise HTTPException(
            status_code=409, detail="A user with this mobile number already exists"
        )

    # ── 2. Create user account (type = PARTNER, status = ACTIVE) ─────────────
    user_id = _uuid.uuid4()
    user_code = f"USR{_random.randint(100000, 999999)}"

    # Split owner_name into first_name / last_name for the users table (first_name NOT NULL)
    _name_parts = payload.owner_name.strip().split(" ", 1)
    _first_name = _name_parts[0]
    _last_name = _name_parts[1] if len(_name_parts) > 1 else None

    # Hash the default password using Argon2id (same as app.core.security.hash_password)
    from argon2 import PasswordHasher as _PH

    _ph = _PH(time_cost=2, memory_cost=65536, parallelism=2, hash_len=32, salt_len=16)
    _default_password_hash = _ph.hash("Waytero@15")

    await db.execute(
        _t(
            """
            INSERT INTO users
                (id, user_code, first_name, last_name,
                 mobile_number, email, user_type, status,
                 is_mobile_verified, is_email_verified, is_active,
                 failed_login_attempts, password_hash, force_password_change,
                 created_at, updated_at)
            VALUES
                (:id, :code, :first_name, :last_name,
                 :mobile, :email, 'PARTNER', 'ACTIVE',
                 TRUE, FALSE, TRUE,
                 0, :pwd_hash, TRUE,
                 NOW(), NOW())
        """
        ),
        {
            "id": user_id,
            "code": user_code,
            "first_name": _first_name,
            "last_name": _last_name,
            "mobile": payload.mobile,
            "email": payload.email,
            "pwd_hash": _default_password_hash,
        },
    )

    # ── 3. Generate unique partner code ──────────────────────────────────────
    partner_code = f"PRT-{_random.randint(100000, 999999)}"
    # ensure uniqueness
    while (
        await db.execute(
            _t("SELECT id FROM partners WHERE partner_code = :c"), {"c": partner_code}
        )
    ).fetchone():
        partner_code = f"PRT-{_random.randint(100000, 999999)}"

    # ── 4. Create partner record ─────────────────────────────────────────────
    result = await db.execute(
        _t(
            """
            INSERT INTO partners
                (uuid, user_id, partner_code, partner_type, owner_name,
                 business_name, mobile, email, city_id, status,
                 logo_url,
                 office_address_line_1, office_address_line_2,
                 office_city_id, office_state_id, office_postal_code,
                 onboarding_source, created_at, updated_at)
            VALUES
                (:uuid, :user_id, :code, :ptype, :owner,
                 :biz, :mobile, :email, :city, 'PENDING',
                 :logo_url,
                 :office_addr1, :office_addr2,
                 :office_city, :office_state, :office_postal,
                 :source, NOW(), NOW())
            RETURNING id
        """
        ),
        {
            "uuid": _uuid.uuid4(),
            "user_id": user_id,
            "code": partner_code,
            "ptype": payload.partner_type,
            "owner": payload.owner_name,
            "biz": payload.business_name,
            "mobile": payload.mobile,
            "email": payload.email,
            "city": payload.city_id,
            "logo_url": payload.logo_url,
            "office_addr1": payload.office_address_line_1,
            "office_addr2": payload.office_address_line_2,
            "office_city": payload.office_city_id or payload.city_id,
            "office_state": payload.office_state_id,
            "office_postal": payload.office_postal_code,
            "source": payload.onboarding_source or "ADMIN",
        },
    )
    partner_id = result.fetchone()[0]

    # ── 5. Auto-create partner_ratings row ───────────────────────────────────
    await db.execute(
        _t(
            """
            INSERT INTO partner_ratings (partner_id, average_rating, total_reviews, updated_at)
            VALUES (:pid, 0, 0, NOW())
        """
        ),
        {"pid": partner_id},
    )

    # ── 5b. Auto-create partner wallet (₹0 balance) ───────────────────────────
    await db.execute(
        _t(
            """
            INSERT INTO wallets (partner_id, wallet_type, available_balance,
                                 hold_balance, credit_limit, wallet_status, created_at)
            VALUES (:pid, 'PREPAID', 0, 0, 0, 'ACTIVE', NOW())
            ON CONFLICT (partner_id) DO NOTHING
        """
        ),
        {"pid": partner_id},
    )

    # ── 6. Audit log ─────────────────────────────────────────────────────────
    await db.execute(
        _t(
            """
            INSERT INTO partner_verification_logs
                (partner_id, action, remarks, created_at)
            VALUES
                (:pid, 'PARTNER_CREATED', 'Partner created by admin via admin portal', NOW())
        """
        ),
        {"pid": partner_id},
    )

    # ── 7. Save GST details — required for COMPANY, optional for INDIVIDUAL ──
    # BRD Rule 14: Company partners must provide GST details (enforced in step 0)
    if payload.gst_number or payload.pan_number:
        await db.execute(
            _t(
                """
                INSERT INTO partner_gst_details
                    (partner_id, gst_number, pan_number, legal_name, trade_name, created_at)
                VALUES
                    (:pid, :gst, :pan, :legal, :trade, NOW())
                ON CONFLICT (partner_id) DO UPDATE
                    SET gst_number  = COALESCE(EXCLUDED.gst_number, partner_gst_details.gst_number),
                        pan_number  = COALESCE(EXCLUDED.pan_number, partner_gst_details.pan_number),
                        legal_name  = COALESCE(EXCLUDED.legal_name, partner_gst_details.legal_name),
                        trade_name  = COALESCE(EXCLUDED.trade_name, partner_gst_details.trade_name)
            """
            ),
            {
                "pid": partner_id,
                "gst": (
                    payload.gst_number.strip().upper() if payload.gst_number else None
                ),
                "pan": (
                    payload.pan_number.strip().upper() if payload.pan_number else None
                ),
                "legal": payload.gst_legal_name,
                "trade": payload.gst_trade_name,
            },
        )

    # ── 8. Save selected services (dynamic — from service_types table) ────────
    if payload.services:
        from app.modules.admin.services import ServiceTypeService as _STS_reg

        valid_services = await _STS_reg.get_valid_codes(db)
        for svc in payload.services:
            if svc.upper() in valid_services:
                svc = svc.upper()  # normalise
                await db.execute(
                    _t(
                        """
                        INSERT INTO partner_services (partner_id, service_type, is_active, created_at)
                        VALUES (:pid, :svc, TRUE, NOW())
                        ON CONFLICT DO NOTHING
                    """
                    ),
                    {"pid": partner_id, "svc": svc},
                )

    # ── 9. Assign commission group if provided ────────────────────────────────
    if payload.commission_group_id:
        group_exists = (
            await db.execute(
                _t(
                    "SELECT id FROM commission_groups WHERE id = :gid AND is_active = TRUE"
                ),
                {"gid": payload.commission_group_id},
            )
        ).fetchone()
        if group_exists:
            await db.execute(
                _t(
                    """
                    INSERT INTO partner_commission_groups (partner_id, commission_group_id, assigned_at)
                    VALUES (:pid, :gid, NOW())
                """
                ),
                {"pid": partner_id, "gid": payload.commission_group_id},
            )
            await db.execute(
                _t(
                    """
                    INSERT INTO partner_verification_logs (partner_id, action, remarks, created_at)
                    VALUES (:pid, 'COMMISSION_GROUP_ASSIGNED',
                            :remarks, NOW())
                """
                ),
                {
                    "pid": partner_id,
                    "remarks": f"Commission group (id={payload.commission_group_id}) assigned at registration",
                },
            )

    await db.commit()

    return {
        "success": True,
        "message": "Partner registered successfully",
        "data": {
            "partner_id": partner_id,
            "partner_code": partner_code,
            "status": "PENDING",
            "user_id": str(user_id),
            "services": payload.services or [],
            "gst_saved": bool(payload.gst_number),
            "commission_group_id": payload.commission_group_id,
        },
    }


# ════════════════════════════════════════════════════════════════
#  §20  ADMIN VEHICLE MANAGEMENT (FULL)
#  Doc Ref: DB Schema Part 3 §12-15 | Migration 0017
#  Routes:
#    POST   /admin/vehicles                           — Admin register vehicle for partner
#    GET    /admin/vehicles/{vehicle_id}             — Full vehicle detail
#    GET    /admin/vehicles/{vehicle_id}/pricing     — Show price structure (city or default)
#    PATCH  /admin/vehicles/{vehicle_id}/status      — Status transition
#    PATCH  /admin/vehicles/{vehicle_id}/assign-officer — Assign verification officer
#    POST   /admin/vehicles/{vehicle_id}/documents   — Upload document
#    PATCH  /admin/vehicles/{vehicle_id}/documents/{doc_id}/verify — Verify document
#    POST   /admin/vehicles/{vehicle_id}/photos      — Upload vehicle photo
#    PATCH  /admin/vehicles/{vehicle_id}/photos/{photo_id}/verify  — Verify photo
# ════════════════════════════════════════════════════════════════

# ─── Pydantic schemas ────────────────────────────────────────────────────────


class _VehicleRegisterAdmin(_VBM):
    """Admin register a vehicle for a partner."""

    partner_id: int
    vehicle_category_id: int
    registration_number: str = _VField(..., max_length=50)
    vehicle_brand: _VOpt[str] = _VField(None, max_length=100)
    vehicle_model: _VOpt[str] = _VField(None, max_length=100)
    manufacturing_year: _VOpt[int] = _VField(None, ge=1990, le=2030)
    fuel_type: _VOpt[str] = _VField(
        None, pattern="^(PETROL|DIESEL|CNG|ELECTRIC|HYBRID)$"
    )
    seating_capacity: _VOpt[int] = _VField(None, ge=1, le=60)
    city_id: int
    color: _VOpt[str] = _VField(None, max_length=50)


class _DocUploadAdmin(_VBM):
    document_type: str  # RC | INSURANCE | FITNESS_CERTIFICATE | PERMIT | PUC
    file_url: str
    expiry_date: _VOpt[_date] = None


class _DocVerifyAdmin(_VBM):
    verification_status: str  # APPROVED | REJECTED
    remarks: _VOpt[str] = None


class _PhotoUploadAdmin(_VBM):
    photo_type: (
        str  # FRONT | BACK | LEFT | RIGHT | INTERIOR | ODOMETER | ENGINE | OTHER
    )
    file_url: str
    caption: _VOpt[str] = None


class _PhotoVerifyAdmin(_VBM):
    verification_status: str  # APPROVED | REJECTED
    remarks: _VOpt[str] = None


class _AssignOfficerReq(_VBM):
    officer_user_id: str  # UUID string of VERIFICATION_OFFICER user


class _StatusChangeReq(_VBM):
    status: str
    remarks: _VOpt[str] = None


# ─── Valid transitions (Doc Ref: DB Schema Part 3, Section 13) ───────────────
_V_TRANSITIONS = {
    "PENDING": {"UNDER_REVIEW", "SUSPENDED"},
    "UNDER_REVIEW": {"APPROVED", "PENDING", "SUSPENDED"},
    "APPROVED": {"ACTIVE", "SUSPENDED"},
    "ACTIVE": {"MAINTENANCE", "SUSPENDED", "INACTIVE"},
    "MAINTENANCE": {"ACTIVE", "SUSPENDED"},
    "INACTIVE": {"ACTIVE", "SUSPENDED"},
    "SUSPENDED": {"ACTIVE", "PENDING"},
}


# ─── Helper: get partner with validation ─────────────────────────────────────
async def _get_partner_with_cab_check(db, partner_id: int):
    """Return partner. Auto-links CAB service if not already active (admin vehicle registration flow)."""
    r = await db.execute(
        _vsel(_Partner).where(_Partner.id == partner_id, _Partner.deleted_at.is_(None))
    )
    partner = r.scalar_one_or_none()
    if not partner:
        raise HTTPException(status_code=404, detail="Partner not found")

    # Check for existing CAB service row (any is_active state)
    r2 = await db.execute(
        _vsel(_PartnerSvc).where(
            _PartnerSvc.partner_id == partner_id,
            _PartnerSvc.service_type == "CAB",
        )
    )
    svc = r2.scalar_one_or_none()
    if svc is None:
        # Auto-create CAB service row — admin registering vehicle implies CAB link
        new_svc = _PartnerSvc(partner_id=partner_id, service_type="CAB", is_active=True)
        db.add(new_svc)
        await db.flush()
    elif not svc.is_active:
        # Re-activate existing CAB service row
        svc.is_active = True
        await db.flush()

    return partner


async def _get_commission_group_for_partner(db, partner_id: int):
    """Return commission group assigned to partner (if any)."""
    r = await db.execute(
        _vtext(
            "SELECT cg.id, cg.group_name, cg.description FROM partner_commission_groups pcg "
            "JOIN commission_groups cg ON cg.id = pcg.commission_group_id "
            "WHERE pcg.partner_id = :pid ORDER BY pcg.assigned_at DESC LIMIT 1"
        ),
        {"pid": partner_id},
    )
    return r.mappings().first()


async def _get_pricing_for_display(db, city_id: int, vehicle_category_id: int):
    """
    Return merged pricing for city + category.
    For each trip type: city-specific rule takes priority; falls back to default if no city rule exists.
    If the city has NO rules at all, returns full default set.
    Doc Ref: DB Schema Part 3 §18 + DefaultVehiclePricingRule | BRD Part 3 §36
    """
    ALL_TRIP_TYPES = [
        "LOCAL",
        "AIRPORT_TRANSFER",
        "OUTSTATION",
        "ONE_WAY",
        "ROUND_TRIP",
    ]

    # Fetch city-specific rules for this category
    r = await db.execute(
        _vsel(_VPricingRule).where(
            _VPricingRule.city_id == city_id,
            _VPricingRule.vehicle_category_id == vehicle_category_id,
        )
    )
    city_rules = r.scalars().all()
    city_map = {x.trip_type: x for x in city_rules}

    # Fetch platform defaults for this category
    r2 = await db.execute(
        _vsel(_DefaultPricing).where(
            _DefaultPricing.vehicle_category_id == vehicle_category_id
        )
    )
    default_rules = r2.scalars().all()
    default_map = {x.trip_type: x for x in default_rules}

    def _rule_dict(x, source: str) -> dict:
        return {
            "trip_type": x.trip_type,
            "base_fare": float(x.base_fare or 0),
            "per_km_rate": float(x.per_km_rate or 0),
            "minimum_km": x.minimum_km,
            "driver_allowance": float(x.driver_allowance or 0),
            "night_charge": float(x.night_charge or 0),
            "driver_allowance_type": x.driver_allowance_type or "PER_TRIP",
            "source": source,
        }

    if not city_map and not default_map:
        return {"source": "default", "rules": []}

    if not city_map:
        # No city rules at all — full default set
        return {
            "source": "default",
            "rules": [_rule_dict(x, "default") for x in default_rules],
        }

    # Merge: for each known trip type, city rule wins; else fall back to default
    merged = []
    has_any_city = False
    for trip_type in ALL_TRIP_TYPES:
        if trip_type in city_map:
            merged.append(_rule_dict(city_map[trip_type], "city"))
            has_any_city = True
        elif trip_type in default_map:
            merged.append(_rule_dict(default_map[trip_type], "default"))
        # else: trip type not configured anywhere — skip

    # Also include any city rules for trip types not in ALL_TRIP_TYPES (future-proofing)
    for trip_type, rule in city_map.items():
        if trip_type not in ALL_TRIP_TYPES:
            merged.append(_rule_dict(rule, "city"))
            has_any_city = True

    source_label = "city" if has_any_city else "default"
    return {"source": source_label, "rules": merged}


# ════════════════════════════════════════════════════════════════
#  ENDPOINT: Admin Register Vehicle
# ════════════════════════════════════════════════════════════════


@router.post(
    "/vehicles/register",
    tags=["Admin – Vehicles"],
    summary="Admin: Register a vehicle for a partner (with validations)",
    status_code=201,
)
async def admin_register_vehicle(
    payload: _VehicleRegisterAdmin,
    db: AsyncSession = Depends(get_db),
):
    """
    Admin registers a vehicle for a partner.
    Guards:
    1. Partner must exist
    2. Partner must have CAB service linked
    3. Registration number must be unique
    4. Vehicle category must exist
    Returns vehicle + pricing structure for display.
    """
    partner = await _get_partner_with_cab_check(db, payload.partner_id)

    # Check duplicate registration
    r = await db.execute(
        _vsel(_Vehicle).where(
            _Vehicle.registration_number == payload.registration_number.upper()
        )
    )
    if r.scalar_one_or_none():
        raise HTTPException(
            status_code=409,
            detail=f"Registration number '{payload.registration_number}' already exists",
        )

    # Validate category
    r2 = await db.execute(
        _vsel(_VehicleCat).where(_VehicleCat.id == payload.vehicle_category_id)
    )
    cat = r2.scalar_one_or_none()
    if not cat:
        raise HTTPException(status_code=404, detail="Vehicle category not found")

    import random as _rnd
    import uuid as _uuid2

    code = f"VHL-{_rnd.randint(100000, 999999)}"

    vehicle = _Vehicle(
        uuid=_uuid2.uuid4(),
        partner_id=payload.partner_id,
        vehicle_category_id=payload.vehicle_category_id,
        vehicle_code=code,
        registration_number=payload.registration_number.upper(),
        vehicle_brand=payload.vehicle_brand,
        vehicle_model=payload.vehicle_model,
        manufacturing_year=payload.manufacturing_year,
        fuel_type=payload.fuel_type,
        seating_capacity=payload.seating_capacity,
        city_id=payload.city_id,
        status="PENDING",
    )
    db.add(vehicle)
    await db.flush()
    await db.refresh(vehicle)

    # Get pricing for display
    pricing = await _get_pricing_for_display(
        db, payload.city_id, payload.vehicle_category_id
    )
    commission_group = await _get_commission_group_for_partner(db, payload.partner_id)

    return {
        "vehicle_id": vehicle.id,
        "vehicle_code": vehicle.vehicle_code,
        "uuid": str(vehicle.uuid),
        "registration_number": vehicle.registration_number,
        "status": vehicle.status,
        "partner_id": vehicle.partner_id,
        "partner_name": partner.owner_name,
        "vehicle_category": cat.category_name,
        "city_id": vehicle.city_id,
        "pricing_structure": pricing,
        "commission_group": dict(commission_group) if commission_group else None,
    }


# ════════════════════════════════════════════════════════════════
#  ENDPOINT: List verification officers
# ════════════════════════════════════════════════════════════════


@router.get(
    "/vehicles/verification-officers",
    tags=["Admin – Vehicles"],
    summary="List staff who are verification officers",
)
async def list_verification_officers(
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(
        _vtext(
            "SELECT id, TRIM(first_name || ' ' || COALESCE(last_name, '')) AS full_name,"
            " mobile_number AS mobile, email FROM users "
            "WHERE user_type IN ('VERIFICATION_OFFICER', 'ADMIN', 'SUPER_ADMIN') "
            "AND is_active = TRUE ORDER BY first_name"
        )
    )
    return [dict(row) for row in r.mappings().all()]


# ════════════════════════════════════════════════════════════════
#  ENDPOINT: Partners with CAB service (for vehicle register dropdown)
# ════════════════════════════════════════════════════════════════


@router.get(
    "/vehicles/partners-with-cab",
    tags=["Admin – Vehicles"],
    summary="List ACTIVE partners that have CAB service linked (for Register Vehicle dropdown)",
)
async def list_partners_with_cab(
    search: Optional[str] = Query(
        None, description="Search by name, business or mobile"
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns paginated partner list for the vehicle register form.
    - Only partners with status=ACTIVE and CAB service linked (uses EXISTS, not JOIN, so multi-service partners appear once).
    - Supports search by owner_name, business_name, mobile, partner_code.
    Doc Ref: Partner API §15, DB Schema Part 3 §12 Business Rules.
    """
    params: dict = {"limit": page_size, "offset": (page - 1) * page_size}
    search_clause = ""
    if search and search.strip():
        search_clause = (
            " AND (p.owner_name ILIKE :q OR p.business_name ILIKE :q "
            "OR p.mobile ILIKE :q OR p.partner_code ILIKE :q)"
        )
        params["q"] = f"%{search.strip()}%"

    # Show ALL active partners; include has_cab flag so frontend can display CAB status
    # Partners without CAB service are shown too — admin can inline-link CAB when registering vehicle
    base_where = "WHERE p.deleted_at IS NULL AND p.status = 'ACTIVE'" + search_clause

    total_row = await db.execute(
        _vtext(f"SELECT COUNT(*) FROM partners p {base_where}"), params
    )
    total = total_row.scalar() or 0

    rows = await db.execute(
        _vtext(
            f"SELECT p.id, p.owner_name, p.business_name, p.mobile, p.partner_code, p.city_id, p.status, "
            f"  EXISTS("
            f"    SELECT 1 FROM partner_services ps "
            f"    WHERE ps.partner_id = p.id AND ps.service_type = 'CAB' AND ps.is_active = TRUE"
            f"  ) AS has_cab, "
            f"  COALESCE(("
            f"    SELECT string_agg(ps2.service_type, ',' ORDER BY ps2.service_type) "
            f"    FROM partner_services ps2 "
            f"    WHERE ps2.partner_id = p.id AND ps2.is_active = TRUE"
            f"  ), '') AS active_services "
            f"FROM partners p {base_where} "
            f"ORDER BY has_cab DESC, p.owner_name ASC LIMIT :limit OFFSET :offset"
        ),
        params,
    )

    items = [dict(row) for row in rows.mappings().all()]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# ════════════════════════════════════════════════════════════════
#  ENDPOINT: Get Vehicle Detail (admin full view)
# ════════════════════════════════════════════════════════════════


@router.get(
    "/vehicles/{vehicle_id}/detail",
    tags=["Admin – Vehicles"],
    summary="Admin: Full vehicle detail with docs, photos, partner, pricing",
)
async def admin_get_vehicle_detail(
    vehicle_id: int,
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(
        _vsel(_Vehicle).where(_Vehicle.id == vehicle_id, _Vehicle.deleted_at.is_(None))
    )
    vehicle = r.scalar_one_or_none()
    if not vehicle:
        raise HTTPException(status_code=404, detail="Vehicle not found")

    # Docs
    r2 = await db.execute(
        _vsel(_VehicleDoc).where(_VehicleDoc.vehicle_id == vehicle_id)
    )
    docs = r2.scalars().all()

    # Photos
    photos = []
    if _VPhoto:
        r3 = await db.execute(
            _vsel(_VPhoto)
            .where(_VPhoto.vehicle_id == vehicle_id)
            .order_by(_VPhoto.uploaded_at)
        )
        photos = r3.scalars().all()

    # Partner info
    rp = await db.execute(_vsel(_Partner).where(_Partner.id == vehicle.partner_id))
    partner = rp.scalar_one_or_none()

    # Category
    rc = await db.execute(
        _vsel(_VehicleCat).where(_VehicleCat.id == vehicle.vehicle_category_id)
    )
    cat = rc.scalar_one_or_none()

    # Pricing
    pricing = await _get_pricing_for_display(
        db, vehicle.city_id, vehicle.vehicle_category_id
    )

    # Commission group
    commission_group = await _get_commission_group_for_partner(db, vehicle.partner_id)

    # Officer assignment
    officer_info = None
    if vehicle_id:
        r_off = await db.execute(
            _vtext(
                "SELECT u.id, TRIM(u.first_name || ' ' || COALESCE(u.last_name, '')) AS full_name,"
                " u.mobile_number AS mobile FROM vehicle_verification_assignments vva "
                "JOIN users u ON u.id = vva.officer_id "
                "WHERE vva.vehicle_id = :vid AND vva.is_active = TRUE LIMIT 1"
            ),
            {"vid": vehicle_id},
        )
        officer_info = r_off.mappings().first()

    return {
        "id": vehicle.id,
        "uuid": str(vehicle.uuid),
        "vehicle_code": vehicle.vehicle_code,
        "registration_number": vehicle.registration_number,
        "vehicle_brand": vehicle.vehicle_brand,
        "vehicle_model": vehicle.vehicle_model,
        "manufacturing_year": vehicle.manufacturing_year,
        "fuel_type": vehicle.fuel_type,
        "seating_capacity": vehicle.seating_capacity,
        "city_id": vehicle.city_id,
        "status": vehicle.status,
        "vehicle_category": cat.category_name if cat else None,
        "vehicle_category_id": vehicle.vehicle_category_id,
        "assigned_officer": dict(officer_info) if officer_info else None,
        "verification_remarks": (
            vehicle.verification_remarks
            if hasattr(vehicle, "verification_remarks")
            else None
        ),
        "created_at": vehicle.created_at.isoformat(),
        "updated_at": vehicle.updated_at.isoformat(),
        "partner": {
            "id": partner.id if partner else None,
            "owner_name": partner.owner_name if partner else None,
            "business_name": partner.business_name if partner else None,
            "mobile": partner.mobile if partner else None,
            "city_id": partner.city_id if partner else None,
        },
        "documents": [
            {
                "id": d.id,
                "document_type": d.document_type,
                "file_url": d.file_url,
                "expiry_date": d.expiry_date.isoformat() if d.expiry_date else None,
                "verification_status": d.verification_status,
                "uploaded_at": d.uploaded_at.isoformat(),
                "verified_at": d.verified_at.isoformat() if d.verified_at else None,
                "remarks": getattr(d, "remarks", None),
            }
            for d in docs
        ],
        "photos": [
            {
                "id": p.id,
                "photo_type": p.photo_type,
                "file_url": p.file_url,
                "caption": p.caption,
                "verification_status": p.verification_status,
                "uploaded_at": p.uploaded_at.isoformat(),
            }
            for p in photos
        ],
        "pricing_structure": pricing,
        "commission_group": dict(commission_group) if commission_group else None,
    }


# ════════════════════════════════════════════════════════════════
#  ENDPOINT: Get Pricing Structure
# ════════════════════════════════════════════════════════════════


@router.get(
    "/vehicles/pricing-preview",
    tags=["Admin – Vehicles"],
    summary="Preview price structure for city + vehicle category (used in Add Vehicle form)",
)
async def admin_vehicle_pricing_preview(
    city_id: int = Query(...),
    vehicle_category_id: int = Query(...),
    db: AsyncSession = Depends(get_db),
):
    return await _get_pricing_for_display(db, city_id, vehicle_category_id)


# ════════════════════════════════════════════════════════════════
#  ENDPOINT: Status Transition
# ════════════════════════════════════════════════════════════════


@router.patch(
    "/vehicles/{vehicle_id}/status",
    tags=["Admin – Vehicles"],
    summary="Admin: Change vehicle status (PENDING→UNDER_REVIEW→APPROVED→ACTIVE…)",
)
async def admin_vehicle_status(
    vehicle_id: int,
    payload: _StatusChangeReq,
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(
        _vsel(_Vehicle).where(_Vehicle.id == vehicle_id, _Vehicle.deleted_at.is_(None))
    )
    vehicle = r.scalar_one_or_none()
    if not vehicle:
        raise HTTPException(status_code=404, detail="Vehicle not found")

    allowed = _V_TRANSITIONS.get(vehicle.status, set())
    if payload.status not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot transition from {vehicle.status} → {payload.status}. Allowed: {sorted(allowed)}",
        )

    from datetime import datetime as _dtnow, timezone as _tz

    vehicle.status = payload.status
    vehicle.updated_at = _dtnow.now(_tz.utc)
    if hasattr(vehicle, "verification_remarks") and payload.remarks:
        vehicle.verification_remarks = payload.remarks
    if payload.status == "APPROVED":
        vehicle.approved_at = _dtnow.now(_tz.utc)
    if payload.status == "ACTIVE":
        # Upsert availability
        ra = await db.execute(
            _vsel(_VehicleAvail).where(_VehicleAvail.vehicle_id == vehicle_id)
        )
        avail = ra.scalar_one_or_none()
        if avail:
            avail.availability_status = "UNAVAILABLE"
        else:
            db.add(
                _VehicleAvail(vehicle_id=vehicle_id, availability_status="UNAVAILABLE")
            )

    await db.flush()
    return {
        "vehicle_id": vehicle_id,
        "new_status": payload.status,
        "message": f"Status updated to {payload.status}",
    }


# ════════════════════════════════════════════════════════════════
#  ENDPOINT: Assign Verification Officer
# ════════════════════════════════════════════════════════════════


@router.post(
    "/vehicles/{vehicle_id}/assign-officer",
    tags=["Admin – Vehicles"],
    summary="Admin: Assign a verification officer to a vehicle",
)
async def admin_assign_officer(
    vehicle_id: int,
    payload: _AssignOfficerReq,
    current_user: dict = Depends(require_roles("ADMIN", "SUPER_ADMIN")),
    db: AsyncSession = Depends(get_db),
):
    import uuid as _uuid3

    # Validate officer exists + has correct role
    r_user = await db.execute(
        _vtext(
            "SELECT u.id, TRIM(u.first_name || ' ' || COALESCE(u.last_name, '')) AS full_name,"
            " u.user_type FROM users u WHERE u.id = :uid AND u.is_active = TRUE"
        ),
        {"uid": payload.officer_user_id},
    )
    officer = r_user.mappings().first()
    if not officer:
        raise HTTPException(
            status_code=404, detail="Officer user not found or inactive"
        )
    if officer["user_type"] not in ("VERIFICATION_OFFICER", "ADMIN", "SUPER_ADMIN"):
        raise HTTPException(
            status_code=400, detail="User is not a Verification Officer"
        )

    # Deactivate old assignments
    await db.execute(
        _vtext(
            "UPDATE vehicle_verification_assignments SET is_active = FALSE, unassigned_at = NOW() "
            "WHERE vehicle_id = :vid AND is_active = TRUE"
        ),
        {"vid": vehicle_id},
    )

    # Update vehicle
    r = await db.execute(
        _vsel(_Vehicle).where(_Vehicle.id == vehicle_id, _Vehicle.deleted_at.is_(None))
    )
    vehicle = r.scalar_one_or_none()
    if not vehicle:
        raise HTTPException(status_code=404, detail="Vehicle not found")

    if hasattr(vehicle, "assigned_officer_id"):
        vehicle.assigned_officer_id = _uuid3.UUID(payload.officer_user_id)

    # New assignment log
    if _VVA:
        assignment = _VVA(
            vehicle_id=vehicle_id,
            officer_id=_uuid3.UUID(payload.officer_user_id),
            assigned_by=_uuid3.UUID(current_user["sub"]),
        )
        db.add(assignment)

    await db.flush()
    return {
        "message": f"Vehicle assigned to officer {officer['full_name']}",
        "officer_id": payload.officer_user_id,
        "officer_name": officer["full_name"],
    }


# ════════════════════════════════════════════════════════════════
#  ENDPOINT: Upload Vehicle Document
# ════════════════════════════════════════════════════════════════


@router.post(
    "/vehicles/{vehicle_id}/documents",
    tags=["Admin – Vehicles"],
    summary="Admin/Officer: Upload a vehicle document (RC, Insurance, etc.)",
    status_code=201,
)
async def admin_upload_vehicle_doc(
    vehicle_id: int,
    payload: _DocUploadAdmin,
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(
        _vsel(_Vehicle).where(_Vehicle.id == vehicle_id, _Vehicle.deleted_at.is_(None))
    )
    if not r.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Vehicle not found")

    doc = _VehicleDoc(
        vehicle_id=vehicle_id,
        document_type=payload.document_type,
        file_url=payload.file_url,
        expiry_date=payload.expiry_date,
        verification_status="PENDING",
    )
    db.add(doc)
    await db.flush()
    await db.refresh(doc)
    return {
        "doc_id": doc.id,
        "document_type": doc.document_type,
        "verification_status": doc.verification_status,
    }


# ════════════════════════════════════════════════════════════════
#  ENDPOINT: Verify Document
# ════════════════════════════════════════════════════════════════


@router.patch(
    "/vehicles/{vehicle_id}/documents/{doc_id}/verify",
    tags=["Admin – Vehicles"],
    summary="Admin/Officer: Verify or reject a vehicle document",
)
async def admin_verify_vehicle_doc(
    vehicle_id: int,
    doc_id: int,
    payload: _DocVerifyAdmin,
    current_user: dict = Depends(
        require_roles("VERIFICATION_OFFICER", "ADMIN", "SUPER_ADMIN")
    ),
    db: AsyncSession = Depends(get_db),
):
    from datetime import datetime as _dtz, timezone as _tzz
    import uuid as _uuid4

    r = await db.execute(
        _vsel(_VehicleDoc).where(
            _VehicleDoc.id == doc_id, _VehicleDoc.vehicle_id == vehicle_id
        )
    )
    doc = r.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    doc.verification_status = payload.verification_status
    doc.verified_at = _dtz.now(_tzz.utc)
    if hasattr(doc, "verified_by"):
        doc.verified_by = _uuid4.UUID(current_user["sub"])
    if hasattr(doc, "remarks") and payload.remarks:
        doc.remarks = payload.remarks
    await db.flush()
    return {"doc_id": doc_id, "verification_status": payload.verification_status}


# ════════════════════════════════════════════════════════════════
#  ENDPOINT: Upload Vehicle Photo
# ════════════════════════════════════════════════════════════════


@router.post(
    "/vehicles/{vehicle_id}/photos",
    tags=["Admin – Vehicles"],
    summary="Admin/Partner: Upload categorized vehicle photo",
    status_code=201,
)
async def admin_upload_vehicle_photo(
    vehicle_id: int,
    payload: _PhotoUploadAdmin,
    db: AsyncSession = Depends(get_db),
):
    if not _VPhoto:
        raise HTTPException(
            status_code=503, detail="Photo upload not available — run migration 0017"
        )

    r = await db.execute(
        _vsel(_Vehicle).where(_Vehicle.id == vehicle_id, _Vehicle.deleted_at.is_(None))
    )
    if not r.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Vehicle not found")

    photo = _VPhoto(
        vehicle_id=vehicle_id,
        photo_type=payload.photo_type,
        file_url=payload.file_url,
        caption=payload.caption,
        verification_status="PENDING",
    )
    db.add(photo)
    await db.flush()
    await db.refresh(photo)
    return {
        "photo_id": photo.id,
        "photo_type": photo.photo_type,
        "verification_status": photo.verification_status,
    }


# ════════════════════════════════════════════════════════════════
#  ENDPOINT: Verify Photo
# ════════════════════════════════════════════════════════════════


@router.patch(
    "/vehicles/{vehicle_id}/photos/{photo_id}/verify",
    tags=["Admin – Vehicles"],
    summary="Admin/Officer: Verify or reject a vehicle photo",
)
async def admin_verify_vehicle_photo(
    vehicle_id: int,
    photo_id: int,
    payload: _PhotoVerifyAdmin,
    current_user: dict = Depends(
        require_roles("VERIFICATION_OFFICER", "ADMIN", "SUPER_ADMIN")
    ),
    db: AsyncSession = Depends(get_db),
):
    if not _VPhoto:
        raise HTTPException(status_code=503, detail="Run migration 0017 first")

    from datetime import datetime as _dtph, timezone as _tzph
    import uuid as _uuid5

    r = await db.execute(
        _vsel(_VPhoto).where(_VPhoto.id == photo_id, _VPhoto.vehicle_id == vehicle_id)
    )
    photo = r.scalar_one_or_none()
    if not photo:
        raise HTTPException(status_code=404, detail="Photo not found")

    photo.verification_status = payload.verification_status
    photo.verified_at = _dtph.now(_tzph.utc)
    photo.verified_by = _uuid5.UUID(current_user["sub"])
    if payload.remarks:
        photo.remarks = payload.remarks
    await db.flush()
    return {"photo_id": photo_id, "verification_status": payload.verification_status}


# ════════════════════════════════════════════════════════════════
#  §25  ADVANCED REPORTS MODULE
#  Doc Ref: BRD Part 6 §155 — GST | BRD Part 7 — Finance Reports
#  Provides: booking summary, GST records, TDS, partner & customer reports
# ════════════════════════════════════════════════════════════════


# ── §25.1  Booking Summary Report ────────────────────────────────────────────


@router.get(
    "/reports/booking-summary",
    tags=["Admin – Reports"],
    summary="Booking summary: completed & settled, grouped by period with revenue, GST, commission",
)
async def report_booking_summary(
    period: str = Query("month", description="day | week | month | year"),
    date_from: Optional[str] = Query(
        None, description="YYYY-MM-DD start date (overrides period)"
    ),
    date_to: Optional[str] = Query(None, description="YYYY-MM-DD end date"),
    status: Optional[str] = Query(
        None, description="COMPLETED | SETTLED | ALL (default ALL)"
    ),
    partner_id: Optional[int] = Query(None, description="Filter by partner"),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns grouped booking rows: date_bucket, count, total_amount, gst_amount, commission,
    net_payable, payment_mode breakdown. Uses cab_bookings + master_bookings + settlements.
    """
    from sqlalchemy import text as _rpt

    # Date range
    if date_from and date_to:
        date_clause = "cb.created_at::date BETWEEN :df AND :dt"
        params: dict = {"df": date_from, "dt": date_to}
    else:
        trunc_map = {"day": "day", "week": "week", "month": "month", "year": "year"}
        trunc = trunc_map.get(period, "month")
        date_clause = f"cb.created_at >= DATE_TRUNC('{trunc}', NOW())"
        params = {}

    status_clause = ""
    if status and status != "ALL":
        status_clause = "AND mb.booking_status = :status"
        params["status"] = status

    partner_clause = ""
    if partner_id:
        partner_clause = "AND cba.partner_id = :partner_id"
        params["partner_id"] = partner_id

    rows = (
        (
            await db.execute(
                _rpt(
                    f"""
        SELECT
            DATE_TRUNC('day', cb.created_at)::date            AS date_bucket,
            COUNT(DISTINCT cb.id)                             AS bookings_count,
            COALESCE(SUM(cb.final_amount), 0)                 AS gross_amount,
            COALESCE(SUM(cb.gst_amount), 0)                   AS gst_amount,
            COUNT(DISTINCT cb.id) FILTER (WHERE mb.payment_status = 'PAID') AS paid_count,
            COUNT(DISTINCT cb.id) FILTER (WHERE mb.payment_status = 'PENDING') AS pending_count,
            COALESCE(SUM(s.commission_amount), 0)             AS total_commission,
            COALESCE(SUM(s.net_payable_amount), 0)            AS total_net_payable
        FROM cab_bookings cb
        JOIN master_bookings mb ON mb.id = cb.master_booking_id
        LEFT JOIN cab_booking_assignments cba ON cba.cab_booking_id = cb.id
        LEFT JOIN settlements s ON s.partner_id = cba.partner_id
            AND s.start_date <= cb.created_at::date
            AND s.end_date   >= cb.created_at::date
        WHERE {date_clause}
          AND mb.booking_status NOT IN ('DRAFT','CANCELLED')
          {status_clause}
          {partner_clause}
        GROUP BY DATE_TRUNC('day', cb.created_at)::date
        ORDER BY date_bucket DESC
        LIMIT 365
    """
                ),
                params,
            )
        )
        .mappings()
        .all()
    )

    # Summary totals
    total_r = (
        (
            await db.execute(
                _rpt(
                    f"""
        SELECT
            COUNT(DISTINCT cb.id)                  AS total_bookings,
            COALESCE(SUM(cb.final_amount), 0)      AS total_revenue,
            COALESCE(SUM(cb.gst_amount), 0)        AS total_gst,
            COALESCE(SUM(s.commission_amount), 0)  AS total_commission
        FROM cab_bookings cb
        JOIN master_bookings mb ON mb.id = cb.master_booking_id
        LEFT JOIN cab_booking_assignments cba ON cba.cab_booking_id = cb.id
        LEFT JOIN settlements s ON s.partner_id = cba.partner_id
            AND s.start_date <= cb.created_at::date
            AND s.end_date   >= cb.created_at::date
        WHERE {date_clause}
          AND mb.booking_status NOT IN ('DRAFT','CANCELLED')
          {status_clause}
          {partner_clause}
    """
                ),
                params,
            )
        )
        .mappings()
        .one()
    )

    return {
        "period": period,
        "date_from": date_from,
        "date_to": date_to,
        "summary": {
            "total_bookings": int(total_r["total_bookings"] or 0),
            "total_revenue": float(total_r["total_revenue"] or 0),
            "total_gst": float(total_r["total_gst"] or 0),
            "total_commission": float(total_r["total_commission"] or 0),
        },
        "rows": [
            {
                "date": str(r["date_bucket"]),
                "bookings_count": int(r["bookings_count"] or 0),
                "gross_amount": float(r["gross_amount"] or 0),
                "gst_amount": float(r["gst_amount"] or 0),
                "paid_count": int(r["paid_count"] or 0),
                "pending_count": int(r["pending_count"] or 0),
                "total_commission": float(r["total_commission"] or 0),
                "total_net_payable": float(r["total_net_payable"] or 0),
            }
            for r in rows
        ],
    }


# ── §25.2  GST Records ────────────────────────────────────────────────────────


@router.get(
    "/reports/gst-records",
    tags=["Admin – Reports"],
    summary="GST records for all tax invoices — filterable by month/year",
)
async def report_gst_records(
    year: int = Query(..., description="e.g. 2026"),
    month: int = Query(..., description="1–12"),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import text as _gst

    rows = (
        (
            await db.execute(
                _gst(
                    """
        SELECT
            cb.booking_number,
            cb.final_amount,
            cb.gst_rate,
            cb.gst_amount,
            cb.is_tax_invoice,
            cb.created_at::date                      AS booking_date,
            cb.created_at,
            -- Customer
            u.first_name || ' ' || COALESCE(u.last_name,'') AS customer_name,
            u.mobile_number                          AS customer_mobile,
            -- Partner
            p.owner_name                             AS partner_name,
            p.business_name,
            p.partner_type,
            pgd.gst_number                           AS partner_gst,
            -- GST Invoice
            gi.invoice_number,
            gi.cgst_amount,
            gi.sgst_amount,
            gi.igst_amount,
            gi.total_amount                          AS invoice_total,
            gi.pdf_url
        FROM cab_bookings cb
        JOIN master_bookings mb  ON mb.id = cb.master_booking_id
        JOIN customers c         ON c.id  = mb.customer_id
        JOIN users u             ON u.id  = c.user_id
        LEFT JOIN cab_booking_assignments cba ON cba.cab_booking_id = cb.id
        LEFT JOIN partners p     ON p.id  = cba.partner_id
        LEFT JOIN partner_gst_details pgd ON pgd.partner_id = p.id
        LEFT JOIN gst_invoices gi ON gi.payment_id = (
            SELECT id FROM payments WHERE master_booking_id = mb.id
            ORDER BY created_at DESC LIMIT 1
        )
        WHERE EXTRACT(YEAR  FROM cb.created_at) = :yr
          AND EXTRACT(MONTH FROM cb.created_at) = :mo
          AND mb.booking_status NOT IN ('DRAFT','CANCELLED')
        ORDER BY cb.created_at DESC
    """
                ),
                {"yr": year, "mo": month},
            )
        )
        .mappings()
        .all()
    )

    # Summary
    taxable_rows = [r for r in rows if r["is_tax_invoice"]]
    total_taxable = sum(
        float(r["final_amount"] or 0) - float(r["gst_amount"] or 0)
        for r in taxable_rows
    )
    total_gst_amt = sum(float(r["gst_amount"] or 0) for r in taxable_rows)
    total_cgst = sum(float(r["cgst_amount"] or 0) for r in taxable_rows)
    total_sgst = sum(float(r["sgst_amount"] or 0) for r in taxable_rows)
    total_igst = sum(float(r["igst_amount"] or 0) for r in taxable_rows)

    return {
        "year": year,
        "month": month,
        "summary": {
            "total_records": len(rows),
            "tax_invoice_count": len(taxable_rows),
            "total_taxable_amount": round(total_taxable, 2),
            "total_gst": round(total_gst_amt, 2),
            "total_cgst": round(total_cgst, 2),
            "total_sgst": round(total_sgst, 2),
            "total_igst": round(total_igst, 2),
        },
        "rows": [
            {
                "booking_number": r["booking_number"],
                "booking_date": str(r["booking_date"]),
                "customer_name": r["customer_name"],
                "customer_mobile": r["customer_mobile"],
                "partner_name": r["business_name"] or r["partner_name"],
                "partner_type": r["partner_type"],
                "partner_gst": r["partner_gst"],
                "taxable_amount": round(
                    float(r["final_amount"] or 0) - float(r["gst_amount"] or 0), 2
                ),
                "gst_rate": float(r["gst_rate"] or 0),
                "gst_amount": float(r["gst_amount"] or 0),
                "cgst_amount": float(r["cgst_amount"] or 0),
                "sgst_amount": float(r["sgst_amount"] or 0),
                "igst_amount": float(r["igst_amount"] or 0),
                "total_amount": float(r["final_amount"] or 0),
                "is_tax_invoice": bool(r["is_tax_invoice"]),
                "invoice_number": r["invoice_number"],
                "pdf_url": r["pdf_url"],
            }
            for r in rows
        ],
    }


# ── §25.3  TDS Records (B2B Company Partners only) ───────────────────────────


@router.get(
    "/reports/tds-records",
    tags=["Admin – Reports"],
    summary="TDS deduction records for COMPANY partners (B2B) — filterable by quarter/year",
)
async def report_tds_records(
    year: int = Query(..., description="Financial year e.g. 2026"),
    quarter: int = Query(..., description="1=Apr-Jun 2=Jul-Sep 3=Oct-Dec 4=Jan-Mar"),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import text as _tds

    # Indian financial year quarter mapping
    q_map = {1: (4, 6), 2: (7, 9), 3: (10, 12), 4: (1, 3)}
    m_start, m_end = q_map.get(quarter, (4, 6))
    # For Q4 (Jan-Mar) the year is the NEXT calendar year
    yr_start = year if quarter != 4 else year + 1
    yr_end = year if quarter != 4 else year + 1

    rows = (
        (
            await db.execute(
                _tds(
                    """
        SELECT
            p.id                                        AS partner_id,
            p.partner_code,
            p.owner_name,
            p.business_name,
            pgd.gst_number,
            pgd.pan_number,
            pgd.legal_name,
            COUNT(DISTINCT s.id)                        AS settlement_count,
            COALESCE(SUM(s.gross_amount), 0)            AS gross_paid,
            COALESCE(SUM(s.commission_amount), 0)       AS total_commission,
            COALESCE(SUM(s.commission_amount) * 0.1, 0) AS tds_deductible,
            COALESCE(SUM(s.net_payable_amount), 0)      AS net_payable
        FROM partners p
        JOIN partner_gst_details pgd ON pgd.partner_id = p.id
        LEFT JOIN settlements s ON s.partner_id = p.id
            AND EXTRACT(YEAR FROM s.created_at)  = CASE WHEN :q = 4 THEN :yr_end ELSE :yr_start END
            AND EXTRACT(MONTH FROM s.created_at) BETWEEN :m_start AND :m_end
            AND s.settlement_status = 'PAID'
        WHERE p.partner_type = 'COMPANY'
          AND p.status = 'ACTIVE'
        GROUP BY p.id, p.partner_code, p.owner_name, p.business_name,
                 pgd.gst_number, pgd.pan_number, pgd.legal_name
        ORDER BY gross_paid DESC
    """
                ),
                {
                    "q": quarter,
                    "yr_start": yr_start,
                    "yr_end": yr_end,
                    "m_start": m_start,
                    "m_end": m_end,
                },
            )
        )
        .mappings()
        .all()
    )

    total_gross = sum(float(r["gross_paid"] or 0) for r in rows)
    total_commission = sum(float(r["total_commission"] or 0) for r in rows)
    total_tds = sum(float(r["tds_deductible"] or 0) for r in rows)

    return {
        "year": year,
        "quarter": quarter,
        "quarter_label": f"Q{quarter} ({['Apr-Jun','Jul-Sep','Oct-Dec','Jan-Mar'][quarter-1]})",
        "summary": {
            "total_company_partners": len(rows),
            "total_gross_paid": round(total_gross, 2),
            "total_commission": round(total_commission, 2),
            "total_tds": round(total_tds, 2),
        },
        "rows": [
            {
                "partner_id": r["partner_id"],
                "partner_code": r["partner_code"],
                "business_name": r["business_name"] or r["owner_name"],
                "legal_name": r["legal_name"],
                "gst_number": r["gst_number"],
                "pan_number": r["pan_number"],
                "settlement_count": int(r["settlement_count"] or 0),
                "gross_paid": round(float(r["gross_paid"] or 0), 2),
                "total_commission": round(float(r["total_commission"] or 0), 2),
                "tds_deductible": round(float(r["tds_deductible"] or 0), 2),
                "net_payable": round(float(r["net_payable"] or 0), 2),
            }
            for r in rows
        ],
    }


# ── §25.4  Partner Report ─────────────────────────────────────────────────────


@router.get(
    "/reports/partner/{partner_id}",
    tags=["Admin – Reports"],
    summary="Detailed partner report: bookings, income, commission, settlements",
)
async def report_partner_detail(
    partner_id: int,
    date_from: Optional[str] = Query(None, description="YYYY-MM-DD"),
    date_to: Optional[str] = Query(None, description="YYYY-MM-DD"),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import text as _pr

    date_clause = ""
    params: dict = {"pid": partner_id}
    if date_from:
        date_clause += " AND cb.created_at::date >= :df"
        params["df"] = date_from
    if date_to:
        date_clause += " AND cb.created_at::date <= :dt"
        params["dt"] = date_to

    # Partner info
    p_row = (
        (
            await db.execute(
                _pr(
                    """
        SELECT p.id, p.partner_code, p.partner_type, p.owner_name, p.business_name,
               p.mobile, p.email, p.status, p.created_at,
               pgd.gst_number, pgd.pan_number,
               w.available_balance AS wallet_balance,
               cg.group_name AS commission_group
        FROM partners p
        LEFT JOIN partner_gst_details pgd ON pgd.partner_id = p.id
        LEFT JOIN wallets w ON w.partner_id = p.id
        LEFT JOIN partner_commission_groups pcg ON pcg.partner_id = p.id
        LEFT JOIN commission_groups cg ON cg.id = pcg.commission_group_id
        WHERE p.id = :pid
    """
                ),
                {"pid": partner_id},
            )
        )
        .mappings()
        .one_or_none()
    )

    if not p_row:
        raise HTTPException(404, "Partner not found")

    # Booking rows
    bookings = (
        (
            await db.execute(
                _pr(
                    f"""
        SELECT
            cb.booking_number,
            mb.booking_status,
            mb.payment_status,
            cb.final_amount,
            cb.gst_amount,
            cb.created_at::date   AS booking_date,
            s.commission_amount,
            s.net_payable_amount,
            s.settlement_status,
            s.settlement_number,
            u.first_name || ' ' || COALESCE(u.last_name,'') AS customer_name,
            u.mobile_number AS customer_mobile,
            d.full_name AS driver_name
        FROM cab_bookings cb
        JOIN master_bookings mb      ON mb.id  = cb.master_booking_id
        JOIN customers c             ON c.id   = mb.customer_id
        JOIN users u                 ON u.id   = c.user_id
        JOIN cab_booking_assignments cba ON cba.cab_booking_id = cb.id
        LEFT JOIN drivers d          ON d.id   = cba.driver_id
        LEFT JOIN settlement_items si ON si.booking_id = cb.id
        LEFT JOIN settlements s      ON s.id   = si.settlement_id
        WHERE cba.partner_id = :pid
          AND mb.booking_status NOT IN ('DRAFT','CANCELLED')
          {date_clause}
        ORDER BY cb.created_at DESC
        LIMIT 500
    """
                ),
                params,
            )
        )
        .mappings()
        .all()
    )

    # Financial summary
    totals = {
        "total_bookings": len(bookings),
        "total_gross": round(sum(float(r["final_amount"] or 0) for r in bookings), 2),
        "total_gst": round(sum(float(r["gst_amount"] or 0) for r in bookings), 2),
        "total_commission": round(
            sum(float(r["commission_amount"] or 0) for r in bookings), 2
        ),
        "total_net_payable": round(
            sum(float(r["net_payable_amount"] or 0) for r in bookings), 2
        ),
        "settled_count": sum(1 for r in bookings if r["settlement_status"] == "PAID"),
        "pending_count": sum(1 for r in bookings if r["settlement_status"] != "PAID"),
    }

    return {
        "partner": {
            "id": p_row["id"],
            "partner_code": p_row["partner_code"],
            "partner_type": p_row["partner_type"],
            "name": p_row["business_name"] or p_row["owner_name"],
            "mobile": p_row["mobile"],
            "email": p_row["email"],
            "status": p_row["status"],
            "gst_number": p_row["gst_number"],
            "pan_number": p_row["pan_number"],
            "wallet_balance": float(p_row["wallet_balance"] or 0),
            "commission_group": p_row["commission_group"],
        },
        "date_from": date_from,
        "date_to": date_to,
        "totals": totals,
        "bookings": [
            {
                "booking_number": r["booking_number"],
                "booking_date": str(r["booking_date"]),
                "booking_status": r["booking_status"],
                "payment_status": r["payment_status"],
                "customer_name": r["customer_name"],
                "customer_mobile": r["customer_mobile"],
                "driver_name": r["driver_name"],
                "final_amount": float(r["final_amount"] or 0),
                "gst_amount": float(r["gst_amount"] or 0),
                "commission_amount": float(r["commission_amount"] or 0),
                "net_payable": float(r["net_payable_amount"] or 0),
                "settlement_status": r["settlement_status"],
                "settlement_number": r["settlement_number"],
            }
            for r in bookings
        ],
    }


# ── §25.5  Customer Report ────────────────────────────────────────────────────


@router.get(
    "/reports/customer/{customer_mobile}",
    tags=["Admin – Reports"],
    summary="Detailed customer report: all bookings, payments, refunds",
)
async def report_customer_detail(
    customer_mobile: str,
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status: Optional[str] = Query(None, description="COMPLETED | CANCELLED | ALL"),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import text as _cr

    date_clause = ""
    params: dict = {"mobile": customer_mobile}
    if date_from:
        date_clause += " AND cb.created_at::date >= :df"
        params["df"] = date_from
    if date_to:
        date_clause += " AND cb.created_at::date <= :dt"
        params["dt"] = date_to
    status_clause = ""
    if status and status != "ALL":
        status_clause = " AND mb.booking_status = :status"
        params["status"] = status

    # Customer info
    c_row = (
        (
            await db.execute(
                _cr(
                    """
        SELECT u.id, u.first_name, u.last_name, u.mobile_number, u.email,
               u.status, u.created_at, u.user_code,
               cw.available_balance AS wallet_balance
        FROM users u
        LEFT JOIN customers c ON c.user_id = u.id
        LEFT JOIN customer_wallets cw ON cw.customer_id = c.id
        WHERE u.mobile_number = :mobile
          AND u.user_type = 'CUSTOMER'
        LIMIT 1
    """
                ),
                {"mobile": customer_mobile},
            )
        )
        .mappings()
        .one_or_none()
    )

    if not c_row:
        raise HTTPException(404, "Customer not found")

    bookings = (
        (
            await db.execute(
                _cr(
                    f"""
        SELECT
            cb.booking_number,
            mb.booking_status,
            mb.payment_status,
            cb.trip_type,
            cb.pickup_location,
            cb.drop_location,
            cb.estimated_distance,
            cb.final_amount,
            cb.gst_amount,
            cb.created_at::date     AS booking_date,
            cb.pickup_datetime,
            p.owner_name            AS partner_name,
            p.business_name,
            d.full_name             AS driver_name,
            pay.payment_method,
            pay.amount              AS paid_amount,
            r.refund_amount,
            r.refund_status
        FROM cab_bookings cb
        JOIN master_bookings mb      ON mb.id  = cb.master_booking_id
        JOIN customers c             ON c.id   = mb.customer_id
        JOIN users u                 ON u.id   = c.user_id
        LEFT JOIN cab_booking_assignments cba ON cba.cab_booking_id = cb.id
        LEFT JOIN partners p         ON p.id   = cba.partner_id
        LEFT JOIN drivers d          ON d.id   = cba.driver_id
        LEFT JOIN payments pay       ON pay.master_booking_id = mb.id
        LEFT JOIN refunds r          ON r.payment_id = pay.id
        WHERE u.mobile_number = :mobile
          {date_clause}
          {status_clause}
        ORDER BY cb.created_at DESC
        LIMIT 500
    """
                ),
                params,
            )
        )
        .mappings()
        .all()
    )

    totals = {
        "total_bookings": len(bookings),
        "completed": sum(1 for r in bookings if r["booking_status"] == "COMPLETED"),
        "cancelled": sum(1 for r in bookings if r["booking_status"] == "CANCELLED"),
        "total_spent": round(sum(float(r["paid_amount"] or 0) for r in bookings), 2),
        "total_gst_paid": round(sum(float(r["gst_amount"] or 0) for r in bookings), 2),
        "total_refunded": round(
            sum(
                float(r["refund_amount"] or 0)
                for r in bookings
                if r["refund_status"] == "COMPLETED"
            ),
            2,
        ),
    }

    return {
        "customer": {
            "user_code": c_row["user_code"],
            "name": f"{c_row['first_name']} {c_row['last_name'] or ''}".strip(),
            "mobile": c_row["mobile_number"],
            "email": c_row["email"],
            "status": c_row["status"],
            "member_since": (
                str(c_row["created_at"].date()) if c_row["created_at"] else None
            ),
            "wallet_balance": float(c_row["wallet_balance"] or 0),
        },
        "totals": totals,
        "bookings": [
            {
                "booking_number": r["booking_number"],
                "booking_date": str(r["booking_date"]),
                "booking_status": r["booking_status"],
                "payment_status": r["payment_status"],
                "trip_type": r["trip_type"],
                "pickup_location": r["pickup_location"],
                "drop_location": r["drop_location"],
                "distance_km": float(r["estimated_distance"] or 0),
                "final_amount": float(r["final_amount"] or 0),
                "gst_amount": float(r["gst_amount"] or 0),
                "payment_method": r["payment_method"],
                "paid_amount": float(r["paid_amount"] or 0),
                "refund_amount": (
                    float(r["refund_amount"] or 0) if r["refund_amount"] else 0
                ),
                "refund_status": r["refund_status"],
                "partner_name": r["business_name"] or r["partner_name"],
                "driver_name": r["driver_name"],
            }
            for r in bookings
        ],
    }


# ── §25.6  Partners List for Report Selector ─────────────────────────────────


@router.get(
    "/reports/partners-list",
    tags=["Admin – Reports"],
    summary="Lightweight active partners list for report selector dropdowns",
)
async def report_partners_list(db: AsyncSession = Depends(get_db)):
    from sqlalchemy import text as _pl

    rows = (
        (
            await db.execute(
                _pl(
                    """
        SELECT id, partner_code, owner_name, business_name, partner_type, status
        FROM partners
        WHERE status IN ('ACTIVE','APPROVED')
        ORDER BY COALESCE(business_name, owner_name) ASC
        LIMIT 500
    """
                )
            )
        )
        .mappings()
        .all()
    )
    return [
        {
            "id": r["id"],
            "code": r["partner_code"],
            "name": r["business_name"] or r["owner_name"],
            "type": r["partner_type"],
            "status": r["status"],
        }
        for r in rows
    ]


# ============================================================
# EXPIRING DOCUMENTS � single feed for the dashboard widget.
# Three sources: partner_documents, driver_documents,
# hotel_documents. Each row carries its entity type so the
# frontend can render a deep-link to the right detail page.
# Doc Ref: BRD Part 4 �76 (hotel doc expiry), BRD Part 3 �23
#          (vehicle/driver docs)
# ============================================================


@router.get(
    "/dashboard/expiring-documents",
    summary="Documents (partner / driver / hotel / vehicle) expiring within N days",
)
async def list_expiring_documents(
    db: AsyncSession = Depends(get_db),
    days: int = Query(30, ge=0, le=365),
    entity_type: Optional[str] = Query(
        None, description="PARTNER | DRIVER | HOTEL | VEHICLE"
    ),
    limit: int = Query(100, ge=1, le=500),
    current_user: dict = Depends(
        require_roles("SUPER_ADMIN", "ADMIN", "FINANCE_MANAGER")
    ),
) -> dict[str, Any]:
    today = _date.today()
    horizon = today + timedelta(days=days)
    rows: list[dict[str, Any]] = []

    partner_sql = text(
        """
        SELECT 'PARTNER' AS entity_type,
               pd.id AS doc_id,
               pd.partner_id AS entity_id,
               COALESCE(NULLIF(p.business_name,''), p.owner_name) AS entity_name,
               p.partner_code AS entity_code,
               pd.document_type,
               pd.expiry_date,
               pd.verification_status
        FROM partner_documents pd
        JOIN partners p ON p.id = pd.partner_id
        WHERE pd.expiry_date IS NOT NULL
          AND pd.expiry_date <= :horizon
          AND p.deleted_at IS NULL
        """
    )
    driver_sql = text(
        """
        SELECT 'DRIVER' AS entity_type,
               dd.id AS doc_id,
               dd.driver_id AS entity_id,
               d.full_name AS entity_name,
               d.driver_code AS entity_code,
               dd.document_type,
               dd.expiry_date,
               dd.verification_status
        FROM driver_documents dd
        JOIN drivers d ON d.id = dd.driver_id
        WHERE dd.expiry_date IS NOT NULL
          AND dd.expiry_date <= :horizon
          AND d.deleted_at IS NULL
        """
    )
    hotel_sql = text(
        """
        SELECT 'HOTEL' AS entity_type,
               hd.id AS doc_id,
               hd.hotel_id AS entity_id,
               h.hotel_name AS entity_name,
               h.hotel_code AS entity_code,
               hd.document_type,
               hd.expiry_date,
               hd.verification_status
        FROM hotel_documents hd
        JOIN hotels h ON h.id = hd.hotel_id
        WHERE hd.expiry_date IS NOT NULL
          AND hd.expiry_date <= :horizon
          AND h.deleted_at IS NULL
        """
    )
    vehicle_sql = text(
        """
        SELECT 'VEHICLE' AS entity_type,
               vd.id AS doc_id,
               vd.vehicle_id AS entity_id,
               v.registration_number AS entity_name,
               v.vehicle_code AS entity_code,
               vd.document_type,
               vd.expiry_date,
               vd.verification_status
        FROM vehicle_documents vd
        JOIN vehicles v ON v.id = vd.vehicle_id
        WHERE vd.expiry_date IS NOT NULL
          AND vd.expiry_date <= :horizon
          AND v.deleted_at IS NULL
        """
    )
    params = {"horizon": horizon}
    if entity_type is None:
        union_parts = [partner_sql, driver_sql, hotel_sql, vehicle_sql]
    elif entity_type == "PARTNER":
        union_parts = [partner_sql]
    elif entity_type == "DRIVER":
        union_parts = [driver_sql]
    elif entity_type == "HOTEL":
        union_parts = [hotel_sql]
    elif entity_type == "VEHICLE":
        union_parts = [vehicle_sql]
    else:
        union_parts = []

    if union_parts:
        union_sql = " UNION ALL ".join(str(s) for s in union_parts)
        full_sql = text(f"{union_sql} ORDER BY expiry_date ASC LIMIT :limit")
        rows = [
            dict(r)
            for r in (await db.execute(full_sql, {**params, "limit": limit}))
            .mappings()
            .all()
        ]

    for r in rows:
        exp = r.get("expiry_date")
        r["expiry_date"] = exp.isoformat() if exp else None
        r["days_until_expiry"] = (exp - today).days if exp else None
        r["is_expired"] = bool(exp and exp < today)

    return {"items": rows, "total": len(rows), "days_window": days}
