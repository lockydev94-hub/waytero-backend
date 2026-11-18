# ============================================================
# WAY TERO — PARTNER API ROUTER
# File: app/modules/partner/api.py
# Phase: 2 — Partner Module
# ============================================================

from uuid import UUID, uuid4
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import text as _text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.modules.partner.schemas import (
    PartnerRegisterRequest,
    PartnerProfileUpdate,
    PartnerResponse,
    PartnerDocumentUpload,
    BankAccountCreate,
    GSTDetailsCreate,
    PartnerServiceUpdate,
)
from app.modules.partner.services import PartnerService as PartnerSvc

router = APIRouter()


def get_service(db: AsyncSession = Depends(get_db)) -> PartnerSvc:
    return PartnerSvc(db=db)


# ---- Registration & Profile ----


@router.post(
    "/register",
    response_model=PartnerResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register as a partner",
)
async def register_partner(
    body: PartnerRegisterRequest,
    current_user: dict = Depends(get_current_user),
    service: PartnerSvc = Depends(get_service),
):
    return await service.register(UUID(current_user["sub"]), body)


@router.get(
    "/me",
    response_model=PartnerResponse,
    status_code=status.HTTP_200_OK,
    summary="Get my partner profile",
)
async def get_my_profile(
    current_user: dict = Depends(get_current_user),
    service: PartnerSvc = Depends(get_service),
):
    return await service.get_profile(UUID(current_user["sub"]))


@router.patch(
    "/me",
    response_model=PartnerResponse,
    status_code=status.HTTP_200_OK,
    summary="Update my partner profile",
)
async def update_profile(
    body: PartnerProfileUpdate,
    current_user: dict = Depends(get_current_user),
    service: PartnerSvc = Depends(get_service),
):
    return await service.update_profile(UUID(current_user["sub"]), body)


# ---- Documents ----


@router.post(
    "/me/documents",
    status_code=status.HTTP_201_CREATED,
    summary="Upload a KYC document",
)
async def upload_document(
    body: PartnerDocumentUpload,
    current_user: dict = Depends(get_current_user),
    service: PartnerSvc = Depends(get_service),
):
    return await service.upload_document(UUID(current_user["sub"]), body)


# ---- Bank Account ----


@router.post(
    "/me/bank-accounts",
    status_code=status.HTTP_201_CREATED,
    summary="Add a bank account",
)
async def add_bank_account(
    body: BankAccountCreate,
    current_user: dict = Depends(get_current_user),
    service: PartnerSvc = Depends(get_service),
):
    return await service.add_bank_account(UUID(current_user["sub"]), body)


# ---- GST ----


@router.post(
    "/me/gst",
    status_code=status.HTTP_201_CREATED,
    summary="Add GST details (company partners)",
)
async def add_gst_details(
    body: GSTDetailsCreate,
    current_user: dict = Depends(get_current_user),
    service: PartnerSvc = Depends(get_service),
):
    return await service.add_gst_details(UUID(current_user["sub"]), body)


# ---- Services ----


@router.post(
    "/me/services",
    status_code=status.HTTP_200_OK,
    summary="Enable or disable a service type",
)
async def toggle_service(
    body: PartnerServiceUpdate,
    current_user: dict = Depends(get_current_user),
    service: PartnerSvc = Depends(get_service),
):
    return await service.toggle_service(UUID(current_user["sub"]), body)


# ---- Dashboard ----


@router.get(
    "/me/dashboard",
    status_code=status.HTTP_200_OK,
    summary="Get partner dashboard summary — KPIs, wallet, vehicles, recent bookings, monthly chart",
)
async def get_partner_dashboard(
    months: int = Query(
        6, ge=1, le=12, description="Number of months for revenue chart"
    ),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns all data needed for the Partner Portal dashboard:
    - Partner profile + status
    - Wallet available balance
    - Vehicle counts (total, active/on-trip, pending)
    - Booking stats (total, this month, completed, cancelled, settled revenue this
      month, pending revenue this month — trips done & payment collected but not
      yet settled by admin)
    - Hotel counts + hotel reservation stats (mirror cab shape, same settled/pending split)
    - Tour booking stats (mirror cab shape, same settled/pending split) + recent tour bookings
    - Enabled services (drives frontend gating)
    - 10 most recent cab + hotel bookings assigned to this partner
    - Monthly revenue chart (last N months, combined cab + hotel across all statuses)
    """
    user_id = current_user["sub"]

    # 1. Resolve partner from user_id
    p_row = (
        (
            await db.execute(
                _text(
                    "SELECT id, partner_code, owner_name, business_name, status, logo_url FROM partners WHERE user_id = :uid AND deleted_at IS NULL"
                ),
                {"uid": user_id},
            )
        )
        .mappings()
        .one_or_none()
    )

    if not p_row:
        return {
            "partner": None,
            "wallet": {"available_balance": 0, "wallet_status": "NOT_FOUND"},
            "vehicles": {"total": 0, "active": 0, "pending": 0},
            "bookings": {
                "total": 0,
                "this_month": 0,
                "completed": 0,
                "cancelled": 0,
                "revenue_this_month": 0,
                "revenue_pending_this_month": 0,
            },
            "hotels": {"total": 0, "active": 0, "pending": 0},
            "hotel_bookings": {
                "total": 0,
                "this_month": 0,
                "completed": 0,
                "cancelled": 0,
                "revenue_this_month": 0,
                "revenue_pending_this_month": 0,
            },
            "tour_bookings": {
                "total": 0,
                "this_month": 0,
                "completed": 0,
                "cancelled": 0,
                "revenue_this_month": 0,
                "revenue_pending_this_month": 0,
            },
            "services": [],
            "recent_bookings": [],
            "recent_hotel_bookings": [],
            "recent_tour_bookings": [],
            "revenue_chart": [],
        }

    partner_id = p_row["id"]

    # 2a. Enabled services — drives UI gating on the partner portal.
    # Read here so the dashboard payload is one round-trip and the analytics
    # page can decide which panels to render without a second /partners/me call.
    svc_rows = (
        (
            await db.execute(
                _text(
                    "SELECT service_type FROM partner_services WHERE partner_id = :pid AND is_active = true"
                ),
                {"pid": partner_id},
            )
        )
        .scalars()
        .all()
    )
    services: list[str] = [str(s) for s in (svc_rows or [])]

    # 2. Wallet balance
    w_row = (
        (
            await db.execute(
                _text(
                    "SELECT available_balance, wallet_status FROM wallets WHERE partner_id = :pid"
                ),
                {"pid": partner_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    wallet = {
        "available_balance": float(w_row["available_balance"] or 0) if w_row else 0,
        "wallet_status": w_row["wallet_status"] if w_row else "NOT_FOUND",
    }

    # 3. Vehicle counts
    v_counts = (
        (
            await db.execute(
                _text(
                    """
            SELECT
                COUNT(*) FILTER (WHERE deleted_at IS NULL)                        AS total,
                COUNT(*) FILTER (WHERE status IN ('ACTIVE','ON_TRIP') AND deleted_at IS NULL) AS active,
                COUNT(*) FILTER (WHERE status = 'PENDING' AND deleted_at IS NULL) AS pending
            FROM vehicles WHERE partner_id = :pid
        """
                ),
                {"pid": partner_id},
            )
        )
        .mappings()
        .one()
    )
    vehicles = {
        "total": int(v_counts["total"] or 0),
        "active": int(v_counts["active"] or 0),
        "pending": int(v_counts["pending"] or 0),
    }

    # 4. Booking stats — via cab_booking_assignments → cab_bookings → master_bookings.
    # revenue_this_month is now strictly SETTLED (admin-cleared), and
    # revenue_pending_this_month sums COMPLETED + SETTLEMENT_PENDING (trip done,
    # payment collected, awaiting admin to settle).
    b_stats = (
        (
            await db.execute(
                _text(
                    """
            SELECT
                COUNT(DISTINCT cb.id)                                                   AS total,
                COUNT(DISTINCT cb.id) FILTER (
                    WHERE DATE_TRUNC('month', cb.created_at) = DATE_TRUNC('month', NOW())
                )                                                                       AS this_month,
                COUNT(DISTINCT cb.id) FILTER (WHERE cb.booking_status IN ('COMPLETED','SETTLEMENT_PENDING','SETTLED'))   AS completed,
                COUNT(DISTINCT cb.id) FILTER (WHERE cb.booking_status = 'CANCELLED')   AS cancelled,
                COALESCE(SUM(COALESCE(NULLIF(cb.partner_payout,0), cb.final_amount)) FILTER (
                    WHERE cb.booking_status = 'SETTLED'
                    AND DATE_TRUNC('month', cb.updated_at) = DATE_TRUNC('month', NOW())
                ), 0)                                                                   AS revenue_this_month,
                COALESCE(SUM(COALESCE(NULLIF(cb.partner_payout,0), cb.final_amount)) FILTER (
                    WHERE cb.booking_status IN ('COMPLETED','SETTLEMENT_PENDING')
                    AND DATE_TRUNC('month', cb.updated_at) = DATE_TRUNC('month', NOW())
                ), 0)                                                                   AS revenue_pending_this_month
            FROM cab_booking_assignments cba
            JOIN cab_bookings cb ON cb.id = cba.cab_booking_id
            WHERE cba.partner_id = :pid
        """
                ),
                {"pid": partner_id},
            )
        )
        .mappings()
        .one()
    )
    bookings = {
        "total": int(b_stats["total"] or 0),
        "this_month": int(b_stats["this_month"] or 0),
        "completed": int(b_stats["completed"] or 0),
        "cancelled": int(b_stats["cancelled"] or 0),
        "revenue_this_month": float(b_stats["revenue_this_month"] or 0),
        "revenue_pending_this_month": float(b_stats["revenue_pending_this_month"] or 0),
    }

    # 4a. Hotel counts (mirrors cab vehicles). Status taxonomy mirrors the cab
    # query — a hotel "ACTIVE" is the equivalent of a vehicle "ACTIVE/ON_TRIP".
    h_counts = (
        (
            await db.execute(
                _text(
                    """
            SELECT
                COUNT(*) FILTER (WHERE deleted_at IS NULL)                        AS total,
                COUNT(*) FILTER (WHERE status = 'ACTIVE'   AND deleted_at IS NULL) AS active,
                COUNT(*) FILTER (WHERE status = 'PENDING'  AND deleted_at IS NULL) AS pending
            FROM hotels WHERE partner_id = :pid
        """
                ),
                {"pid": partner_id},
            )
        )
        .mappings()
        .one()
    )
    hotels = {
        "total": int(h_counts["total"] or 0),
        "active": int(h_counts["active"] or 0),
        "pending": int(h_counts["pending"] or 0),
    }

    # 4c. Tour booking stats (mirrors cab bookings). A tour booking belongs
    # to a package, and the package belongs to the partner — so tour bookings
    # are joined through tour_packages.partner_id. Status taxonomy mirrors
    # the cab query: COMPLETED/SETTLEMENT_PENDING/SETTLED count as completed.
    # Revenue uses tb.partner_payout for SETTLED rows (frozen at settlement);
    # for in-flight COMPLETED rows we use total_amount because partner_payout
    # is 0 until settlement freezes it (same rule as hotel reservations).
    tb_stats = (
        (
            await db.execute(
                _text(
                    """
            SELECT
                COUNT(DISTINCT tb.id)                                                   AS total,
                COUNT(DISTINCT tb.id) FILTER (
                    WHERE DATE_TRUNC('month', tb.created_at) = DATE_TRUNC('month', NOW())
                )                                                                       AS this_month,
                COUNT(DISTINCT tb.id) FILTER (WHERE tb.booking_status IN ('COMPLETED','SETTLEMENT_PENDING','SETTLED'))   AS completed,
                COUNT(DISTINCT tb.id) FILTER (WHERE tb.booking_status = 'CANCELLED')   AS cancelled,
                COALESCE(SUM(tb.partner_payout) FILTER (
                    WHERE tb.booking_status = 'SETTLED'
                    AND DATE_TRUNC('month', tb.updated_at) = DATE_TRUNC('month', NOW())
                ), 0)                                                                   AS revenue_this_month,
                COALESCE(SUM(tb.total_amount) FILTER (
                    WHERE tb.booking_status = 'COMPLETED'
                    AND tb.payment_status = 'PAID'
                    AND DATE_TRUNC('month', tb.updated_at) = DATE_TRUNC('month', NOW())
                ), 0)                                                                   AS revenue_pending_this_month
            FROM tour_bookings tb
            JOIN tour_packages tp ON tp.id = tb.package_id
            WHERE tp.partner_id = :pid
        """
                ),
                {"pid": partner_id},
            )
        )
        .mappings()
        .one()
    )
    tour_bookings = {
        "total": int(tb_stats["total"] or 0),
        "this_month": int(tb_stats["this_month"] or 0),
        "completed": int(tb_stats["completed"] or 0),
        "cancelled": int(tb_stats["cancelled"] or 0),
        "revenue_this_month": float(tb_stats["revenue_this_month"] or 0),
        "revenue_pending_this_month": float(
            tb_stats["revenue_pending_this_month"] or 0
        ),
    }

    # 4b. Hotel reservation stats (mirrors cab bookings).
    # hr.total_amount is the bill grand total (includes GST, extras, overtime).
    # hr.partner_payout is the live commission-resolved payout for SETTLED rows;
    # for in-flight COMPLETED reservations we use total_amount because partner_payout
    # is 0 until settlement freezes it. payment_collected_status='PAID' is the
    # gate that admin's settlement flow checks before allowing settle.
    # actual_check_out_at closes the financial loop; updated_at fallback covers
    # in-progress rows whose billing hasn't closed yet — acceptable for KPI.
    hb_stats = (
        (
            await db.execute(
                _text(
                    """
            SELECT
                COUNT(DISTINCT hr.id)                                                              AS total,
                COUNT(DISTINCT hr.id) FILTER (
                    WHERE DATE_TRUNC('month', hr.created_at) = DATE_TRUNC('month', NOW())
                )                                                                                  AS this_month,
                COUNT(DISTINCT hr.id) FILTER (
                    WHERE hr.reservation_status IN ('COMPLETED','SETTLEMENT_PENDING','SETTLED')
                )                                                                                  AS completed,
                COUNT(DISTINCT hr.id) FILTER (
                    WHERE hr.reservation_status = 'CANCELLED'
                )                                                                                  AS cancelled,
                COALESCE(SUM(hr.partner_payout) FILTER (
                    WHERE hr.reservation_status = 'SETTLED'
                    AND DATE_TRUNC('month', COALESCE(hr.actual_check_out_at, hr.updated_at))
                        = DATE_TRUNC('month', NOW())
                ), 0)                                                                              AS revenue_this_month,
                COALESCE(SUM(hr.total_amount) FILTER (
                    WHERE hr.reservation_status = 'COMPLETED'
                    AND hr.payment_collected_status = 'PAID'
                    AND DATE_TRUNC('month', COALESCE(hr.actual_check_out_at, hr.updated_at))
                        = DATE_TRUNC('month', NOW())
                ), 0)                                                                              AS revenue_pending_this_month
            FROM hotel_reservations hr
            JOIN hotels h ON h.id = hr.hotel_id
            WHERE h.partner_id = :pid AND h.deleted_at IS NULL
        """
                ),
                {"pid": partner_id},
            )
        )
        .mappings()
        .one()
    )
    hotel_bookings = {
        "total": int(hb_stats["total"] or 0),
        "this_month": int(hb_stats["this_month"] or 0),
        "completed": int(hb_stats["completed"] or 0),
        "cancelled": int(hb_stats["cancelled"] or 0),
        "revenue_this_month": float(hb_stats["revenue_this_month"] or 0),
        "revenue_pending_this_month": float(
            hb_stats["revenue_pending_this_month"] or 0
        ),
    }

    # 5. Recent bookings (last 10)
    # customers has no mobile column — mobile lives on users table (joined via customers.user_id)
    #
    # cab_booking_assignments has NO created_at column. Lifecycle timestamps
    # are assigned_at / accepted_at / rejected_at. A booking reassigned to the
    # same partner (after a previous REJECTED attempt) yields multiple cba
    # rows for the same cab_booking_id, which — joined naively — duplicates
    # booking_number in the result. We dedupe to one row per cab_booking,
    # picking the partner's most recent activity: prefer accepted_at when
    # present (the partner actively took the trip), else assigned_at
    # (still pending acceptance).
    recent_rows = (
        (
            await db.execute(
                _text(
                    """
            SELECT
                cb.booking_number,
                cb.booking_status,
                cb.trip_type,
                cb.pickup_location,
                cb.drop_location,
                cb.pickup_datetime,
                cb.partner_payout,
                cb.final_amount,
                mb.journey_start_date,
                COALESCE(NULLIF(TRIM(COALESCE(c.first_name,'') || ' ' || COALESCE(c.last_name,'')), ''), 'Unknown') AS customer_name,
                u.mobile_number AS customer_mobile
            FROM (
                SELECT DISTINCT ON (cba.cab_booking_id)
                    cba.cab_booking_id,
                    COALESCE(cba.accepted_at, cba.assigned_at) AS assignment_activity_at
                FROM cab_booking_assignments cba
                WHERE cba.partner_id = :pid
                ORDER BY cba.cab_booking_id, COALESCE(cba.accepted_at, cba.assigned_at) DESC
            ) latest_cba
            JOIN cab_bookings cb ON cb.id = latest_cba.cab_booking_id
            JOIN master_bookings mb ON mb.id = cb.master_booking_id
            JOIN customers c ON c.id = mb.customer_id
            JOIN users u ON u.id = c.user_id
            ORDER BY cb.created_at DESC
            LIMIT 10
        """
                ),
                {"pid": partner_id},
            )
        )
        .mappings()
        .all()
    )

    recent_bookings = [
        {
            "booking_number": r["booking_number"],
            "booking_status": r["booking_status"],
            "trip_type": r["trip_type"],
            "pickup_location": r["pickup_location"],
            "drop_location": r["drop_location"],
            "pickup_datetime": (
                r["pickup_datetime"].isoformat() if r["pickup_datetime"] else None
            ),
            "partner_payout": float(r["partner_payout"] or 0),
            "final_amount": float(r["final_amount"] or 0),
            "journey_start_date": (
                r["journey_start_date"].isoformat() if r["journey_start_date"] else None
            ),
            "customer_name": r["customer_name"],
            "customer_mobile": r["customer_mobile"],
        }
        for r in recent_rows
    ]

    # 5a. Recent hotel reservations (last 10, mirrors cab recent_bookings).
    # Frontend merges these with recent_bookings for the combined table view.
    recent_hotel_rows = (
        (
            await db.execute(
                _text(
                    """
            SELECT
                hr.reservation_number,
                hr.reservation_status,
                hr.check_in_date,
                hr.check_out_date,
                hr.total_amount,
                hr.rooms_count,
                hr.adults_count,
                hr.created_at,
                h.hotel_name,
                COALESCE(NULLIF(TRIM(COALESCE(c.first_name,'') || ' ' || COALESCE(c.last_name,'')), ''), 'Unknown') AS customer_name,
                u.mobile_number AS customer_mobile
            FROM hotel_reservations hr
            JOIN hotels h ON h.id = hr.hotel_id
            JOIN master_bookings mb ON mb.id = hr.master_booking_id
            JOIN customers c ON c.id = mb.customer_id
            JOIN users u ON u.id = c.user_id
            WHERE h.partner_id = :pid AND h.deleted_at IS NULL
            ORDER BY hr.created_at DESC
            LIMIT 10
        """
                ),
                {"pid": partner_id},
            )
        )
        .mappings()
        .all()
    )

    recent_hotel_bookings = [
        {
            "reservation_number": r["reservation_number"],
            "reservation_status": r["reservation_status"],
            "hotel_name": r["hotel_name"],
            "check_in_date": (
                r["check_in_date"].isoformat() if r["check_in_date"] else None
            ),
            "check_out_date": (
                r["check_out_date"].isoformat() if r["check_out_date"] else None
            ),
            "total_amount": float(r["total_amount"] or 0),
            "rooms_count": (
                int(r["rooms_count"]) if r["rooms_count"] is not None else None
            ),
            "adults_count": (
                int(r["adults_count"]) if r["adults_count"] is not None else None
            ),
            "customer_name": r["customer_name"],
            "customer_mobile": r["customer_mobile"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in recent_hotel_rows
    ]

    # 5b. Recent tour bookings (last 10, mirrors cab recent_bookings).
    # Frontend merges these with recent_bookings + recent_hotel_bookings for
    # the combined table view.
    recent_tour_rows = (
        (
            await db.execute(
                _text(
                    """
            SELECT
                tb.booking_number,
                tb.booking_status,
                tb.travel_start_date,
                tb.travel_end_date,
                tb.persons_count,
                tb.total_amount,
                tb.partner_payout,
                tb.created_at,
                tp.package_name,
                tp.destination,
                COALESCE(NULLIF(TRIM(COALESCE(c.first_name,'') || ' ' || COALESCE(c.last_name,'')), ''), 'Unknown') AS customer_name,
                u.mobile_number AS customer_mobile
            FROM tour_bookings tb
            JOIN tour_packages tp ON tp.id = tb.package_id
            JOIN master_bookings mb ON mb.id = tb.master_booking_id
            JOIN customers c ON c.id = mb.customer_id
            JOIN users u ON u.id = c.user_id
            WHERE tp.partner_id = :pid
            ORDER BY tb.created_at DESC
            LIMIT 10
        """
                ),
                {"pid": partner_id},
            )
        )
        .mappings()
        .all()
    )

    recent_tour_bookings = [
        {
            "booking_number": r["booking_number"],
            "booking_status": r["booking_status"],
            "package_name": r["package_name"],
            "destination": r["destination"],
            "travel_start_date": (
                r["travel_start_date"].isoformat() if r["travel_start_date"] else None
            ),
            "travel_end_date": (
                r["travel_end_date"].isoformat() if r["travel_end_date"] else None
            ),
            "persons_count": (
                int(r["persons_count"]) if r["persons_count"] is not None else None
            ),
            "total_amount": float(r["total_amount"] or 0),
            "partner_payout": float(r["partner_payout"] or 0),
            "customer_name": r["customer_name"],
            "customer_mobile": r["customer_mobile"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in recent_tour_rows
    ]

    # 6. Monthly revenue chart (last N months) — combined cab + hotel.
    chart_rows = (
        (
            await db.execute(
                _text(
                    """
            WITH monthly AS (
                SELECT DATE_TRUNC('month', cb.updated_at) AS m,
                       SUM(COALESCE(NULLIF(cb.partner_payout,0), cb.final_amount)) AS rev
                FROM cab_booking_assignments cba
                JOIN cab_bookings cb ON cb.id = cba.cab_booking_id
                WHERE cba.partner_id = :pid
                  AND cb.booking_status IN ('COMPLETED','SETTLEMENT_PENDING','SETTLED')
                  AND cb.updated_at >= DATE_TRUNC('month', NOW()) - INTERVAL '1 month' * :months
                GROUP BY 1
                UNION ALL
                SELECT DATE_TRUNC('month', COALESCE(hr.actual_check_out_at, hr.updated_at)) AS m,
                       SUM(hr.total_amount) AS rev
                FROM hotel_reservations hr
                JOIN hotels h ON h.id = hr.hotel_id
                WHERE h.partner_id = :pid AND h.deleted_at IS NULL
                  AND hr.reservation_status IN ('COMPLETED','SETTLEMENT_PENDING','SETTLED')
                  AND COALESCE(hr.actual_check_out_at, hr.updated_at) >= DATE_TRUNC('month', NOW()) - INTERVAL '1 month' * :months
                GROUP BY 1
                UNION ALL
                SELECT DATE_TRUNC('month', COALESCE(tb.updated_at, tb.created_at)) AS m,
                       SUM(tb.partner_payout) AS rev
                FROM tour_bookings tb
                JOIN tour_packages tp ON tp.id = tb.package_id
                WHERE tp.partner_id = :pid
                  AND tb.booking_status IN ('COMPLETED','SETTLEMENT_PENDING','SETTLED')
                  AND COALESCE(tb.updated_at, tb.created_at) >= DATE_TRUNC('month', NOW()) - INTERVAL '1 month' * :months
                GROUP BY 1
            )
            SELECT TO_CHAR(m, 'Mon') AS month_label, m AS month_date, SUM(rev) AS revenue
            FROM monthly
            GROUP BY m, month_label
            ORDER BY m ASC
        """
                ),
                {"pid": partner_id, "months": months - 1},
            )
        )
        .mappings()
        .all()
    )
    revenue_chart = [
        {"month": r["month_label"], "revenue": float(r["revenue"] or 0)}
        for r in chart_rows
    ]

    return {
        "partner": {
            "id": partner_id,
            "partner_code": p_row["partner_code"],
            "owner_name": p_row["owner_name"],
            "business_name": p_row["business_name"],
            "status": p_row["status"],
            "logo_url": p_row["logo_url"],
        },
        "wallet": wallet,
        "vehicles": vehicles,
        "bookings": bookings,
        "hotels": hotels,
        "hotel_bookings": hotel_bookings,
        "tour_bookings": tour_bookings,
        "services": services,
        "recent_bookings": recent_bookings,
        "recent_hotel_bookings": recent_hotel_bookings,
        "recent_tour_bookings": recent_tour_bookings,
        "revenue_chart": revenue_chart,
    }


# ---- Commission ----


@router.get(
    "/me/commission",
    status_code=status.HTTP_200_OK,
    summary="Get my commission group and per-service commission rates",
)
async def get_my_commission(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns the commission group currently assigned to this partner (most
    recent assignment wins — history is preserved in partner_commission_groups)
    and the active per-service-type commission rules of that group.

    commission_type is PERCENTAGE (deducted as % of booking value) or FLAT
    (fixed amount per booking). Rules may be city-specific (city_id set) or
    apply to all cities (city_id NULL).

    Doc Ref: Partner API §15 | DB Schema Part 2 §14-15
    """
    user_id = current_user["sub"]
    p_row = (
        (
            await db.execute(
                _text(
                    "SELECT id FROM partners WHERE user_id = :uid AND deleted_at IS NULL"
                ),
                {"uid": user_id},
            )
        )
        .mappings()
        .one_or_none()
    )

    if not p_row:
        return {"commission_group": None, "rules": []}

    partner_id = p_row["id"]

    group = (
        (
            await db.execute(
                _text(
                    """
                SELECT cg.id, cg.group_name, cg.description, pcg.assigned_at
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
        .one_or_none()
    )

    rules: list = []
    if group:
        rows = (
            (
                await db.execute(
                    _text(
                        """
                    SELECT cr.id, cr.service_type, cr.commission_type,
                           cr.commission_value, cr.city_id, c.name AS city_name,
                           cr.effective_from, cr.effective_to
                    FROM commission_rules cr
                    LEFT JOIN cities c ON c.id = cr.city_id
                    WHERE cr.commission_group_id = :gid AND cr.is_active = TRUE
                    ORDER BY cr.service_type, cr.city_id NULLS LAST
                """
                    ),
                    {"gid": group["id"]},
                )
            )
            .mappings()
            .all()
        )
        rules = [
            {
                "id": r["id"],
                "service_type": r["service_type"],
                "commission_type": r["commission_type"],
                "commission_value": float(r["commission_value"] or 0),
                "city_id": r["city_id"],
                "city_name": r["city_name"],
                "effective_from": (
                    r["effective_from"].isoformat() if r["effective_from"] else None
                ),
                "effective_to": (
                    r["effective_to"].isoformat() if r["effective_to"] else None
                ),
            }
            for r in rows
        ]

    return {
        "commission_group": (
            {
                "id": group["id"],
                "group_name": group["group_name"],
                "description": group["description"],
                "assigned_at": (
                    group["assigned_at"].isoformat() if group["assigned_at"] else None
                ),
            }
            if group
            else None
        ),
        "rules": rules,
    }


# ============================================================
# PARTNER DRIVERS
# GET /partners/me/drivers
# The driver module only exposes create/get/update by UUID. The partner
# app needs the roster before it can assign a driver to a booking, so this
# read is scoped directly from the authenticated user's partner row.
# ============================================================


@router.get(
    "/me/drivers",
    status_code=status.HTTP_200_OK,
    summary="List the authenticated partner's drivers",
)
async def list_my_drivers(
    status_filter: str | None = Query(None, alias="status"),
    search: str | None = Query(None),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    partner_id = (
        await db.execute(
            _text(
                "SELECT id FROM partners WHERE user_id = :uid AND deleted_at IS NULL"
            ),
            {"uid": current_user["sub"]},
        )
    ).scalar_one_or_none()
    if partner_id is None:
        return {"items": [], "total": 0}

    where = ["d.partner_id = :pid", "d.deleted_at IS NULL"]
    params: dict = {"pid": partner_id}
    if status_filter:
        where.append("d.status = :status")
        params["status"] = status_filter.strip().upper()
    if search:
        where.append(
            "(d.full_name ILIKE :search OR d.mobile ILIKE :search "
            "OR d.driver_code ILIKE :search OR d.license_number ILIKE :search)"
        )
        params["search"] = f"%{search.strip()}%"

    rows = (
        (
            await db.execute(
                _text(
                    f"""
                    SELECT d.id, d.uuid::text AS uuid, d.driver_code,
                           d.full_name, d.mobile, d.email, d.license_number,
                           d.license_expiry_date, d.status, d.joining_date,
                           COALESCE(da.availability_status, 'OFFLINE') AS availability_status,
                           (SELECT COUNT(*) FROM driver_documents dd
                             WHERE dd.driver_id = d.id) AS document_count,
                           (SELECT COUNT(*) FROM cab_booking_assignments cba
                             JOIN cab_bookings cb ON cb.id = cba.cab_booking_id
                            WHERE cba.driver_id = d.id
                              AND cb.booking_status IN
                                  ('ASSIGNED','DRIVER_ASSIGNED','STARTED','IN_PROGRESS')
                              AND cb.trip_ended_at IS NULL) AS active_trips,
                           (SELECT v.registration_number
                              FROM cab_booking_assignments cba
                              JOIN cab_bookings cb ON cb.id = cba.cab_booking_id
                              JOIN vehicles v ON v.id = cba.vehicle_id
                             WHERE cba.driver_id = d.id
                               AND cb.booking_status IN
                                   ('ASSIGNED','DRIVER_ASSIGNED','STARTED','IN_PROGRESS')
                               AND cb.trip_ended_at IS NULL
                             ORDER BY cba.id DESC LIMIT 1) AS vehicle_reg
                      FROM drivers d
                      LEFT JOIN driver_availability da ON da.driver_id = d.id
                     WHERE {' AND '.join(where)}
                     ORDER BY d.full_name ASC
                    """
                ),
                params,
            )
        )
        .mappings()
        .all()
    )
    items = [
        {
            "id": r["id"],
            "uuid": r["uuid"],
            "driver_code": r["driver_code"],
            "full_name": r["full_name"],
            "mobile": r["mobile"],
            "email": r["email"],
            "license_number": r["license_number"],
            "license_expiry_date": (
                r["license_expiry_date"].isoformat()
                if r["license_expiry_date"]
                else None
            ),
            "status": r["status"],
            "availability_status": r["availability_status"],
            "joining_date": (
                r["joining_date"].isoformat() if r["joining_date"] else None
            ),
            "document_count": int(r["document_count"] or 0),
            "active_trips": int(r["active_trips"] or 0),
            "vehicle_reg": r["vehicle_reg"],
        }
        for r in rows
    ]
    return {"items": items, "total": len(items)}


# ============================================================
# PARTNER SETTLEMENTS
# GET /partners/me/settlements
# Read-only period statements plus the current unsettled position.
# The partner portal had no partner-scoped settlement history; the app
# needs it for F2 without exposing the admin settlement routes.
# ============================================================


@router.get(
    "/me/settlements",
    status_code=status.HTTP_200_OK,
    summary="Settlement history for the authenticated partner",
)
async def list_my_settlements(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    partner_id = (
        await db.execute(
            _text(
                "SELECT id FROM partners WHERE user_id = :uid AND deleted_at IS NULL"
            ),
            {"uid": current_user["sub"]},
        )
    ).scalar_one_or_none()
    if partner_id is None:
        return {
            "items": [],
            "total": 0,
            "page": page,
            "pages": 0,
            "pending": {
                "bookings_count": 0,
                "gross_amount": 0.0,
                "commission_amount": 0.0,
                "net_payable_amount": 0.0,
                "start_date": None,
                "end_date": None,
            },
        }

    total = (
        await db.execute(
            _text("SELECT COUNT(*) FROM settlements WHERE partner_id = :pid"),
            {"pid": partner_id},
        )
    ).scalar() or 0

    rows = (
        (
            await db.execute(
                _text(
                    """
                    SELECT s.id, s.uuid::text AS uuid, s.settlement_number,
                           s.settlement_type, s.start_date, s.end_date,
                           s.gross_amount, s.commission_amount, s.gst_amount,
                           s.net_payable_amount, s.settlement_status,
                           s.settled_at, s.created_at,
                           (SELECT COUNT(*) FROM settlement_items si
                             WHERE si.settlement_id = s.id) AS booking_count
                      FROM settlements s
                     WHERE s.partner_id = :pid
                     ORDER BY s.start_date DESC, s.id DESC
                     LIMIT :limit OFFSET :offset
                    """
                ),
                {
                    "pid": partner_id,
                    "limit": page_size,
                    "offset": (page - 1) * page_size,
                },
            )
        )
        .mappings()
        .all()
    )

    pending = (
        (
            await db.execute(
                _text(
                    """
                    SELECT COUNT(DISTINCT cb.id) AS booking_count,
                           COALESCE(SUM(COALESCE(cb.final_amount, 0)
                                        + COALESCE(cb.gst_amount, 0)), 0) AS gross_amount,
                           COALESCE(SUM(COALESCE(cb.platform_commission, 0)), 0) AS commission_amount,
                           COALESCE(SUM(COALESCE(NULLIF(cb.partner_payout, 0),
                                                 cb.final_amount, 0)), 0) AS net_payable_amount,
                           MIN(cb.trip_ended_at)::date AS start_date,
                           MAX(cb.trip_ended_at)::date AS end_date
                      FROM cab_bookings cb
                      JOIN cab_booking_assignments cba
                        ON cba.cab_booking_id = cb.id AND cba.partner_id = :pid
                     WHERE cb.booking_status IN ('COMPLETED','SETTLEMENT_PENDING')
                       AND cb.payment_mode IS NOT NULL
                       AND NOT EXISTS (
                           SELECT 1 FROM settlement_items si
                            WHERE si.booking_id = cb.id
                       )
                    """
                ),
                {"pid": partner_id},
            )
        )
        .mappings()
        .one()
    )

    return {
        "items": [
            {
                "id": r["id"],
                "uuid": r["uuid"],
                "settlement_number": r["settlement_number"],
                "settlement_type": r["settlement_type"],
                "start_date": r["start_date"].isoformat() if r["start_date"] else None,
                "end_date": r["end_date"].isoformat() if r["end_date"] else None,
                "bookings_count": int(r["booking_count"] or 0),
                "gross_amount": float(r["gross_amount"] or 0),
                "commission_amount": float(r["commission_amount"] or 0),
                "gst_amount": float(r["gst_amount"] or 0),
                "net_payable_amount": float(r["net_payable_amount"] or 0),
                "status": r["settlement_status"],
                "settled_at": r["settled_at"].isoformat() if r["settled_at"] else None,
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
        "total": int(total),
        "page": page,
        "pages": (int(total) + page_size - 1) // page_size,
        "pending": {
            "bookings_count": int(pending["booking_count"] or 0),
            "gross_amount": float(pending["gross_amount"] or 0),
            "commission_amount": float(pending["commission_amount"] or 0),
            "net_payable_amount": float(pending["net_payable_amount"] or 0),
            "start_date": (
                pending["start_date"].isoformat() if pending["start_date"] else None
            ),
            "end_date": (
                pending["end_date"].isoformat() if pending["end_date"] else None
            ),
        },
    }


# ============================================================
# PARTNER SUPPORT TICKETS
# Doc Ref: 04_FINANCE_ACCOUNT.md AC4
# The help screen needs a durable place for an issue to land. Kept
# separate from customer_care_logs: a partner issue is not about one
# customer and must not enter the customer-care queue as if it were.
# ============================================================

SUPPORT_CATEGORIES = {
    "TRIP_ISSUE",
    "PAYMENT",
    "DOCUMENT",
    "APP",
    "OTHER",
}
SUPPORT_PRIORITIES = {"LOW", "NORMAL", "HIGH", "URGENT"}


class PartnerSupportTicketCreate(BaseModel):
    category: str = Field(..., max_length=50)
    subject: str = Field(..., min_length=3, max_length=255)
    description: str = Field(..., min_length=10, max_length=4000)
    booking_reference: str | None = Field(None, max_length=100)
    priority: str = Field("NORMAL", max_length=20)


class PartnerSupportMessageCreate(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)


async def _partner_row_id(db: AsyncSession, user_uuid: str) -> int | None:
    return (
        await db.execute(
            _text(
                "SELECT id FROM partners WHERE user_id = :uid AND deleted_at IS NULL"
            ),
            {"uid": user_uuid},
        )
    ).scalar_one_or_none()


@router.get(
    "/me/support-contact",
    status_code=status.HTTP_200_OK,
    summary="Support contact details and the categories the help form accepts",
)
async def get_support_contact(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        (
            await db.execute(
                _text(
                    """
                    SELECT config_key, config_value
                      FROM system_configurations
                     WHERE config_key IN ('SUPPORT_EMAIL', 'SUPPORT_PHONE', 'PLATFORM_NAME')
                    """
                )
            )
        )
        .mappings()
        .all()
    )
    cfg = {r["config_key"]: (r["config_value"] or "") for r in rows}
    phone = cfg.get("SUPPORT_PHONE", "").strip()
    return {
        "support_email": cfg.get("SUPPORT_EMAIL", "").strip() or "support@waytero.com",
        "support_phone": phone,
        "whatsapp_number": phone,
        "platform_name": cfg.get("PLATFORM_NAME", "WayTero") or "WayTero",
        "hours": "Mon–Sat, 10 AM–7 PM IST · 24×7 for active trips",
        "categories": sorted(SUPPORT_CATEGORIES),
        "priorities": sorted(SUPPORT_PRIORITIES),
    }


@router.get(
    "/me/support-tickets",
    status_code=status.HTTP_200_OK,
    summary="List the authenticated partner's support tickets",
)
async def list_my_support_tickets(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    partner_id = await _partner_row_id(db, current_user["sub"])
    if partner_id is None:
        return {
            "items": [],
            "total": 0,
            "page": page,
            "pages": 0,
            "unread_count": 0,
        }

    total = (
        await db.execute(
            _text(
                "SELECT COUNT(*) FROM partner_support_tickets WHERE partner_id = :pid"
            ),
            {"pid": partner_id},
        )
    ).scalar() or 0
    rows = (
        (
            await db.execute(
                _text(
                    """
                    SELECT t.id, t.ticket_number, t.category, t.subject,
                           t.description, t.booking_reference, t.priority,
                           t.status, t.resolution_notes, t.created_at,
                           t.updated_at, t.resolved_at,
                           (SELECT COUNT(*)
                              FROM partner_support_ticket_messages m
                             WHERE m.ticket_id = t.id
                               AND m.author_type = 'ADMIN'
                               AND m.read_by_partner_at IS NULL)
                               AS unread_count
                      FROM partner_support_tickets t
                     WHERE t.partner_id = :pid
                     ORDER BY created_at DESC, id DESC
                     LIMIT :limit OFFSET :offset
                    """
                ),
                {
                    "pid": partner_id,
                    "limit": page_size,
                    "offset": (page - 1) * page_size,
                },
            )
        )
        .mappings()
        .all()
    )
    return {
        "items": [
            {
                "id": r["id"],
                "ticket_number": r["ticket_number"],
                "category": r["category"],
                "subject": r["subject"],
                "description": r["description"],
                "booking_reference": r["booking_reference"],
                "priority": r["priority"],
                "status": r["status"],
                "resolution_notes": r["resolution_notes"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
                "resolved_at": (
                    r["resolved_at"].isoformat() if r["resolved_at"] else None
                ),
                "unread_count": int(r["unread_count"] or 0),
            }
            for r in rows
        ],
        "total": int(total),
        "page": page,
        "pages": (int(total) + page_size - 1) // page_size,
        "unread_count": int(
            (
                await db.execute(
                    _text(
                        "SELECT COUNT(*) "
                        "FROM partner_support_ticket_messages m "
                        "JOIN partner_support_tickets t ON t.id = m.ticket_id "
                        "WHERE t.partner_id = :pid "
                        "AND m.author_type = 'ADMIN' "
                        "AND m.read_by_partner_at IS NULL"
                    ),
                    {"pid": partner_id},
                )
            ).scalar()
            or 0
        ),
    }


@router.post(
    "/me/support-tickets",
    status_code=status.HTTP_201_CREATED,
    summary="Raise a support ticket",
)
async def create_support_ticket(
    body: PartnerSupportTicketCreate,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    partner_id = await _partner_row_id(db, current_user["sub"])
    if partner_id is None:
        raise HTTPException(status_code=404, detail="Partner profile not found")

    category = body.category.strip().upper()
    if category not in SUPPORT_CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported category. Valid: {', '.join(sorted(SUPPORT_CATEGORIES))}",
        )
    priority = body.priority.strip().upper()
    if priority not in SUPPORT_PRIORITIES:
        priority = "NORMAL"

    ticket_number = f"PST-{uuid4().hex[:8].upper()}"
    row = (
        (
            await db.execute(
                _text(
                    """
                    INSERT INTO partner_support_tickets (
                        uuid, partner_id, ticket_number, category, subject,
                        description, booking_reference, priority, status,
                        created_at, updated_at
                    ) VALUES (
                        :uuid, :pid, :number, :category, :subject,
                        :description, :booking, :priority, 'OPEN', NOW(), NOW()
                    )
                    RETURNING id, ticket_number, category, subject, description,
                              booking_reference, priority, status, created_at
                    """
                ),
                {
                    "uuid": str(uuid4()),
                    "pid": partner_id,
                    "number": ticket_number,
                    "category": category,
                    "subject": body.subject.strip(),
                    "description": body.description.strip(),
                    "booking": (body.booking_reference or "").strip() or None,
                    "priority": priority,
                },
            )
        )
        .mappings()
        .one()
    )
    partner_name = (
        await db.execute(
            _text(
                "SELECT COALESCE(NULLIF(owner_name, ''), business_name, 'Partner') "
                "FROM partners WHERE id = :pid"
            ),
            {"pid": partner_id},
        )
    ).scalar_one_or_none()
    await db.execute(
        _text(
            """
            INSERT INTO partner_support_ticket_messages (
                uuid, ticket_id, author_type, author_user_id,
                author_name, body, created_at,
                read_by_partner_at, read_by_admin_at
            ) VALUES (
                gen_random_uuid(), :ticket_id, 'PARTNER', :user_id,
                :author_name, :body, NOW(), NOW(), NULL
            )
            """
        ),
        {
            "ticket_id": row["id"],
            "user_id": current_user["sub"],
            "author_name": partner_name or "Partner",
            "body": row["description"],
        },
    )
    return {
        "id": row["id"],
        "ticket_number": row["ticket_number"],
        "category": row["category"],
        "subject": row["subject"],
        "description": row["description"],
        "booking_reference": row["booking_reference"],
        "priority": row["priority"],
        "status": row["status"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "message": "Support ticket raised",
    }


@router.get(
    "/me/support-unread-count",
    status_code=status.HTTP_200_OK,
    summary="Unread support replies for the authenticated partner",
)
async def get_my_support_unread_count(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    partner_id = await _partner_row_id(db, current_user["sub"])
    if partner_id is None:
        return {"unread_count": 0}
    count = (
        await db.execute(
            _text(
                "SELECT COUNT(*) "
                "FROM partner_support_ticket_messages m "
                "JOIN partner_support_tickets t ON t.id = m.ticket_id "
                "WHERE t.partner_id = :pid "
                "AND m.author_type = 'ADMIN' "
                "AND m.read_by_partner_at IS NULL"
            ),
            {"pid": partner_id},
        )
    ).scalar() or 0
    return {"unread_count": int(count)}


@router.get(
    "/me/support-tickets/{ticket_id}",
    status_code=status.HTTP_200_OK,
    summary="Get one of the partner's support tickets with its thread",
)
async def get_my_support_ticket(
    ticket_id: int,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    partner_id = await _partner_row_id(db, current_user["sub"])
    if partner_id is None:
        raise HTTPException(status_code=404, detail="Partner profile not found")

    # Opening the thread is the read receipt for the partner side.
    await db.execute(
        _text(
            "UPDATE partner_support_ticket_messages "
            "SET read_by_partner_at = NOW() "
            "WHERE ticket_id = :tid AND author_type = 'ADMIN' "
            "AND read_by_partner_at IS NULL"
        ),
        {"tid": ticket_id},
    )
    ticket = (
        (
            await db.execute(
                _text(
                    """
                    SELECT id, ticket_number, category, subject, description,
                           booking_reference, priority, status, resolution_notes,
                           created_at, updated_at, resolved_at
                      FROM partner_support_tickets
                     WHERE id = :tid AND partner_id = :pid
                    """
                ),
                {"tid": ticket_id, "pid": partner_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if not ticket:
        raise HTTPException(status_code=404, detail="Support ticket not found")

    messages = (
        (
            await db.execute(
                _text(
                    """
                    SELECT id, ticket_id, author_type, author_name, body,
                           created_at
                      FROM partner_support_ticket_messages
                     WHERE ticket_id = :tid
                     ORDER BY created_at ASC, id ASC
                    """
                ),
                {"tid": ticket_id},
            )
        )
        .mappings()
        .all()
    )
    return {
        "id": ticket["id"],
        "ticket_number": ticket["ticket_number"],
        "category": ticket["category"],
        "subject": ticket["subject"],
        "description": ticket["description"],
        "booking_reference": ticket["booking_reference"],
        "priority": ticket["priority"],
        "status": ticket["status"],
        "resolution_notes": ticket["resolution_notes"],
        "created_at": (
            ticket["created_at"].isoformat() if ticket["created_at"] else None
        ),
        "updated_at": (
            ticket["updated_at"].isoformat() if ticket["updated_at"] else None
        ),
        "resolved_at": (
            ticket["resolved_at"].isoformat() if ticket["resolved_at"] else None
        ),
        "messages": [
            {
                "id": message["id"],
                "ticket_id": message["ticket_id"],
                "author_type": message["author_type"],
                "author_name": message["author_name"],
                "body": message["body"],
                "created_at": (
                    message["created_at"].isoformat() if message["created_at"] else None
                ),
            }
            for message in messages
        ],
    }


@router.post(
    "/me/support-tickets/{ticket_id}/messages",
    status_code=status.HTTP_201_CREATED,
    summary="Reply on a partner support ticket",
)
async def reply_to_my_support_ticket(
    ticket_id: int,
    body: PartnerSupportMessageCreate,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    partner_id = await _partner_row_id(db, current_user["sub"])
    if partner_id is None:
        raise HTTPException(status_code=404, detail="Partner profile not found")

    ticket = (
        (
            await db.execute(
                _text(
                    """
                    SELECT id, status FROM partner_support_tickets
                     WHERE id = :tid AND partner_id = :pid
                    """
                ),
                {"tid": ticket_id, "pid": partner_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if not ticket:
        raise HTTPException(status_code=404, detail="Support ticket not found")
    if ticket["status"] == "CLOSED":
        raise HTTPException(
            status_code=400,
            detail="This ticket is closed. Raise a new ticket for a new issue.",
        )

    message = body.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    partner_name = (
        await db.execute(
            _text(
                "SELECT COALESCE(NULLIF(owner_name, ''), business_name, 'Partner') "
                "FROM partners WHERE id = :pid"
            ),
            {"pid": partner_id},
        )
    ).scalar_one_or_none()
    row = (
        (
            await db.execute(
                _text(
                    """
                    INSERT INTO partner_support_ticket_messages (
                        uuid, ticket_id, author_type, author_user_id,
                        author_name, body, created_at,
                        read_by_partner_at, read_by_admin_at
                    ) VALUES (
                        gen_random_uuid(), :ticket_id, 'PARTNER', :user_id,
                        :author_name, :body, NOW(), NOW(), NULL
                    )
                    RETURNING id, ticket_id, author_type, author_name,
                              body, created_at
                    """
                ),
                {
                    "ticket_id": ticket_id,
                    "user_id": current_user["sub"],
                    "author_name": partner_name or "Partner",
                    "body": message,
                },
            )
        )
        .mappings()
        .one()
    )

    # A resolved ticket is not finished if the partner comes back. Reopen it
    # so it returns to the admin queue instead of silently staying resolved.
    if ticket["status"] == "RESOLVED":
        await db.execute(
            _text(
                "UPDATE partner_support_tickets SET status = 'OPEN', "
                "resolved_at = NULL, updated_at = NOW() WHERE id = :id"
            ),
            {"id": ticket_id},
        )
    else:
        await db.execute(
            _text(
                "UPDATE partner_support_tickets SET updated_at = NOW() WHERE id = :id"
            ),
            {"id": ticket_id},
        )

    return {
        "id": row["id"],
        "ticket_id": row["ticket_id"],
        "author_type": row["author_type"],
        "author_name": row["author_name"],
        "body": row["body"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "ticket_status": "OPEN" if ticket["status"] == "RESOLVED" else ticket["status"],
    }


# ============================================================
# PARTNER BOOKING ENDPOINTS
# Live in app/modules/partner/booking_api.py — that router is
# registered under the same /partners prefix in api/router.py.
# Defining them here as well shadows those routes: FastAPI matches
# the first registered path, and this module is included first, so
# duplicates here silently win and drop trip/payment/invoice fields.
# ============================================================
