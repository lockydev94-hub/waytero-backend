# ============================================================
# WAYTERO — ADMIN CAPTURE API
# File: app/modules/admin/capture_admin_api.py
# Doc Ref: Website Lead Capture §3 — Admin API; migrations 0082, 0083
# Prefix: /admin/capture  (registered in api/router.py)
#
# Queues the CCO works through for the public capture surfaces:
#   GET   /admin/capture/enquiries                 — website enquiries
#   GET   /admin/capture/enquiries/stats
#   PATCH /admin/capture/enquiries/{id}            — status + note
#   POST  /admin/capture/enquiries/{id}/quotation  — raise a quote
#   GET   /admin/capture/quotations
#   PATCH /admin/capture/quotations/{id}           — quote status
#   GET   /admin/capture/callbacks                 — callback requests
#   GET   /admin/capture/callbacks/stats
#   PATCH /admin/capture/callbacks/{id}            — mark called + note
#   GET   /admin/capture/feedback                  — guest feedback queue
#   POST  /admin/capture/feedback/{id}/approve
#   POST  /admin/capture/feedback/{id}/reject
#
# Same gate and same list envelope as admin/leads (require_roles SUPER_ADMIN /
# ADMIN / CCO). Nothing here books or registers a customer — that stays in
# /admin/customer-care, which already does it. The enquiry and callback rows
# carry `customer_found` and the admin opens the existing lookup with the
# number prefilled, so there is one booking flow, not two.
# ============================================================

from datetime import date
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import text as _text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import require_roles
from app.core.exceptions import ResourceNotFoundException, ValidationException

router = APIRouter()

ENQUIRY_STATUSES = ("NEW", "CONTACTED", "QUOTED", "BOOKED", "CLOSED")
CALLBACK_STATUSES = ("NEW", "CALLED", "NOT_PICKED", "DONE")
QUOTATION_STATUSES = ("DRAFT", "SENT", "ACCEPTED", "DECLINED", "EXPIRED")
FEEDBACK_STATUSES = ("PENDING", "APPROVED", "REJECTED")

# A callback is "still workable" until it is marked DONE, so the queue's
# default filter and the stats tiles agree on what OPEN means.
_CLOSED_STATUSES = ("CLOSED", "DONE", "EXPIRED")


def _one_of(values: tuple, field: str, value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    upper = value.strip().upper()
    if upper not in values:
        raise ValidationException(
            f"{field} must be one of {', '.join(values)}", details={"field": field}
        )
    return upper


def _paginate(total: int, page: int, page_size: int) -> dict:
    return {
        "total": int(total or 0),
        "page": page,
        "page_size": page_size,
        "total_pages": (int(total or 0) + page_size - 1) // page_size,
    }


# ── Schemas ──────────────────────────────────────────────────────


class EnquiryUpdate(BaseModel):
    status: Optional[str] = Field(None, description=" / ".join(ENQUIRY_STATUSES))
    handle_note: Optional[str] = Field(None, max_length=4000)

    def validate_all(self) -> None:
        self.status = _one_of(ENQUIRY_STATUSES, "status", self.status)


class QuotationCreate(BaseModel):
    summary: str = Field(..., min_length=2, max_length=2000)
    amount: Decimal = Field(..., gt=0, le=10000000, decimal_places=2, max_digits=12)
    service_type: Optional[str] = Field(
        None, description="Defaults to the enquiry's service type"
    )
    valid_till: Optional[date] = None
    terms: Optional[str] = Field(None, max_length=4000)
    status: Optional[str] = Field(None, description=" / ".join(QUOTATION_STATUSES))

    def validate_all(self) -> None:
        self.status = _one_of(QUOTATION_STATUSES, "status", self.status)
        self.service_type = _one_of(
            ("CAB", "HOTEL", "TOUR", "OTHER"), "service_type", self.service_type
        )


class QuotationUpdate(BaseModel):
    status: Optional[str] = Field(None, description=" / ".join(QUOTATION_STATUSES))
    summary: Optional[str] = Field(None, min_length=2, max_length=2000)
    amount: Optional[Decimal] = Field(
        None, gt=0, le=10000000, decimal_places=2, max_digits=12
    )
    valid_till: Optional[date] = None
    terms: Optional[str] = Field(None, max_length=4000)

    def validate_all(self) -> None:
        self.status = _one_of(QUOTATION_STATUSES, "status", self.status)


class CallbackUpdate(BaseModel):
    status: Optional[str] = Field(None, description=" / ".join(CALLBACK_STATUSES))
    handle_note: Optional[str] = Field(None, max_length=4000)

    def validate_all(self) -> None:
        self.status = _one_of(CALLBACK_STATUSES, "status", self.status)


class FeedbackReject(BaseModel):
    reason: str = Field(..., min_length=3, max_length=1000)


# ── Shared SQL fragments ─────────────────────────────────────────


# Is this mobile already a WayTero customer? Mirrors
# /admin/customer-care/lookup (users JOIN customers, user_type='CUSTOMER') so
# both surfaces answer the same way.
async def _customer_flags(db: AsyncSession, mobiles: list) -> dict:
    """mobile -> registered customer id, for the rows about to be returned."""
    if not mobiles:
        return {}
    rows = (
        (
            await db.execute(
                _text(
                    """
                    SELECT u.mobile_number AS mobile, c.id AS customer_id
                    FROM users u
                    JOIN customers c ON c.user_id = u.id
                    WHERE u.mobile_number = ANY(:mobiles) AND u.user_type = 'CUSTOMER'
                    """
                ),
                {"mobiles": list(set(mobiles))},
            )
        )
        .mappings()
        .all()
    )
    return {r["mobile"]: r["customer_id"] for r in rows}


# ── Enquiries ────────────────────────────────────────────────────


@router.get("/enquiries", tags=["Admin Capture"], summary="List website enquiries")
async def list_enquiries(
    status: Optional[str] = Query(None),
    service_type: Optional[str] = Query(None),
    search: Optional[str] = Query(
        None, description="Search name / mobile / email / message"
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(require_roles("SUPER_ADMIN", "ADMIN", "CCO")),
    db: AsyncSession = Depends(get_db),
):
    filters = ["1 = 1"]
    params: dict = {}
    if status:
        filters.append("e.status = :status")
        params["status"] = _one_of(ENQUIRY_STATUSES, "status", status)
    if service_type:
        filters.append("e.service_type = :service_type")
        params["service_type"] = _one_of(
            ("CAB", "HOTEL", "TOUR", "OTHER"), "service_type", service_type
        )
    if search:
        filters.append(
            "(e.first_name ILIKE :search OR e.last_name ILIKE :search"
            " OR e.mobile ILIKE :search OR e.email ILIKE :search"
            " OR e.message ILIKE :search)"
        )
        params["search"] = f"%{search.strip()}%"
    where = " AND ".join(filters)
    offset = (page - 1) * page_size

    total = (
        await db.execute(
            _text(f"SELECT COUNT(*) FROM enquiries e WHERE {where}"), params
        )
    ).scalar()

    rows = (
        (
            await db.execute(
                _text(
                    f"""
                    SELECT e.id, e.first_name, e.last_name, e.email, e.mobile,
                           e.service_type, e.message, e.status, e.handle_note,
                           e.handled_at, e.created_at, e.source,
                           au.email AS handler_email,
                           (SELECT COUNT(*) FROM quotations q
                             WHERE q.enquiry_id = e.id) AS quotation_count
                    FROM enquiries e
                    LEFT JOIN users au ON au.id = e.handled_by
                    WHERE {where}
                    ORDER BY e.created_at DESC
                    LIMIT :limit OFFSET :offset
                    """
                ),
                {**params, "limit": page_size, "offset": offset},
            )
        )
        .mappings()
        .all()
    )

    known = await _customer_flags(db, [r["mobile"] for r in rows])
    return {
        "items": [
            {
                "id": r["id"],
                "first_name": r["first_name"],
                "last_name": r["last_name"],
                "email": r["email"],
                "mobile": r["mobile"],
                "service_type": r["service_type"],
                "message": r["message"],
                "status": r["status"],
                "handle_note": r["handle_note"],
                "source": r["source"],
                "handled_at": r["handled_at"].isoformat() if r["handled_at"] else None,
                "handler_email": r["handler_email"],
                "quotation_count": int(r["quotation_count"] or 0),
                "customer_found": r["mobile"] in known,
                "customer_id": known.get(r["mobile"]),
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
        ],
        **_paginate(int(total or 0), page, page_size),
    }


@router.get(
    "/enquiries/stats", tags=["Admin Capture"], summary="Enquiry counts by status"
)
async def enquiry_stats(
    current_user: dict = Depends(require_roles("SUPER_ADMIN", "ADMIN", "CCO")),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        (
            await db.execute(
                _text("SELECT status, COUNT(*) AS c FROM enquiries GROUP BY status")
            )
        )
        .mappings()
        .all()
    )
    by_status = {r["status"]: int(r["c"]) for r in rows}
    return {
        "total": sum(by_status.values()),
        "open": sum(v for k, v in by_status.items() if k not in _CLOSED_STATUSES),
        "by_status": by_status,
    }


@router.patch(
    "/enquiries/{enquiry_id}",
    tags=["Admin Capture"],
    summary="Update enquiry status / note",
)
async def update_enquiry(
    enquiry_id: int,
    body: EnquiryUpdate,
    current_user: dict = Depends(require_roles("SUPER_ADMIN", "ADMIN", "CCO")),
    db: AsyncSession = Depends(get_db),
):
    body.validate_all()

    existing = (
        await db.execute(
            _text("SELECT id FROM enquiries WHERE id = :id FOR UPDATE"),
            {"id": enquiry_id},
        )
    ).first()
    if not existing:
        raise ResourceNotFoundException("Enquiry", enquiry_id)

    sets = ["updated_at = now()"]
    params: dict = {"id": enquiry_id}
    if body.status is not None:
        sets += ["status = :status", "handled_by = :handler", "handled_at = now()"]
        params["status"] = body.status
        params["handler"] = current_user["sub"]
    if body.handle_note is not None:
        sets.append("handle_note = :note")
        params["note"] = body.handle_note.strip()

    await db.execute(
        _text(f"UPDATE enquiries SET {', '.join(sets)} WHERE id = :id"), params
    )
    return await _read_back(
        db,
        "SELECT e.id, e.status, e.handle_note, e.handled_at, e.updated_at,"
        " au.email AS handler_email"
        " FROM enquiries e LEFT JOIN users au ON au.id = e.handled_by"
        " WHERE e.id = :id",
        enquiry_id,
    )


async def _read_back(db: AsyncSession, sql: str, entity_id: int) -> dict:
    row = (await db.execute(_text(sql), {"id": entity_id})).mappings().one()
    out = {}
    for key, value in dict(row).items():
        if hasattr(value, "isoformat"):
            out[key] = value.isoformat()
        elif hasattr(value, "__float__") and key.endswith("amount"):
            # Money leaves as a string so no client ever parses it as a float.
            out[key] = str(value)
        else:
            out[key] = value
    return out


# ── Quotations ───────────────────────────────────────────────────


@router.post(
    "/enquiries/{enquiry_id}/quotation",
    status_code=201,
    tags=["Admin Capture"],
    summary="Raise a quotation against an enquiry",
)
async def create_quotation(
    enquiry_id: int,
    body: QuotationCreate,
    current_user: dict = Depends(require_roles("SUPER_ADMIN", "ADMIN", "CCO")),
    db: AsyncSession = Depends(get_db),
):
    body.validate_all()

    enquiry = (
        (
            await db.execute(
                _text("SELECT id, service_type FROM enquiries WHERE id = :id"),
                {"id": enquiry_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if not enquiry:
        raise ResourceNotFoundException("Enquiry", enquiry_id)

    row = (
        (
            await db.execute(
                _text(
                    """
                    INSERT INTO quotations
                        (enquiry_id, service_type, summary, amount, valid_till,
                         terms, status, created_by)
                    VALUES (:enquiry_id, :service_type, :summary, :amount,
                            :valid_till, :terms, :status, :created_by)
                    RETURNING id
                    """
                ),
                {
                    "enquiry_id": enquiry_id,
                    "service_type": body.service_type or enquiry["service_type"],
                    "summary": body.summary.strip(),
                    "amount": body.amount,
                    "valid_till": body.valid_till,
                    "terms": body.terms.strip() if body.terms else None,
                    "status": body.status or "DRAFT",
                    "created_by": current_user["sub"],
                },
            )
        )
        .mappings()
        .one()
    )

    # Offering a quote is a step in the enquiry's life, not a side note.
    await db.execute(
        _text(
            "UPDATE enquiries SET status = 'QUOTED', updated_at = now()"
            " WHERE id = :id AND status IN ('NEW', 'CONTACTED')"
        ),
        {"id": enquiry_id},
    )

    return await _read_back(
        db,
        "SELECT id, enquiry_id, service_type, summary, amount, currency,"
        " valid_till, terms, status, created_at FROM quotations WHERE id = :id",
        row["id"],
    )


@router.get(
    "/quotations", tags=["Admin Capture"], summary="List quotations for an enquiry"
)
async def list_quotations(
    enquiry_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(require_roles("SUPER_ADMIN", "ADMIN", "CCO")),
    db: AsyncSession = Depends(get_db),
):
    filters = ["1 = 1"]
    params: dict = {}
    if enquiry_id is not None:
        filters.append("q.enquiry_id = :enquiry_id")
        params["enquiry_id"] = enquiry_id
    if status:
        filters.append("q.status = :status")
        params["status"] = _one_of(QUOTATION_STATUSES, "status", status)
    where = " AND ".join(filters)
    offset = (page - 1) * page_size

    total = (
        await db.execute(
            _text(f"SELECT COUNT(*) FROM quotations q WHERE {where}"), params
        )
    ).scalar()

    rows = (
        (
            await db.execute(
                _text(
                    f"""
                    SELECT q.id, q.enquiry_id, q.customer_id, q.master_booking_id,
                           q.service_type, q.summary, q.amount, q.currency,
                           q.valid_till, q.terms, q.status, q.created_at,
                           au.email AS created_by_email
                    FROM quotations q
                    LEFT JOIN users au ON au.id = q.created_by
                    WHERE {where}
                    ORDER BY q.created_at DESC
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
                **dict(r),
                "amount": str(r["amount"]),
                "valid_till": r["valid_till"].isoformat() if r["valid_till"] else None,
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
        ],
        **_paginate(int(total or 0), page, page_size),
    }


@router.patch(
    "/quotations/{quotation_id}", tags=["Admin Capture"], summary="Update a quotation"
)
async def update_quotation(
    quotation_id: int,
    body: QuotationUpdate,
    current_user: dict = Depends(require_roles("SUPER_ADMIN", "ADMIN", "CCO")),
    db: AsyncSession = Depends(get_db),
):
    body.validate_all()

    existing = (
        await db.execute(
            _text("SELECT id FROM quotations WHERE id = :id FOR UPDATE"),
            {"id": quotation_id},
        )
    ).first()
    if not existing:
        raise ResourceNotFoundException("Quotation", quotation_id)

    sets = ["updated_at = now()"]
    params: dict = {"id": quotation_id}
    for column, attr in (
        ("status", "status"),
        ("summary", "summary"),
        ("amount", "amount"),
        ("valid_till", "valid_till"),
        ("terms", "terms"),
    ):
        value = getattr(body, attr)
        if value is not None:
            sets.append(f"{column} = :{attr}")
            params[attr] = value.strip() if isinstance(value, str) else value

    await db.execute(
        _text(f"UPDATE quotations SET {', '.join(sets)} WHERE id = :id"), params
    )
    return await _read_back(
        db,
        "SELECT id, enquiry_id, service_type, summary, amount, currency,"
        " valid_till, terms, status, updated_at FROM quotations WHERE id = :id",
        quotation_id,
    )


# ── Callback requests ────────────────────────────────────────────


@router.get("/callbacks", tags=["Admin Capture"], summary="List callback requests")
async def list_callbacks(
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(None, description="Search mobile / name"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(require_roles("SUPER_ADMIN", "ADMIN", "CCO")),
    db: AsyncSession = Depends(get_db),
):
    filters = ["1 = 1"]
    params: dict = {}
    if status:
        filters.append("cb.status = :status")
        params["status"] = _one_of(CALLBACK_STATUSES, "status", status)
    if search:
        filters.append("(cb.mobile ILIKE :search OR cb.name ILIKE :search)")
        params["search"] = f"%{search.strip()}%"
    where = " AND ".join(filters)
    offset = (page - 1) * page_size

    total = (
        await db.execute(
            _text(f"SELECT COUNT(*) FROM callback_requests cb WHERE {where}"), params
        )
    ).scalar()

    rows = (
        (
            await db.execute(
                _text(
                    f"""
                    SELECT cb.id, cb.name, cb.mobile, cb.status, cb.handle_note,
                           cb.called_at, cb.created_at, cb.source,
                           au.email AS handler_email
                    FROM callback_requests cb
                    LEFT JOIN users au ON au.id = cb.handled_by
                    WHERE {where}
                    ORDER BY cb.created_at DESC
                    LIMIT :limit OFFSET :offset
                    """
                ),
                {**params, "limit": page_size, "offset": offset},
            )
        )
        .mappings()
        .all()
    )

    known = await _customer_flags(db, [r["mobile"] for r in rows])
    return {
        "items": [
            {
                "id": r["id"],
                "name": r["name"],
                "mobile": r["mobile"],
                "status": r["status"],
                "handle_note": r["handle_note"],
                "source": r["source"],
                "called_at": r["called_at"].isoformat() if r["called_at"] else None,
                "handler_email": r["handler_email"],
                # Tells the CCO before clicking whether they are calling an
                # existing customer or a brand-new prospect.
                "customer_found": r["mobile"] in known,
                "customer_id": known.get(r["mobile"]),
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
        ],
        **_paginate(int(total or 0), page, page_size),
    }


@router.get(
    "/callbacks/stats", tags=["Admin Capture"], summary="Callback counts by status"
)
async def callback_stats(
    current_user: dict = Depends(require_roles("SUPER_ADMIN", "ADMIN", "CCO")),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        (
            await db.execute(
                _text(
                    "SELECT status, COUNT(*) AS c FROM callback_requests GROUP BY status"
                )
            )
        )
        .mappings()
        .all()
    )
    by_status = {r["status"]: int(r["c"]) for r in rows}
    return {
        "total": sum(by_status.values()),
        "open": sum(v for k, v in by_status.items() if k not in _CLOSED_STATUSES),
        "by_status": by_status,
    }


@router.patch(
    "/callbacks/{callback_id}",
    tags=["Admin Capture"],
    summary="Mark a callback worked / add a note",
)
async def update_callback(
    callback_id: int,
    body: CallbackUpdate,
    current_user: dict = Depends(require_roles("SUPER_ADMIN", "ADMIN", "CCO")),
    db: AsyncSession = Depends(get_db),
):
    body.validate_all()

    existing = (
        await db.execute(
            _text("SELECT id FROM callback_requests WHERE id = :id FOR UPDATE"),
            {"id": callback_id},
        )
    ).first()
    if not existing:
        raise ResourceNotFoundException("Callback request", callback_id)

    sets = ["updated_at = now()", "handled_by = :handler"]
    params: dict = {"id": callback_id, "handler": current_user["sub"]}
    if body.status is not None:
        sets.append("status = :status")
        params["status"] = body.status
        if body.status != "NEW":
            # The first attempt is the moment worth recording; moving on to
            # DONE later must not rewrite it.
            sets.append("called_at = COALESCE(called_at, now())")
    if body.handle_note is not None:
        sets.append("handle_note = :note")
        params["note"] = body.handle_note.strip()

    await db.execute(
        _text(f"UPDATE callback_requests SET {', '.join(sets)} WHERE id = :id"), params
    )
    return await _read_back(
        db,
        "SELECT cb.id, cb.name, cb.mobile, cb.status, cb.handle_note,"
        " cb.called_at, cb.updated_at, au.email AS handler_email"
        " FROM callback_requests cb"
        " LEFT JOIN users au ON au.id = cb.handled_by"
        " WHERE cb.id = :id",
        callback_id,
    )


# ── Guest feedback queue ─────────────────────────────────────────


@router.get("/feedback", tags=["Admin Capture"], summary="List guest feedback")
async def list_feedback(
    status: Optional[str] = Query(None),
    service_type: Optional[str] = Query(None),
    search: Optional[str] = Query(
        None, description="Search booking reference / guest name / review"
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(require_roles("SUPER_ADMIN", "ADMIN", "CCO")),
    db: AsyncSession = Depends(get_db),
):
    filters = ["br.source = 'GUEST_WEB'"]
    params: dict = {}
    if status:
        filters.append("br.status = :status")
        params["status"] = _one_of(FEEDBACK_STATUSES, "status", status)
    if service_type:
        filters.append(
            "EXISTS (SELECT 1 FROM booking_services bs WHERE"
            " bs.master_booking_id = br.master_booking_id"
            " AND bs.service_type = :service_type)"
        )
        params["service_type"] = _one_of(
            ("CAB", "HOTEL", "TOUR"), "service_type", service_type
        )
    if search:
        filters.append(
            "(br.booking_ref_submitted ILIKE :search OR br.guest_name ILIKE :search"
            " OR br.review ILIKE :search)"
        )
        params["search"] = f"%{search.strip()}%"
    where = " AND ".join(filters)
    offset = (page - 1) * page_size

    total = (
        await db.execute(
            _text(f"SELECT COUNT(*) FROM booking_reviews br WHERE {where}"), params
        )
    ).scalar()

    # "Verify then show all details as booking related": the moderation row
    # carries the booking's own identity, status, money and every service line,
    # so the admin judges the feedback with the trip in front of them.
    rows = (
        (
            await db.execute(
                _text(
                    f"""
                    SELECT br.id, br.rating, br.review, br.status, br.guest_name,
                           br.guest_mobile, br.guest_email,
                           br.booking_ref_submitted, br.rejection_reason,
                           br.moderated_at, br.created_at,
                           br.master_booking_id, mb.booking_number,
                           mb.booking_status, mb.payment_status, mb.total_amount,
                           mb.journey_start_date, mb.journey_end_date,
                           mb.customer_id, mb.city_id,
                           ci.name AS city_name,
                           COALESCE(NULLIF(TRIM(COALESCE(c.first_name,'') || ' '
                               || COALESCE(c.last_name,'')), ''), 'Guest')
                               AS customer_name,
                           cu.mobile_number AS customer_mobile,
                           cu.email AS customer_email,
                           COALESCE((SELECT string_agg(bs.service_type || ':'
                                      || COALESCE(bs.service_status, 'PENDING'), ' | ')
                                     FROM booking_services bs
                                     WHERE bs.master_booking_id = br.master_booking_id),
                                    '') AS services,
                           au.email AS moderator_email
                    FROM booking_reviews br
                    JOIN master_bookings mb ON mb.id = br.master_booking_id
                    JOIN customers c        ON c.id = mb.customer_id
                    JOIN users cu           ON cu.id = c.user_id
                    LEFT JOIN cities ci     ON ci.id = mb.city_id
                    LEFT JOIN users au      ON au.id = br.moderated_by
                    WHERE {where}
                    ORDER BY br.created_at DESC
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
                "rating": r["rating"],
                "review": r["review"],
                "status": r["status"],
                "guest_name": r["guest_name"],
                "guest_mobile": r["guest_mobile"],
                "guest_email": r["guest_email"],
                "booking_ref_submitted": r["booking_ref_submitted"],
                "rejection_reason": r["rejection_reason"],
                "moderated_at": (
                    r["moderated_at"].isoformat() if r["moderated_at"] else None
                ),
                "moderator_email": r["moderator_email"],
                "created_at": r["created_at"].isoformat(),
                "booking": {
                    "master_booking_id": r["master_booking_id"],
                    "booking_number": r["booking_number"],
                    "booking_status": r["booking_status"],
                    "payment_status": r["payment_status"],
                    "total_amount": str(r["total_amount"]),
                    "journey_start_date": (
                        r["journey_start_date"].isoformat()
                        if r["journey_start_date"]
                        else None
                    ),
                    "journey_end_date": (
                        r["journey_end_date"].isoformat()
                        if r["journey_end_date"]
                        else None
                    ),
                    "city": r["city_name"],
                    "services": r["services"],
                    "customer_id": r["customer_id"],
                    "customer_name": r["customer_name"],
                    "customer_mobile": r["customer_mobile"],
                    "customer_email": r["customer_email"],
                },
            }
            for r in rows
        ],
        **_paginate(int(total or 0), page, page_size),
    }


async def _moderate(
    db: AsyncSession,
    feedback_id: int,
    status: str,
    reason: Optional[str],
    actor: str,
) -> dict:
    existing = (
        (
            await db.execute(
                _text(
                    "SELECT id FROM booking_reviews"
                    " WHERE id = :id AND source = 'GUEST_WEB' FOR UPDATE"
                ),
                {"id": feedback_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if not existing:
        raise ResourceNotFoundException("Feedback", feedback_id)

    await db.execute(
        _text(
            """
            UPDATE booking_reviews
               SET status = :status, moderated_by = :actor, moderated_at = now(),
                   rejection_reason = :reason
             WHERE id = :id
            """
        ),
        {
            "id": feedback_id,
            "status": status,
            "actor": actor,
            "reason": reason,
        },
    )
    return await _read_back(
        db,
        "SELECT id, status, moderated_at, rejection_reason FROM booking_reviews"
        " WHERE id = :id",
        feedback_id,
    )


@router.post(
    "/feedback/{feedback_id}/approve",
    tags=["Admin Capture"],
    summary="Approve guest feedback",
)
async def approve_feedback(
    feedback_id: int,
    current_user: dict = Depends(require_roles("SUPER_ADMIN", "ADMIN", "CCO")),
    db: AsyncSession = Depends(get_db),
):
    return await _moderate(db, feedback_id, "APPROVED", None, current_user["sub"])


@router.post(
    "/feedback/{feedback_id}/reject",
    tags=["Admin Capture"],
    summary="Reject guest feedback",
)
async def reject_feedback(
    feedback_id: int,
    body: FeedbackReject,
    current_user: dict = Depends(require_roles("SUPER_ADMIN", "ADMIN", "CCO")),
    db: AsyncSession = Depends(get_db),
):
    return await _moderate(
        db, feedback_id, "REJECTED", body.reason.strip(), current_user["sub"]
    )
