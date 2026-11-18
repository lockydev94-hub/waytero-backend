# ============================================================
# WAYTERO — ADMIN DASHBOARD SERVICE (ASYNC, RAW SQL)
# File: app/modules/admin/services/dashboard.py
# Doc Ref:
#   Admin API §3  — Dashboard
#   Admin API §4  — System Summary
#   Admin API §5  — Users
#   Admin API §8  — Partners
#   Admin API §9  — Drivers
#   Admin API §10 — Vehicles
#   Admin API §11 — Bookings
#   Admin API §16 — Settlements
#   Admin API §22 — Notifications Broadcast
#   Admin API §23 — Reports
#   Admin API §24 — Audit Logs
#
# Uses raw SQL via text() — booking/payment/settlement ORM models
# are stubs. Queries reference actual tables from migrations.
# ============================================================

from decimal import Decimal
from datetime import date, datetime, timezone, timedelta
from typing import Optional, List, Dict, Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from app.core.timezone import get_platform_tz
from app.core.branch_scope import BranchScope, ScopeSql
from app.core.exceptions import (
    BusinessException,
    PermissionDeniedException,
    ResourceNotFoundException,
    ValidationException,
)
from app.infrastructure.cache.redis_client import RedisCache, get_redis
from app.modules.auth.repositories import StaffWorkDailyRepository
from app.modules.admin.schemas.dashboard import (
    StaffProfileCreate,
    StaffUserCreate,
    StaffUserUpdate,
)

# Sidebar badges: 45 s is shorter than the portal's 60 s poll, so a queue is
# never served from a cache that is staler than the refresh the client does.
ACTION_COUNTS_CACHE_PREFIX = "admin:action_counts"
ACTION_COUNTS_TTL_SECONDS = 45


def action_counts_cache_key(scope: BranchScope) -> str:
    """One cache entry per branch, never one for the platform.

    The badge counts are per-branch facts, and they are cached. A single key
    would serve whichever branch asked first to every other branch's sidebar —
    and the first caller after deployment is whoever happens to be logged in, so
    the wrong number would be correct half the time in testing and wrong in
    production. "all" is the super admin's unfiltered view, which is a real
    thing to cache and is not any branch's answer.
    """
    return f"{ACTION_COUNTS_CACHE_PREFIX}:{scope.branch_id or 'all'}"


# The staff register. SUPER_ADMIN is a row only a super admin may enumerate:
# it is not a hireable role (StaffUserCreate refuses it) and a manager has no
# reason to learn how many master accounts exist or which one to probe.
STAFF_LIST_ROLES = (
    "ADMIN",
    "CCO",
    "VERIFICATION_OFFICER",
    "FINANCE_MANAGER",
    "SUPER_ADMIN",
)
MANAGER_VISIBLE_ROLES = (
    "ADMIN",
    "CCO",
    "VERIFICATION_OFFICER",
    "FINANCE_MANAGER",
)

# (nav key, count query) — built per caller, because every arm of the UNION has
# to carry this branch's city or the sidebar lies. The keys are the sidebar's, so
# the client can index the response without a mapping table of its own.
_CLOSED_CAPTURE_STATUSES = "'CLOSED', 'DONE', 'EXPIRED'"


def _action_counts_statement(scope: BranchScope) -> tuple[str, dict[str, Any]]:
    """The eleven queue counts as one UNION, scoped to this caller.

    Written out per queue rather than generated from a table: each reaches its
    city a different way — its own `city_id` column (the capture queues, which
    migration 0085 gave one), its booking (the cab queues and guest feedback,
    which have no location of their own), its partner (deletions and approvals) —
    and a template that hides which one is a template that quietly counts another
    branch's work.
    """
    city = ScopeSql(scope)
    parts = [
        (
            "SELECT 'bookings' AS nav_key, COUNT(*) AS c FROM cab_bookings cb "
            f"WHERE cb.booking_status = 'PENDING_ASSIGNMENT' "
            f"AND ({city.through_bookings('cb', param='ac_bookings')})"
        ),
        (
            "SELECT 'cab-ops' AS nav_key, COUNT(*) AS c FROM cab_bookings cb "
            f"WHERE cb.booking_status = 'PENDING_PARTNER_ACCEPTANCE' "
            f"AND ({city.through_bookings('cb', param='ac_cab_ops')})"
        ),
        (
            "SELECT 'trip-assistance' AS nav_key, COUNT(*) AS c FROM cab_bookings cb "
            "WHERE (cb.booking_status IN ('DRIVER_ASSIGNED', 'STARTED') "
            "   OR (cb.booking_status = 'COMPLETED' AND cb.payment_mode IS NULL) "
            "   OR (cb.booking_status = 'COMPLETED' AND cb.cash_pending_at = 'DRIVER')) "
            f"AND ({city.through_bookings('cb', param='ac_trip_assist')})"
        ),
        (
            "SELECT 'customer-care' AS nav_key, COUNT(*) AS c FROM customer_care_logs cl "
            f"WHERE cl.status IN ('OPEN', 'IN_PROGRESS') "
            f"AND ({city.city('cl.city_id', param='ac_care')})"
        ),
        (
            "SELECT 'partner-support' AS nav_key, COUNT(*) AS c "
            "FROM partner_support_ticket_messages m "
            "JOIN partner_support_tickets t ON t.id = m.ticket_id "
            "JOIN partners p ON p.id = t.partner_id "
            "WHERE m.author_type = 'PARTNER' "
            "AND m.read_by_admin_at IS NULL "
            f"AND ({city.city('p.city_id', param='ac_partner_support')})"
        ),
        (
            "SELECT 'chat' AS nav_key, COUNT(*) AS c FROM chat_conversations cc "
            "WHERE (cc.status = 'WAITING' "
            "       OR (cc.status = 'OPEN' AND cc.unread_admin_count > 0)) "
            f"AND ({city.city('cc.city_id', param='ac_chat')})"
        ),
        (
            f"SELECT 'callback-requests' AS nav_key, COUNT(*) AS c FROM callback_requests cr "
            f"WHERE cr.status NOT IN ({_CLOSED_CAPTURE_STATUSES}) "
            f"AND ({city.city('cr.city_id', param='ac_callback')})"
        ),
        (
            "SELECT 'contact-messages' AS nav_key, COUNT(*) AS c FROM contact_messages cm "
            f"WHERE cm.is_read = false "
            f"AND ({city.city('cm.city_id', param='ac_messages')})"
        ),
        (
            f"SELECT 'enquiries' AS nav_key, COUNT(*) AS c FROM enquiries e "
            f"WHERE e.status NOT IN ({_CLOSED_CAPTURE_STATUSES}) "
            f"AND ({city.city('e.city_id', param='ac_enquiries')})"
        ),
        (
            "SELECT 'partner-applications' AS nav_key, COUNT(*) AS c "
            "FROM partner_applications pa "
            f"WHERE pa.status = 'NEW' "
            f"AND ({city.city('pa.city_id', param='ac_partner_apps')})"
        ),
        (
            "SELECT 'guest-feedback' AS nav_key, COUNT(*) AS c FROM booking_reviews br "
            f"WHERE br.status = 'PENDING' "
            f"AND ({city.through_bookings('br', param='ac_feedback')})"
        ),
        (
            "SELECT 'account-deletion' AS nav_key, COUNT(*) AS c "
            "FROM account_deletion_requests adr "
            "WHERE adr.status = 'PENDING' "
            f"AND ({_deletion_city_clause(city)})"
        ),
    ]
    return "\nUNION ALL\n".join(parts), city.params


def _deletion_city_clause(city: ScopeSql) -> str:
    """A deletion request's office is the office of the customer asking for it.

    `account_deletion_requests` has no city and no booking — only the customer
    row, whose own `city_id` is nullable. A request whose customer cannot be
    resolved therefore belongs to no branch, including the head office: the
    predicate is false for it, which leaves it visible to a super admin (whose
    clause is vacuous) and invisible to everyone else. That is the fail-closed
    direction, and a deletion request is not a queue a branch acts on this round.
    """
    if not city.scope.restricted:
        return "1 = 1"
    return (
        "EXISTS (SELECT 1 FROM customers cu WHERE cu.id = adr.customer_id AND "
        + city.city("cu.city_id", param="ac_deletion")
        + ")"
    )


class AdminDashboardService:

    # ── §3 Dashboard KPIs ────────────────────────────────────────────────────
    @staticmethod
    async def get_dashboard(db: AsyncSession, scope: BranchScope) -> Dict[str, Any]:
        """The KPI row on the landing page, for one caller's branch.

        Every figure is scoped, including the money: a Cuttack manager reading a
        "today's revenue" that contains Bhubaneswar's bookings has no way to tell
        the number is not theirs, and no way to decide anything with it. A
        settlement and a partner approval are scoped through the party that owes
        or is waiting, because a settlement has no city of its own.
        """
        city = ScopeSql(scope)
        r = await db.execute(
            text(
                f"""
            SELECT
                (SELECT COUNT(*) FROM cab_bookings cb
                 WHERE DATE(cb.created_at) = CURRENT_DATE
                   AND ({city.through_bookings("cb", param="dash_cab_today")})
                ) AS today_cab_bookings,
                (SELECT COUNT(*) FROM hotel_reservations hr
                 WHERE DATE(hr.created_at) = CURRENT_DATE
                   AND ({city.through_bookings("hr", param="dash_hotel_today")})
                ) AS today_hotel_reservations,
                (SELECT COUNT(*) FROM cab_bookings cb
                 WHERE cb.booking_status IN ('STARTED','DRIVER_ASSIGNED')
                   AND ({city.through_bookings("cb", param="dash_cab_active")})
                ) AS active_trips,
                COALESCE((
                    SELECT SUM(cb.final_amount)
                    FROM cab_bookings cb
                    WHERE cb.booking_status = 'COMPLETED'
                      AND DATE(cb.trip_ended_at) = CURRENT_DATE
                      AND ({city.through_bookings("cb", param="dash_cab_rev")})
                ), 0) +
                COALESCE((
                    SELECT SUM(hr.total_amount)
                    FROM hotel_reservations hr
                    WHERE hr.payment_collected_status = 'PAID'
                      AND DATE(hr.payment_collected_at) = CURRENT_DATE
                      AND ({city.through_bookings("hr", param="dash_hotel_rev")})
                ), 0) AS today_revenue,
                COALESCE((
                    SELECT SUM(s.net_payable_amount) FROM settlements s
                    JOIN partners p ON p.id = s.partner_id
                    WHERE s.settlement_status = 'PENDING'
                      AND ({city.city("p.city_id", param="dash_settle", allow_unassigned=False)})
                ), 0) AS pending_settlements,
                (SELECT COUNT(*) FROM partners p
                 WHERE p.status IN ('PENDING','UNDER_REVIEW','DOCUMENT_PENDING')
                   AND ({city.city("p.city_id", param="dash_partner", allow_unassigned=False)})
                ) AS pending_partner_approvals
        """
            ),
            city.params,
        )
        row = r.mappings().one()
        return {
            "today_cab_bookings": int(row["today_cab_bookings"] or 0),
            "today_hotel_reservations": int(row["today_hotel_reservations"] or 0),
            "active_trips": int(row["active_trips"] or 0),
            "today_revenue": Decimal(str(row["today_revenue"] or 0)),
            "pending_settlements": Decimal(str(row["pending_settlements"] or 0)),
            "pending_partner_approvals": int(row["pending_partner_approvals"] or 0),
        }

    # ── §4 System Summary ────────────────────────────────────────────────────
    @staticmethod
    async def get_system_summary(
        db: AsyncSession, scope: BranchScope
    ) -> Dict[str, Any]:
        city = ScopeSql(scope)

        # A customer's office is the one on their customer row, not a branch of
        # their own. Written as EXISTS rather than a join so that a super admin —
        # for whom the predicate is vacuous — still gets the count of every
        # customer account, including any that has no customer row yet.
        customers_clause = (
            "1 = 1"
            if not scope.restricted
            else "EXISTS (SELECT 1 FROM customers cu WHERE cu.user_id = u.id AND "
            + city.city("cu.city_id", param="sum_customers")
            + ")"
        )

        r = await db.execute(
            text(
                f"""
            SELECT
                (SELECT COUNT(*) FROM users u WHERE u.user_type = 'CUSTOMER'
                 AND ({customers_clause})) AS total_customers,
                (SELECT COUNT(*) FROM partners p WHERE p.deleted_at IS NULL
                 AND ({city.city("p.city_id", param="sum_partners", allow_unassigned=False)})) AS total_partners,
                (SELECT COUNT(*) FROM partners p WHERE p.status = 'ACTIVE' AND p.deleted_at IS NULL
                 AND ({city.city("p.city_id", param="sum_active_partners", allow_unassigned=False)})) AS active_partners,
                (SELECT COUNT(*) FROM hotels h
                 WHERE ({city.city("h.city_id", param="sum_hotels", allow_unassigned=False)})) AS total_hotels,
                (SELECT COUNT(*) FROM hotels h WHERE h.status = 'ACTIVE'
                 AND ({city.city("h.city_id", param="sum_active_hotels", allow_unassigned=False)})) AS active_hotels,
                COALESCE((
                    SELECT SUM(cb.final_amount)
                    FROM cab_bookings cb
                    WHERE cb.booking_status = 'COMPLETED'
                      AND cb.trip_ended_at >= DATE_TRUNC('month', NOW())
                      AND ({city.through_bookings("cb", param="sum_cab_rev")})
                ), 0) +
                COALESCE((
                    SELECT SUM(hr.total_amount)
                    FROM hotel_reservations hr
                    WHERE hr.reservation_status NOT IN ('CANCELLED','EXPIRED')
                      AND hr.created_at >= DATE_TRUNC('month', NOW())
                      AND ({city.through_bookings("hr", param="sum_hotel_rev")})
                ), 0) AS monthly_revenue,
                (SELECT COUNT(*) FROM hotels h
                 WHERE h.status IN ('PENDING','UNDER_REVIEW','DOCUMENT_PENDING')
                 AND ({city.city("h.city_id", param="sum_hotel_pending", allow_unassigned=False)}))
                    AS pending_hotel_approvals
        """
            ),
            city.params,
        )
        row = r.mappings().one()
        return {
            "total_customers": int(row["total_customers"] or 0),
            "total_partners": int(row["total_partners"] or 0),
            "active_partners": int(row["active_partners"] or 0),
            "total_hotels": int(row["total_hotels"] or 0),
            "active_hotels": int(row["active_hotels"] or 0),
            "monthly_revenue": Decimal(str(row["monthly_revenue"] or 0)),
            "pending_hotel_approvals": int(row["pending_hotel_approvals"] or 0),
        }

    # ── Dashboard Charts ─────────────────────────────────────────────────────

    @staticmethod
    async def get_city_performance(
        db: AsyncSession, scope: BranchScope
    ) -> List[Dict[str, Any]]:
        """Top 5 cities by combined cab + hotel bookings this month.

        For a branch account this chart is one bar: the chart compares offices,
        and an account that owns one office has one thing to compare.
        """
        city = ScopeSql(scope)
        r = await db.execute(
            text(
                f"""
            SELECT ci.name AS city, COUNT(*) AS bookings
            FROM master_bookings mb
            JOIN cities ci ON ci.id = mb.city_id
            WHERE mb.created_at >= DATE_TRUNC('month', NOW())
              AND ({city.city("mb.city_id", param="perf_city", allow_unassigned=False)})
            GROUP BY ci.name
            ORDER BY bookings DESC
            LIMIT 5
        """
            ),
            city.params,
        )
        return [
            {"city": row["city"], "bookings": int(row["bookings"])}
            for row in r.mappings().all()
        ]

    @staticmethod
    async def get_revenue_trend(
        db: AsyncSession, scope: BranchScope
    ) -> List[Dict[str, Any]]:
        """Monthly revenue + booking count for the last 6 calendar months."""
        city = ScopeSql(scope)
        r = await db.execute(
            text(
                f"""
            WITH months AS (
                SELECT generate_series(
                    DATE_TRUNC('month', NOW()) - INTERVAL '5 months',
                    DATE_TRUNC('month', NOW()),
                    '1 month'
                ) AS month_start
            ),
            cab_rev AS (
                SELECT DATE_TRUNC('month', cb.trip_ended_at) AS m,
                       COALESCE(SUM(cb.final_amount), 0) AS rev,
                       COUNT(*) AS cnt
                FROM cab_bookings cb
                WHERE cb.booking_status = 'COMPLETED'
                  AND cb.trip_ended_at >= DATE_TRUNC('month', NOW()) - INTERVAL '5 months'
                  AND ({city.through_bookings("cb", param="trend_cab")})
                GROUP BY m
            ),
            hotel_rev AS (
                SELECT DATE_TRUNC('month', hr.created_at) AS m,
                       COALESCE(SUM(hr.total_amount), 0) AS rev,
                       COUNT(*) AS cnt
                FROM hotel_reservations hr
                WHERE hr.reservation_status NOT IN ('CANCELLED','EXPIRED')
                  AND hr.created_at >= DATE_TRUNC('month', NOW()) - INTERVAL '5 months'
                  AND ({city.through_bookings("hr", param="trend_hotel")})
                GROUP BY m
            )
            SELECT
                TO_CHAR(months.month_start, 'Mon') AS month,
                COALESCE(cab_rev.rev, 0) + COALESCE(hotel_rev.rev, 0) AS revenue,
                COALESCE(cab_rev.cnt, 0) + COALESCE(hotel_rev.cnt, 0) AS bookings
            FROM months
            LEFT JOIN cab_rev   ON cab_rev.m   = months.month_start
            LEFT JOIN hotel_rev ON hotel_rev.m = months.month_start
            ORDER BY months.month_start
        """
            ),
            city.params,
        )
        return [
            {
                "month": row["month"],
                "revenue": float(row["revenue"] or 0),
                "bookings": int(row["bookings"] or 0),
            }
            for row in r.mappings().all()
        ]

    @staticmethod
    async def get_recent_activity(
        db: AsyncSession, limit: int = 10, *, scope: BranchScope
    ) -> List[Dict[str, Any]]:
        """Latest meaningful events from booking_timelines + partner_verification_logs.

        Filtered before the LIMIT, not after: the feed is "the newest ten things
        that happened here", and taking the platform's newest ten and then
        dropping the ones that are not yours would leave a branch manager with a
        list of three that is missing seven events they never see they had.
        """
        city = ScopeSql(scope)
        booking_events = await db.execute(
            text(
                f"""
            SELECT
                bt.id::text AS id,
                bt.event_type,
                bt.event_description AS label,
                bt.event_timestamp AS ts,
                mb.booking_number
            FROM booking_timelines bt
            JOIN master_bookings mb ON mb.id = bt.master_booking_id
            WHERE bt.event_type IN (
                'TRIP_CLOSED_BY_PARTNER','HOTEL_CHECKED_IN','HOTEL_CHECKED_OUT',
                'HOTEL_COMPLETED','PAYMENT_CASH_COLLECTED','HOTEL_PAYMENT_COLLECTED'
            )
              AND ({city.city("mb.city_id", param="act_bookings", allow_unassigned=False)})
            ORDER BY bt.event_timestamp DESC
            LIMIT :lim
        """
            ),
            {**city.params, "lim": limit},
        )

        partner_events = await db.execute(
            text(
                f"""
            SELECT
                pvl.id::text AS id,
                pvl.action AS event_type,
                COALESCE(p.business_name, p.owner_name, 'Partner') || ' — ' || pvl.action AS label,
                pvl.created_at AS ts,
                NULL AS booking_number
            FROM partner_verification_logs pvl
            JOIN partners p ON p.id = pvl.partner_id
            WHERE pvl.action IN (
                'ADMIN_ACTIVE','ADMIN_APPROVED','PARTNER_REGISTERED'
            )
              AND ({city.city("p.city_id", param="act_partners", allow_unassigned=False)})
            ORDER BY pvl.created_at DESC
            LIMIT :lim
        """
            ),
            {**city.params, "lim": limit // 2},
        )

        _STATUS_MAP = {
            "TRIP_CLOSED_BY_PARTNER": "success",
            "HOTEL_CHECKED_IN": "success",
            "HOTEL_CHECKED_OUT": "success",
            "HOTEL_COMPLETED": "success",
            "PAYMENT_CASH_COLLECTED": "success",
            "HOTEL_PAYMENT_COLLECTED": "success",
            "ADMIN_ACTIVE": "success",
            "ADMIN_APPROVED": "success",
            "PARTNER_REGISTERED": "info",
        }

        _TYPE_MAP = {
            "TRIP_CLOSED_BY_PARTNER": "Cab",
            "HOTEL_CHECKED_IN": "Hotel",
            "HOTEL_CHECKED_OUT": "Hotel",
            "HOTEL_COMPLETED": "Hotel",
            "PAYMENT_CASH_COLLECTED": "Payment",
            "HOTEL_PAYMENT_COLLECTED": "Payment",
            "ADMIN_ACTIVE": "Partner",
            "ADMIN_APPROVED": "Partner",
            "PARTNER_REGISTERED": "Partner",
        }

        from datetime import timezone as _tz

        def _format_time(ts) -> str:
            if not ts:
                return ""
            now = datetime.now(_tz.utc)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=_tz.utc)
            diff = int((now - ts).total_seconds())
            if diff < 60:
                return "just now"
            if diff < 3600:
                return f"{diff // 60} min ago"
            if diff < 86400:
                return f"{diff // 3600} hr ago"
            return f"{diff // 86400}d ago"

        combined = []
        for row in booking_events.mappings().all():
            combined.append(
                {
                    "id": row["id"],
                    "type": _TYPE_MAP.get(row["event_type"], "Booking"),
                    "label": row["label"] or row["booking_number"] or row["event_type"],
                    "time": _format_time(row["ts"]),
                    "status": _STATUS_MAP.get(row["event_type"], "info"),
                }
            )
        for row in partner_events.mappings().all():
            combined.append(
                {
                    "id": f"pvl-{row['id']}",
                    "type": _TYPE_MAP.get(row["event_type"], "Partner"),
                    "label": row["label"],
                    "time": _format_time(row["ts"]),
                    "status": _STATUS_MAP.get(row["event_type"], "info"),
                }
            )

        combined.sort(key=lambda x: x["time"])
        return combined[:limit]

    # ── §5 User Management ───────────────────────────────────────────────────
    @staticmethod
    async def list_users(
        db: AsyncSession,
        role: Optional[str] = None,
        status: Optional[str] = None,
        mobile: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        where_clauses = []
        params: Dict[str, Any] = {"limit": page_size, "offset": (page - 1) * page_size}

        if role:
            where_clauses.append("user_type = :role")
            params["role"] = role
        if status:
            where_clauses.append("status = :status")
            params["status"] = status
        if mobile:
            where_clauses.append("mobile_number ILIKE :mobile")
            params["mobile"] = f"%{mobile}%"

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        count_r = await db.execute(
            text(f"SELECT COUNT(*) FROM users {where_sql}"), params
        )
        total = count_r.scalar() or 0

        rows_r = await db.execute(
            text(
                f"""
                SELECT id, user_code, first_name, last_name, mobile_number,
                       email, user_type, status, is_active, created_at
                FROM users {where_sql}
                ORDER BY created_at DESC
                LIMIT :limit OFFSET :offset
            """
            ),
            params,
        )
        items = [dict(r) for r in rows_r.mappings().all()]
        return {
            "items": items,
            "total": int(total),
            "page": page,
            "page_size": page_size,
        }

    @staticmethod
    async def get_user(db: AsyncSession, user_id: UUID) -> Optional[Dict[str, Any]]:
        r = await db.execute(
            text(
                """
                SELECT id, user_code, first_name, last_name, mobile_number,
                       email, user_type, status, is_active, created_at
                FROM users WHERE id = :user_id
            """
            ),
            {"user_id": str(user_id)},
        )
        row = r.mappings().one_or_none()
        return dict(row) if row else None

    @staticmethod
    async def set_user_status(
        db: AsyncSession, user_id: UUID, is_active: bool, status: str
    ) -> bool:
        r = await db.execute(
            text(
                """
                UPDATE users SET is_active = :is_active, status = :status,
                                 updated_at = NOW()
                WHERE id = :user_id
            """
            ),
            {"is_active": is_active, "status": status, "user_id": str(user_id)},
        )
        await db.commit()
        return r.rowcount > 0

    # ── §8 Partner Management ────────────────────────────────────────────────
    @staticmethod
    async def list_partners(
        db: AsyncSession,
        status: Optional[str] = None,
        partner_type: Optional[str] = None,
        search: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        where_clauses = []
        params: Dict[str, Any] = {"limit": page_size, "offset": (page - 1) * page_size}

        if status:
            where_clauses.append("status = :status")
            params["status"] = status

        if partner_type:
            where_clauses.append("partner_type = :partner_type")
            params["partner_type"] = partner_type

        if search and search.strip():
            where_clauses.append(
                "(business_name ILIKE :search OR owner_name ILIKE :search "
                "OR partner_code ILIKE :search OR mobile ILIKE :search "
                "OR email ILIKE :search)"
            )
            params["search"] = f"%{search.strip()}%"

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        total = (
            await db.execute(text(f"SELECT COUNT(*) FROM partners {where_sql}"), params)
        ).scalar() or 0
        rows = await db.execute(
            text(
                f"""
                SELECT p.id, p.partner_code, p.partner_type, p.business_name,
                       p.owner_name,
                       p.owner_name AS contact_person,
                       p.mobile,
                       p.mobile AS mobile_number,
                       p.email, p.status, p.city_id, p.created_at,
                       CASE WHEN EXISTS(
                           SELECT 1 FROM partner_services ps
                           WHERE ps.partner_id = p.id AND ps.service_type = 'CAB' AND ps.is_active = TRUE
                       ) THEN TRUE ELSE FALSE END AS has_cab_service
                FROM partners p {where_sql}
                ORDER BY p.created_at DESC
                LIMIT :limit OFFSET :offset
            """
            ),
            params,
        )
        return {
            "items": [dict(r) for r in rows.mappings().all()],
            "total": int(total),
            "page": page,
            "page_size": page_size,
        }

    @staticmethod
    async def set_partner_status(
        db: AsyncSession, partner_id: int, status: str
    ) -> bool:
        r = await db.execute(
            text(
                "UPDATE partners SET status = :status, updated_at = NOW() WHERE id = :id"
            ),
            {"status": status, "id": partner_id},
        )
        await db.commit()
        return r.rowcount > 0

    # ── §9 Driver Management ─────────────────────────────────────────────────
    @staticmethod
    async def list_drivers(
        db: AsyncSession,
        status: Optional[str] = None,
        search: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        where_clauses = []
        params: Dict[str, Any] = {"limit": page_size, "offset": (page - 1) * page_size}

        if status:
            where_clauses.append("status = :status")
            params["status"] = status

        if search and search.strip():
            where_clauses.append(
                "(full_name ILIKE :search OR mobile ILIKE :search "
                "OR driver_code ILIKE :search OR license_number ILIKE :search)"
            )
            params["search"] = f"%{search.strip()}%"

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        total = (
            await db.execute(text(f"SELECT COUNT(*) FROM drivers {where_sql}"), params)
        ).scalar() or 0
        rows = await db.execute(
            text(
                f"""
                SELECT id, driver_code, full_name, mobile AS mobile_number,
                       license_number, status, NULL AS city_id, created_at
                FROM drivers {where_sql}
                ORDER BY created_at DESC
                LIMIT :limit OFFSET :offset
            """
            ),
            params,
        )
        return {
            "items": [dict(r) for r in rows.mappings().all()],
            "total": int(total),
            "page": page,
            "page_size": page_size,
        }

    @staticmethod
    async def set_driver_status(db: AsyncSession, driver_id: int, status: str) -> bool:
        r = await db.execute(
            text(
                "UPDATE drivers SET status = :status, updated_at = NOW() WHERE id = :id"
            ),
            {"status": status, "id": driver_id},
        )
        await db.commit()
        return r.rowcount > 0

    # ── §10 Vehicle Management ───────────────────────────────────────────────
    @staticmethod
    async def list_vehicles(
        db: AsyncSession,
        status: Optional[str] = None,
        search: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        where_clauses = ["v.deleted_at IS NULL"]
        params: Dict[str, Any] = {"limit": page_size, "offset": (page - 1) * page_size}

        if status:
            where_clauses.append("v.status = :status")
            params["status"] = status

        if search and search.strip():
            where_clauses.append(
                "(v.registration_number ILIKE :search OR v.vehicle_code ILIKE :search "
                "OR v.vehicle_brand ILIKE :search OR v.vehicle_model ILIKE :search "
                "OR p.business_name ILIKE :search OR p.owner_name ILIKE :search)"
            )
            params["search"] = f"%{search.strip()}%"

        where_sql = "WHERE " + " AND ".join(where_clauses)
        total = (
            await db.execute(
                text(
                    "SELECT COUNT(*) FROM vehicles v "
                    "LEFT JOIN partners p ON p.id = v.partner_id "
                    f"{where_sql}"
                ),
                params,
            )
        ).scalar() or 0
        rows = await db.execute(
            text(
                f"""
                SELECT v.id, v.vehicle_code, v.registration_number,
                       v.vehicle_brand AS make, v.vehicle_model AS model,
                       v.vehicle_category_id, v.status, v.partner_id, v.created_at,
                       vc.category_name AS vehicle_category,
                       p.owner_name AS partner_name,
                       p.business_name AS partner_business,
                       p.mobile AS partner_mobile,
                       ci.name AS city_name,
                       v.city_id
                FROM vehicles v
                LEFT JOIN vehicle_categories vc ON vc.id = v.vehicle_category_id
                LEFT JOIN partners p ON p.id = v.partner_id
                LEFT JOIN cities ci ON ci.id = v.city_id
                {where_sql}
                ORDER BY v.created_at DESC
                LIMIT :limit OFFSET :offset
            """
            ),
            params,
        )
        return {
            "items": [dict(r) for r in rows.mappings().all()],
            "total": int(total),
            "page": page,
            "page_size": page_size,
        }

    @staticmethod
    async def set_vehicle_status(
        db: AsyncSession, vehicle_id: int, status: str
    ) -> bool:
        r = await db.execute(
            text(
                "UPDATE vehicles SET status = :status, updated_at = NOW() WHERE id = :id"
            ),
            {"status": status, "id": vehicle_id},
        )
        await db.commit()
        return r.rowcount > 0

    # ── §11 Booking Operations ───────────────────────────────────────────────
    @staticmethod
    async def list_bookings(
        db: AsyncSession,
        booking_status: Optional[str] = None,
        payment_status: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        where_clauses: List[str] = []
        params: Dict[str, Any] = {"limit": page_size, "offset": (page - 1) * page_size}

        if booking_status:
            where_clauses.append("booking_status = :booking_status")
            params["booking_status"] = booking_status
        if payment_status:
            where_clauses.append("payment_status = :payment_status")
            params["payment_status"] = payment_status

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        total = (
            await db.execute(
                text(f"SELECT COUNT(*) FROM master_bookings {where_sql}"), params
            )
        ).scalar() or 0
        rows = await db.execute(
            text(
                f"""
                SELECT
                    mb.id, mb.uuid, mb.booking_number, mb.booking_status,
                    mb.payment_status, mb.total_amount,
                    mb.journey_start_date, mb.journey_end_date,
                    mb.city_id, mb.created_at,
                    u.first_name || ' ' || COALESCE(u.last_name, '') AS customer_name,
                    u.mobile_number AS customer_mobile
                FROM master_bookings mb
                LEFT JOIN customers c ON c.id = mb.customer_id
                LEFT JOIN users u ON u.id = c.user_id
                {where_sql}
                ORDER BY mb.created_at DESC
                LIMIT :limit OFFSET :offset
            """
            ),
            params,
        )
        return {
            "items": [dict(r) for r in rows.mappings().all()],
            "total": int(total),
            "page": page,
            "page_size": page_size,
        }

    @staticmethod
    async def get_booking(
        db: AsyncSession, booking_id: int
    ) -> Optional[Dict[str, Any]]:
        r = await db.execute(
            text(
                """
                SELECT
                    mb.id, mb.uuid, mb.booking_number, mb.booking_status,
                    mb.payment_status, mb.total_amount,
                    mb.total_paid_amount, mb.total_refund_amount,
                    mb.journey_start_date, mb.journey_end_date,
                    mb.remarks, mb.city_id, mb.created_at, mb.updated_at,
                    u.first_name || ' ' || COALESCE(u.last_name, '') AS customer_name,
                    u.mobile_number AS customer_mobile
                FROM master_bookings mb
                LEFT JOIN customers c ON c.id = mb.customer_id
                LEFT JOIN users u ON u.id = c.user_id
                WHERE mb.id = :id
            """
            ),
            {"id": booking_id},
        )
        row = r.mappings().one_or_none()
        return dict(row) if row else None

    @staticmethod
    async def cancel_booking(db: AsyncSession, booking_id: int, reason: str) -> bool:
        r = await db.execute(
            text(
                """
                UPDATE master_bookings
                SET booking_status = 'CANCELLED',
                    remarks = :reason,
                    updated_at = NOW()
                WHERE id = :id AND booking_status NOT IN ('COMPLETED','CANCELLED')
            """
            ),
            {"id": booking_id, "reason": reason},
        )
        await db.commit()
        return r.rowcount > 0

    @staticmethod
    async def assign_partner(
        db: AsyncSession, booking_id: int, partner_id: int
    ) -> bool:
        """Assign partner to first cab_booking under this master_booking via cab_booking_assignments."""
        cab_r = await db.execute(
            text(
                "SELECT id FROM cab_bookings WHERE master_booking_id = :mbid ORDER BY id LIMIT 1"
            ),
            {"mbid": booking_id},
        )
        cab_id = cab_r.scalar_one_or_none()
        if not cab_id:
            return False
        await db.execute(
            text(
                """
                INSERT INTO cab_booking_assignments (cab_booking_id, partner_id, assignment_type, assigned_at)
                VALUES (:cab_id, :partner_id, 'MANUAL', NOW())
                ON CONFLICT DO NOTHING
            """
            ),
            {"cab_id": cab_id, "partner_id": partner_id},
        )
        await db.commit()
        return True

    @staticmethod
    async def assign_driver(db: AsyncSession, booking_id: int, driver_id: int) -> bool:
        """Assign driver to first cab_booking under this master_booking."""
        cab_r = await db.execute(
            text(
                "SELECT id FROM cab_bookings WHERE master_booking_id = :mbid ORDER BY id LIMIT 1"
            ),
            {"mbid": booking_id},
        )
        cab_id = cab_r.scalar_one_or_none()
        if not cab_id:
            return False
        await db.execute(
            text(
                """
                UPDATE cab_booking_assignments
                SET driver_id = :driver_id
                WHERE cab_booking_id = :cab_id
            """
            ),
            {"cab_id": cab_id, "driver_id": driver_id},
        )
        await db.commit()
        return True

    # ── §16 Settlement Management ────────────────────────────────────────────
    @staticmethod
    async def list_settlements(
        db: AsyncSession,
        status: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        where_clauses: List[str] = []
        params: Dict[str, Any] = {"limit": page_size, "offset": (page - 1) * page_size}

        if status:
            where_clauses.append("settlement_status = :status")
            params["status"] = status

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        total = (
            await db.execute(
                text(f"SELECT COUNT(*) FROM settlements {where_sql}"), params
            )
        ).scalar() or 0
        rows = await db.execute(
            text(
                f"""
                SELECT id, settlement_number, partner_id, net_payable_amount, settlement_status AS status, created_at
                FROM settlements {where_sql}
                ORDER BY created_at DESC
                LIMIT :limit OFFSET :offset
            """
            ),
            params,
        )
        return {
            "items": [dict(r) for r in rows.mappings().all()],
            "total": int(total),
            "page": page,
            "page_size": page_size,
        }

    @staticmethod
    async def update_settlement_status(
        db: AsyncSession,
        settlement_id: int,
        status: str,
        actor_id: Optional[str] = None,
    ) -> bool:
        """Move a statement along the documented settlement flow.

        This used to write `SET status`, `paid_at` and `updated_at` — three
        columns `settlements` does not have (`settlement_status` and `settled_at`
        are the real ones, and there is no `updated_at`), so every call failed at
        the database. It also committed inside a service although `get_db`
        auto-commits. The transition rules now live in one place,
        `settlement_statements.transition_statement`, so the two dashboard
        endpoints and the new status route cannot disagree about them.
        """
        from app.modules.admin.services import settlement_statements as statements

        await statements.transition_statement(
            db, settlement_id=settlement_id, to_status=status, actor_id=actor_id
        )
        return True

    # ── §22 Notifications Broadcast ──────────────────────────────────────────
    @staticmethod
    async def broadcast(
        db: AsyncSession, channel: str, message: str, title: Optional[str]
    ) -> int:
        """Queue broadcast to all active users via the chosen channel."""
        users_r = await db.execute(
            text("SELECT id FROM users WHERE is_active = TRUE AND status = 'ACTIVE'")
        )
        user_ids = [row[0] for row in users_r.fetchall()]

        if not user_ids:
            return 0

        if channel == "SMS":
            # Queue to sms_queue via users' mobile numbers
            await db.execute(
                text(
                    """
                    INSERT INTO sms_queue (mobile_number, message, status, created_at)
                    SELECT mobile_number, :message, 'QUEUED', NOW()
                    FROM users WHERE id = ANY(:ids) AND mobile_number IS NOT NULL
                """
                ),
                {"message": message, "ids": [str(uid) for uid in user_ids]},
            )
        elif channel in ("PUSH", "IN_APP"):
            for uid in user_ids:
                await db.execute(
                    text(
                        """
                        INSERT INTO notifications
                            (user_id, notification_type, channel, title, message, status, created_at)
                        VALUES (:uid, 'BROADCAST', :channel, :title, :message, 'QUEUED', NOW())
                    """
                    ),
                    {
                        "uid": str(uid),
                        "channel": channel,
                        "title": title,
                        "message": message,
                    },
                )
        elif channel == "EMAIL":
            await db.execute(
                text(
                    """
                    INSERT INTO email_queue (email_address, subject, body, status, created_at)
                    SELECT email, :title, :message, 'QUEUED', NOW()
                    FROM users WHERE id = ANY(:ids) AND email IS NOT NULL
                """
                ),
                {
                    "title": title or "WayTero Announcement",
                    "message": message,
                    "ids": [str(uid) for uid in user_ids],
                },
            )
        elif channel == "WHATSAPP":
            await db.execute(
                text(
                    """
                    INSERT INTO whatsapp_queue (mobile_number, payload, status, created_at)
                    SELECT mobile_number,
                           jsonb_build_object('message', :message),
                           'QUEUED', NOW()
                    FROM users WHERE id = ANY(:ids) AND mobile_number IS NOT NULL
                """
                ),
                {"message": message, "ids": [str(uid) for uid in user_ids]},
            )

        await db.commit()
        return len(user_ids)

    # ── §23 Reports ──────────────────────────────────────────────────────────
    @staticmethod
    async def report_revenue(
        db: AsyncSession, period: str = "monthly"
    ) -> Dict[str, Any]:
        if period == "daily":
            trunc = "day"
        elif period == "weekly":
            trunc = "week"
        else:
            trunc = "month"

        r = await db.execute(
            text(
                f"""
            SELECT
                COALESCE(SUM(amount), 0) AS total_revenue,
                COUNT(*) AS total_bookings
            FROM payments
            WHERE payment_status = 'SUCCESS'
              AND created_at >= DATE_TRUNC('{trunc}', NOW())
        """
            )
        )
        row = r.mappings().one()
        total_rev = Decimal(str(row["total_revenue"] or 0))
        total_bk = int(row["total_bookings"] or 0)
        avg = (total_rev / total_bk) if total_bk else Decimal("0")
        return {
            "period": period,
            "total_revenue": total_rev,
            "total_bookings": total_bk,
            "avg_booking_value": avg.quantize(Decimal("0.01")),
        }

    @staticmethod
    async def report_bookings(
        db: AsyncSession, period: str = "monthly"
    ) -> Dict[str, Any]:
        trunc = (
            "day" if period == "daily" else "week" if period == "weekly" else "month"
        )
        r = await db.execute(
            text(
                f"""
            SELECT
                COUNT(*) AS total_bookings,
                COUNT(*) FILTER (WHERE booking_status = 'COMPLETED') AS completed,
                COUNT(*) FILTER (WHERE booking_status = 'CANCELLED') AS cancelled,
                COUNT(*) FILTER (WHERE booking_status NOT IN ('COMPLETED','CANCELLED')) AS pending
            FROM master_bookings
            WHERE created_at >= DATE_TRUNC('{trunc}', NOW())
        """
            )
        )
        row = r.mappings().one()
        return {
            "period": period,
            "total_bookings": int(row["total_bookings"] or 0),
            "completed": int(row["completed"] or 0),
            "cancelled": int(row["cancelled"] or 0),
            "pending": int(row["pending"] or 0),
        }

    @staticmethod
    async def report_partners(db: AsyncSession) -> Dict[str, Any]:
        r = await db.execute(
            text(
                """
            SELECT
                COUNT(*) AS total_partners,
                COUNT(*) FILTER (WHERE status = 'ACTIVE') AS active,
                COUNT(*) FILTER (WHERE status = 'PENDING') AS pending,
                COUNT(*) FILTER (WHERE status = 'SUSPENDED') AS suspended
            FROM partners
        """
            )
        )
        row = r.mappings().one()
        return {
            "total_partners": int(row["total_partners"] or 0),
            "active": int(row["active"] or 0),
            "pending": int(row["pending"] or 0),
            "suspended": int(row["suspended"] or 0),
        }

    @staticmethod
    async def report_settlements(db: AsyncSession) -> Dict[str, Any]:
        r = await db.execute(
            text(
                """
            SELECT
                COUNT(*) AS total_settlements,
                COALESCE(SUM(net_payable_amount) FILTER (WHERE settlement_status = 'PENDING'), 0) AS pending_amount,
                COALESCE(SUM(net_payable_amount) FILTER (WHERE settlement_status = 'PAID'), 0) AS paid_amount,
                COUNT(*) FILTER (WHERE settlement_status = 'PENDING') AS pending_count
            FROM settlements
        """
            )
        )
        row = r.mappings().one()
        return {
            "total_settlements": int(row["total_settlements"] or 0),
            "pending_amount": Decimal(str(row["pending_amount"] or 0)),
            "paid_amount": Decimal(str(row["paid_amount"] or 0)),
            "pending_count": int(row["pending_count"] or 0),
        }

    # ── §24 Audit Logs ───────────────────────────────────────────────────────
    @staticmethod
    async def list_audit_logs(
        db: AsyncSession,
        user_id: Optional[int] = None,
        action_type: Optional[str] = None,
        module: Optional[str] = None,
        entity_name: Optional[str] = None,
        q: Optional[str] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        where_clauses: List[str] = []
        params: Dict[str, Any] = {"limit": page_size, "offset": (page - 1) * page_size}

        if user_id is not None:
            where_clauses.append("user_id = :user_id")
            params["user_id"] = user_id
        if action_type:
            where_clauses.append("action_type = :action_type")
            params["action_type"] = action_type
        if module:
            where_clauses.append("module_name ILIKE :module")
            params["module"] = f"%{module}%"
        if entity_name:
            where_clauses.append("entity_name = :entity_name")
            params["entity_name"] = entity_name
        if q and q.strip():
            where_clauses.append(
                "(module_name ILIKE :q OR action_type ILIKE :q "
                "OR entity_name ILIKE :q)"
            )
            params["q"] = f"%{q.strip()}%"
        if date_from is not None:
            where_clauses.append("created_at >= :date_from")
            params["date_from"] = date_from
        if date_to is not None:
            where_clauses.append("created_at <= :date_to")
            params["date_to"] = date_to

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        total = (
            await db.execute(
                text(f"SELECT COUNT(*) FROM audit_logs {where_sql}"), params
            )
        ).scalar() or 0
        rows = await db.execute(
            text(
                f"""
                SELECT id, user_id, module_name, entity_name, entity_id,
                       action_type, old_values, new_values,
                       ip_address, user_agent, request_id, created_at
                FROM audit_logs {where_sql}
                ORDER BY created_at DESC
                LIMIT :limit OFFSET :offset
            """
            ),
            params,
        )
        return {
            "items": [dict(r) for r in rows.mappings().all()],
            "total": int(total),
            "page": page,
            "page_size": page_size,
        }

    @staticmethod
    async def list_audit_logs_summary(db: AsyncSession) -> Dict[str, Any]:
        """
        Aggregate counts for the admin audit header strip.

        Three small reads — no transactions needed; small races are
        acceptable because the summary powers a UI tile, not a control.
        """
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = today_start - timedelta(days=7)

        # Total + today + this-week in one round trip.
        row = (
            (
                await db.execute(
                    text(
                        """
                SELECT
                    COUNT(*)                                                AS total,
                    COUNT(*) FILTER (WHERE created_at >= :today_start)      AS today,
                    COUNT(*) FILTER (WHERE created_at >= :week_start)       AS this_week,
                    COUNT(DISTINCT user_id)
                        FILTER (WHERE created_at >= :today_start
                                AND user_id IS NOT NULL)                    AS unique_users_today
                FROM audit_logs
            """
                    ),
                    {"today_start": today_start, "week_start": week_start},
                )
            )
            .mappings()
            .one()
        )

        # Top-N modules and action types — used by the chip strip and the
        # "events by type" widget on the page.
        module_rows = (
            (
                await db.execute(
                    text(
                        """
                SELECT module_name, COUNT(*) AS n
                FROM audit_logs
                WHERE created_at >= :week_start
                GROUP BY module_name
                ORDER BY n DESC
                LIMIT 8
            """
                    ),
                    {"week_start": week_start},
                )
            )
            .mappings()
            .all()
        )

        action_rows = (
            (
                await db.execute(
                    text(
                        """
                SELECT action_type, COUNT(*) AS n
                FROM audit_logs
                WHERE created_at >= :week_start
                GROUP BY action_type
                ORDER BY n DESC
                LIMIT 10
            """
                    ),
                    {"week_start": week_start},
                )
            )
            .mappings()
            .all()
        )

        return {
            "total": int(row["total"] or 0),
            "today": int(row["today"] or 0),
            "this_week": int(row["this_week"] or 0),
            "unique_users_today": int(row["unique_users_today"] or 0),
            "by_module": {r["module_name"]: int(r["n"]) for r in module_rows},
            "by_action_type": {r["action_type"]: int(r["n"]) for r in action_rows},
        }

    # ── Staff User Management ─────────────────────────────────────────────────
    # Doc Ref: BRD Part 2 §12 — Admin, CCO, Verification Officer, Finance Manager
    # Doc Ref: BRD Part 8 §196 — Admin User Types
    # Staff = internal users created by Super Admin (not self-registered)

    @staticmethod
    async def list_staff_users(
        db: AsyncSession,
        role: Optional[str] = None,
        status: Optional[str] = None,
        search: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
        scope: Optional[BranchScope] = None,
    ) -> Dict[str, Any]:
        """List internal staff members only (excludes CUSTOMER, PARTNER, DRIVER).

        Two filters the register cannot do without (migration 0085):

          * a branch manager sees their own branch's people, never another
            office's — `scope` carries that, resolved from the caller's row;
          * SUPER_ADMIN is not a hireable role and not a row a manager may
            enumerate, so it leaves the visible set for everyone but a super
            admin.

        Without a `scope` the caller is unscoped (a super admin browsing all
        branches) — that is the pre-0085 behaviour, kept for callers that have
        no branch concept.
        """
        visible_roles = (
            STAFF_LIST_ROLES
            if scope is None or scope.is_super
            else MANAGER_VISIBLE_ROLES
        )
        placeholders = ",".join(f"'{r}'" for r in visible_roles)
        conditions = [f"u.user_type IN ({placeholders})"]
        params: Dict[str, Any] = {"limit": page_size, "offset": (page - 1) * page_size}

        if scope is not None and scope.restricted:
            conditions.append("u.branch_id = :scope_branch_id")
            params["scope_branch_id"] = scope.branch_id
        if role:
            conditions.append("u.user_type = :role")
            params["role"] = role
        if status:
            conditions.append("u.status = :status")
            params["status"] = status
        if search:
            conditions.append(
                "(u.mobile_number ILIKE :search OR u.email ILIKE :search"
                " OR u.first_name ILIKE :search OR u.last_name ILIKE :search)"
            )
            params["search"] = f"%{search}%"

        where_sql = "WHERE " + " AND ".join(conditions)
        from_sql = (
            "FROM users u "
            "LEFT JOIN branches b ON b.id = u.branch_id "
            "LEFT JOIN staff_profiles sp ON sp.user_id = u.id"
        )

        count_r = await db.execute(
            text(f"SELECT COUNT(*) {from_sql} {where_sql}"),
            params,
        )
        total = count_r.scalar() or 0

        rows_r = await db.execute(
            text(
                f"""
                SELECT
                    u.id, u.user_code, u.first_name, u.last_name, u.mobile_number,
                    u.email, u.user_type, u.status, u.is_active, u.profile_image_url,
                    u.created_at, u.last_login_at,
                    u.branch_id, b.name AS branch_name,
                    sp.id AS profile_id,
                    sp.designation, sp.department, sp.joining_date, sp.employee_id,
                    sp.city, sp.state,
                    sp.id_card_status, sp.id_card_generated_at
                {from_sql}
                {where_sql}
                ORDER BY u.created_at DESC
                LIMIT :limit OFFSET :offset
            """
            ),
            params,
        )
        items = []
        for row in rows_r.mappings().all():
            d = dict(row)
            profile_summary = {
                "designation": d.pop("designation", None),
                "department": d.pop("department", None),
                "joining_date": d.pop("joining_date", None),
                "employee_id": d.pop("employee_id", None),
                "city": d.pop("city", None),
                "state": d.pop("state", None),
                "id_card_status": d.pop("id_card_status", None),
                "id_card_generated_at": d.pop("id_card_generated_at", None),
            }
            d["profile"] = profile_summary if d.pop("profile_id", None) else None
            items.append(d)
        return {
            "items": items,
            "total": int(total),
            "page": page,
            "page_size": page_size,
        }

    @staticmethod
    async def get_staff_user(
        db: AsyncSession, user_id: UUID, scope: Optional[BranchScope] = None
    ) -> Optional[Dict[str, Any]]:
        """Fetch staff user with extended profile joined from staff_profiles.
        Doc Ref: DB 0014_staff_profiles

        A `scope` narrows the read to the caller's branch, and a row outside it
        comes back None so the route answers 404 — the same shape as "no such
        staff member", which is the point (see branch_scope rule 3).
        """
        branch_clause = ""
        params: Dict[str, Any] = {"user_id": str(user_id)}
        if scope is not None and scope.restricted:
            branch_clause = " AND u.branch_id = :scope_branch_id"
            params["scope_branch_id"] = scope.branch_id

        r = await db.execute(
            text(
                f"""
                SELECT
                    u.id, u.user_code, u.first_name, u.last_name, u.mobile_number,
                    u.email, u.user_type, u.status, u.is_active, u.profile_image_url,
                    u.created_at, u.last_login_at,
                    u.branch_id, b.name AS branch_name,
                    sp.id AS profile_id,
                    sp.date_of_birth, sp.gender, sp.blood_group, sp.personal_email,
                    sp.emergency_contact_name, sp.emergency_contact_phone,
                    sp.designation, sp.department, sp.employment_type, sp.joining_date,
                    sp.employee_id,
                    sp.address_line1, sp.address_line2, sp.city, sp.state, sp.pincode, sp.country,
                    sp.pan_number, sp.aadhar_number, sp.driving_license_number,
                    sp.bank_name, sp.bank_account_number, sp.bank_ifsc_code,
                    sp.bank_branch, sp.bank_account_type,
                    sp.nominee_name, sp.nominee_relation, sp.nominee_phone, sp.nominee_address,
                    sp.id_card_status, sp.id_card_generated_at, sp.id_card_notes
                FROM users u
                LEFT JOIN branches b ON b.id = u.branch_id
                LEFT JOIN staff_profiles sp ON sp.user_id = u.id
                WHERE u.id = :user_id
                  AND u.user_type IN ('ADMIN','CCO','VERIFICATION_OFFICER','FINANCE_MANAGER','SUPER_ADMIN')
                  {branch_clause}
            """
            ),
            params,
        )
        row = r.mappings().one_or_none()
        if not row:
            return None
        d = dict(row)
        # Nest profile fields
        profile_keys = [
            "date_of_birth",
            "gender",
            "blood_group",
            "personal_email",
            "emergency_contact_name",
            "emergency_contact_phone",
            "designation",
            "department",
            "employment_type",
            "joining_date",
            "employee_id",
            "address_line1",
            "address_line2",
            "city",
            "state",
            "pincode",
            "country",
            "pan_number",
            "aadhar_number",
            "driving_license_number",
            "bank_name",
            "bank_account_number",
            "bank_ifsc_code",
            "bank_branch",
            "bank_account_type",
            "nominee_name",
            "nominee_relation",
            "nominee_phone",
            "nominee_address",
            "id_card_status",
            "id_card_generated_at",
            "id_card_notes",
        ]
        profile_data = {k: d.pop(k) for k in profile_keys if k in d}
        if d.pop("profile_id", None):
            profile_data["user_id"] = str(d["id"])
            d["profile"] = profile_data
        else:
            d["profile"] = None
        return d

    @staticmethod
    async def _upsert_staff_profile(
        db: AsyncSession,
        user_id: UUID,
        profile_data: "StaffProfileCreate",
    ) -> None:
        """Create or update staff_profiles row for the given user.
        Doc Ref: DB 0014_staff_profiles
        """
        import uuid as _uuid

        existing = await db.execute(
            text("SELECT id FROM staff_profiles WHERE user_id = :uid"),
            {"uid": str(user_id)},
        )
        profile_row = existing.scalar_one_or_none()

        fields = {
            "date_of_birth": profile_data.date_of_birth,
            "gender": profile_data.gender,
            "blood_group": profile_data.blood_group,
            "personal_email": profile_data.personal_email,
            "emergency_contact_name": profile_data.emergency_contact_name,
            "emergency_contact_phone": profile_data.emergency_contact_phone,
            "designation": profile_data.designation,
            "department": profile_data.department,
            "employment_type": profile_data.employment_type or "FULL_TIME",
            "joining_date": profile_data.joining_date,
            "address_line1": profile_data.address_line1,
            "address_line2": profile_data.address_line2,
            "city": profile_data.city,
            "state": profile_data.state,
            "pincode": profile_data.pincode,
            "country": profile_data.country or "India",
            "pan_number": profile_data.pan_number,
            "aadhar_number": profile_data.aadhar_number,
            "driving_license_number": profile_data.driving_license_number,
            "bank_name": profile_data.bank_name,
            "bank_account_number": profile_data.bank_account_number,
            "bank_ifsc_code": profile_data.bank_ifsc_code,
            "bank_branch": profile_data.bank_branch,
            "bank_account_type": profile_data.bank_account_type,
            "nominee_name": profile_data.nominee_name,
            "nominee_relation": profile_data.nominee_relation,
            "nominee_phone": profile_data.nominee_phone,
            "nominee_address": profile_data.nominee_address,
            "id_card_notes": profile_data.id_card_notes,
        }

        if not profile_row:
            # ── Auto-generate employee_id (EMP-XXXXXX) on first profile creation
            # Doc Ref: DB 0014_staff_profiles — employee_id UNIQUE VARCHAR(50)
            count_r = await db.execute(text("SELECT COUNT(*) FROM staff_profiles"))
            seq = (count_r.scalar() or 0) + 1
            auto_employee_id = f"EMP-{seq:06d}"
            fields["employee_id"] = auto_employee_id

            profile_id = _uuid.uuid4()
            col_names = ", ".join(
                ["id", "user_id"] + list(fields.keys()) + ["created_at", "updated_at"]
            )
            placeholders = ", ".join(
                [":id", ":user_id"]
                + [f":{k}" for k in fields.keys()]
                + ["NOW()", "NOW()"]
            )
            await db.execute(
                text(
                    f"INSERT INTO staff_profiles ({col_names}) VALUES ({placeholders})"
                ),
                {"id": str(profile_id), "user_id": str(user_id), **fields},
            )
        else:
            # ── Backfill employee_id if somehow NULL (e.g. staff registered before this fix)
            # Doc Ref: DB 0014_staff_profiles — employee_id UNIQUE VARCHAR(50)
            existing_emp = await db.execute(
                text("SELECT employee_id FROM staff_profiles WHERE user_id = :uid"),
                {"uid": str(user_id)},
            )
            existing_emp_id = existing_emp.scalar_one_or_none()
            if not existing_emp_id:
                count_r = await db.execute(text("SELECT COUNT(*) FROM staff_profiles"))
                seq = (count_r.scalar() or 0) + 1
                auto_employee_id = f"EMP-{seq:06d}"
                fields["employee_id"] = auto_employee_id

            set_clauses = ", ".join(
                [f"{k} = :{k}" for k in fields.keys()] + ["updated_at = NOW()"]
            )
            await db.execute(
                text(
                    f"UPDATE staff_profiles SET {set_clauses} WHERE user_id = :user_id"
                ),
                {"user_id": str(user_id), **fields},
            )

    @staticmethod
    async def create_staff_user(
        db: AsyncSession,
        payload: "StaffUserCreate",
        created_by: Optional[UUID] = None,
        scope: Optional[BranchScope] = None,
    ) -> Dict[str, Any]:
        """Create an internal staff member account.
        Doc Ref: BRD Part 2 §12 — Password Login for Admin/CCO/Officer

        `scope` decides which office the new account belongs to, and it is not
        optional any more: `ck_users_branch_membership` (migration 0085) rejects
        a staff row with no branch, so an insert that skips this decision fails
        at the database.

        Two owner rulings live here:

          * a branch manager hires for their own branch only — the role list is
            CCO / Verification Officer / Finance Manager, and the branch is
            forced, not taken from the body;
          * creating a branch manager (ADMIN), or filing anyone under a
            different office, is a super admin act.

        SUPER_ADMIN is unreachable from this function at all: the schema
        validator refuses it and the branch CHECK would too, since a master
        account has no office.
        """
        from app.core.security import hash_password
        from app.core.branch_scope import load_active_branch
        import uuid as _uuid

        if payload.user_type == "SUPER_ADMIN":
            raise PermissionDeniedException(
                "A master admin account cannot be created here.",
                code="SUPER_ADMIN_NOT_CREATABLE",
            )

        if scope is None:
            raise ValidationException(
                "Staff accounts belong to a branch; this caller has no branch "
                "scope, so there is no office to file them under."
            )
        if scope.is_super:
            if payload.branch_id is None:
                raise ValidationException(
                    "Choose the branch this person will work from."
                )
            branch_id = (await load_active_branch(db, payload.branch_id)).id
        else:
            if payload.user_type == "ADMIN":
                raise PermissionDeniedException(
                    "Only a super admin can create a branch manager.",
                    code="STAFF_ADMIN_CREATION_SUPER_ONLY",
                )
            if payload.branch_id not in (None, scope.branch_id):
                raise PermissionDeniedException(
                    "You can only hire into your own branch.",
                    code="STAFF_BRANCH_OUT_OF_SCOPE",
                )
            branch_id = scope.branch_id

        # Check duplicate email / mobile
        dup = await db.execute(
            text(
                "SELECT id FROM users WHERE email = :email OR mobile_number = :mobile LIMIT 1"
            ),
            {"email": payload.email, "mobile": payload.mobile_number},
        )
        if dup.scalar_one_or_none():
            raise ValueError("Email or mobile number already registered")

        user_id = _uuid.uuid4()
        password_hash = hash_password(payload.password)

        # Generate user_code: ADMIN → ADM-000001, CCO → CCO-000001, etc.
        prefix_map = {
            "ADMIN": "ADM",
            "CCO": "CCO",
            "VERIFICATION_OFFICER": "VER",
            "FINANCE_MANAGER": "FIN",
            "SUPER_ADMIN": "SUP",
        }
        prefix = prefix_map.get(payload.user_type, "STF")
        count_r = await db.execute(
            text("SELECT COUNT(*) FROM users WHERE user_type = :ut"),
            {"ut": payload.user_type},
        )
        seq = (count_r.scalar() or 0) + 1
        user_code = f"{prefix}-{seq:06d}"

        await db.execute(
            text(
                """
                INSERT INTO users (
                    id, user_code, first_name, last_name, mobile_number, email,
                    password_hash, user_type, status, is_active, branch_id,
                    is_mobile_verified, is_email_verified,
                    profile_image_url, created_by, created_at, updated_at
                ) VALUES (
                    :id, :user_code, :first_name, :last_name, :mobile_number, :email,
                    :password_hash, :user_type, 'ACTIVE', TRUE, :branch_id,
                    FALSE, FALSE,
                    :profile_image_url, :created_by, NOW(), NOW()
                )
            """
            ),
            {
                "id": str(user_id),
                "user_code": user_code,
                "first_name": payload.first_name,
                "last_name": payload.last_name,
                "mobile_number": payload.mobile_number,
                "email": payload.email,
                "password_hash": password_hash,
                "user_type": payload.user_type,
                "branch_id": branch_id,
                "profile_image_url": payload.profile_image_url,
                "created_by": str(created_by) if created_by else None,
            },
        )
        await db.commit()

        # Always ensure a staff_profiles row exists with an auto-generated employee_id
        # Doc Ref: DB 0014_staff_profiles — employee_id must never be NULL for staff
        from app.modules.admin.schemas.dashboard import StaffProfileCreate as _SPC

        profile_payload = payload.profile if payload.profile else _SPC()
        await AdminDashboardService._upsert_staff_profile(db, user_id, profile_payload)
        await db.commit()

        return await AdminDashboardService.get_staff_user(db, user_id)

    @staticmethod
    async def assert_staff_row(
        db: AsyncSession,
        scope: Optional[BranchScope],
        user_id: UUID,
        *,
        for_write: bool = False,
    ) -> Dict[str, Any]:
        """The staff row this caller is about to read or write, or the refusal.

        Answers, deliberately different:

          * not a staff row, or another branch's → 404. A 403 would confirm to a
            Cuttack manager that this id exists and is Bhubaneswar's. (A
            SUPER_ADMIN row lands here too: its branch is NULL, so it matches no
            branch — which is also why no manager can demote one.)
          * writing the ADMIN who heads a branch, by anyone but a super admin →
            403. Reading one is fine: the branch manager's own account is an
            ADMIN row, and their list already shows the office's managers.
          * a super admin, or the caller's own branch → the row.

        Returns the row so a caller that then needs `user_type` (the last-master
        guard below) does not read it a second time after the UPDATE.
        """
        row = (
            (
                await db.execute(
                    text(
                        "SELECT u.user_type, u.branch_id "
                        "FROM users u "
                        "WHERE u.id = :user_id "
                        "AND u.user_type IN ('ADMIN','CCO','VERIFICATION_OFFICER',"
                        "                    'FINANCE_MANAGER','SUPER_ADMIN')"
                    ),
                    {"user_id": str(user_id)},
                )
            )
            .mappings()
            .one_or_none()
        )

        if row is None:
            raise ResourceNotFoundException("Staff user", user_id)
        if scope is None:
            return dict(row)

        if scope.restricted:
            if row["branch_id"] != scope.branch_id:
                raise ResourceNotFoundException("Staff user", user_id)
            is_self = str(user_id) == scope.actor_id
            if for_write and row["user_type"] == "ADMIN" and not is_self:
                raise PermissionDeniedException(
                    "Only a super admin can change a branch manager.",
                    code="STAFF_ADMIN_ROW_SUPER_ONLY",
                )
        return dict(row)

    @staticmethod
    async def refuse_losing_last_super_admin(
        db: AsyncSession, row: Dict[str, Any], *, would_end_mastership: bool
    ) -> None:
        """Keep at least one account that can assign branches.

        Nothing else can reach this: a branch manager is refused by
        assert_staff_row_writable, and no route can create a SUPER_ADMIN at all.
        So the way this platform loses its master admin is an edit that demotes
        or suspends the only one there is.
        """
        if row["user_type"] != "SUPER_ADMIN" or not would_end_mastership:
            return
        count = (
            await db.execute(
                text(
                    "SELECT COUNT(*) FROM users "
                    "WHERE user_type = 'SUPER_ADMIN' AND is_active = TRUE"
                )
            )
        ).scalar() or 0
        if count <= 1:
            raise BusinessException(
                "This is the only master admin account. Another has to exist "
                "before this one can be demoted or suspended.",
                code="LAST_SUPER_ADMIN",
            )

    @staticmethod
    async def update_staff_user(
        db: AsyncSession,
        user_id: UUID,
        payload: "StaffUserUpdate",
        scope: Optional[BranchScope] = None,
    ) -> Optional[Dict[str, Any]]:
        # Before anything else: whose row is this, and may this caller touch it?
        row = await AdminDashboardService.assert_staff_row(
            db, scope, user_id, for_write=True
        )

        updates = []
        params: Dict[str, Any] = {"user_id": str(user_id)}

        if payload.first_name is not None:
            updates.append("first_name = :first_name")
            params["first_name"] = payload.first_name
        if payload.last_name is not None:
            updates.append("last_name = :last_name")
            params["last_name"] = payload.last_name
        if payload.email is not None:
            updates.append("email = :email")
            params["email"] = payload.email
        if payload.mobile_number is not None:
            updates.append("mobile_number = :mobile_number")
            params["mobile_number"] = payload.mobile_number
        if payload.user_type is not None:
            await AdminDashboardService.refuse_losing_last_super_admin(
                db, row, would_end_mastership=True
            )
            updates.append("user_type = :user_type")
            params["user_type"] = payload.user_type
        if payload.branch_id is not None:
            # Moving a person between offices is the super admin's act (owner
            # decision). A manager sending it is refused rather than ignored:
            # silently dropping a field the caller meant to change is how a UI
            # ends up believing the save worked.
            if payload.branch_id != row["branch_id"]:
                if scope is not None and not scope.is_super:
                    raise PermissionDeniedException(
                        "Only a super admin can move someone to another branch.",
                        code="STAFF_BRANCH_MOVE_SUPER_ONLY",
                    )
                from app.core.branch_scope import load_active_branch

                await load_active_branch(db, payload.branch_id)
                updates.append("branch_id = :branch_id")
                params["branch_id"] = payload.branch_id
        if payload.profile_image_url is not None:
            updates.append("profile_image_url = :profile_image_url")
            params["profile_image_url"] = payload.profile_image_url

        if not updates:
            return await AdminDashboardService.get_staff_user(db, user_id)

        updates.append("updated_at = NOW()")
        await db.execute(
            text(
                f"UPDATE users SET {', '.join(updates)} WHERE id = :user_id AND user_type IN ('ADMIN','CCO','VERIFICATION_OFFICER','FINANCE_MANAGER','SUPER_ADMIN')"
            ),
            params,
        )

        branch_moved = "branch_id" in params and params["branch_id"] != row["branch_id"]
        if branch_moved:
            # Enforcement never read the branch from the token, so this is not
            # what makes the move take effect — it is here so a screen full of
            # the old branch's queues is not still open when they come back.
            from app.modules.auth.repositories import SessionRepository

            await SessionRepository(db).deactivate_all_user_sessions(user_id)

        await db.commit()

        # Always upsert profile — even if no profile fields changed, this ensures
        # any staff member with a NULL employee_id gets one auto-generated now.
        # Doc Ref: DB 0014_staff_profiles — employee_id backfill on edit save
        from app.modules.admin.schemas.dashboard import StaffProfileCreate as _SPC

        profile_payload = payload.profile if payload.profile else _SPC()
        await AdminDashboardService._upsert_staff_profile(db, user_id, profile_payload)
        await db.commit()

        return await AdminDashboardService.get_staff_user(db, user_id)

    @staticmethod
    async def generate_staff_id_card(
        db: AsyncSession,
        user_id: UUID,
        notes: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Mark staff ID card as generated and set generated_at timestamp.
        Doc Ref: DB 0014_staff_profiles — id_card_status, id_card_generated_at
        """
        r = await db.execute(
            text(
                """
                UPDATE staff_profiles
                SET id_card_status = 'GENERATED',
                    id_card_generated_at = NOW(),
                    id_card_notes = COALESCE(:notes, id_card_notes),
                    updated_at = NOW()
                WHERE user_id = :user_id
                RETURNING id
            """
            ),
            {"user_id": str(user_id), "notes": notes},
        )
        if not r.fetchone():
            return None
        await db.commit()
        return await AdminDashboardService.get_staff_user(db, user_id)

    @staticmethod
    async def reset_staff_password(
        db: AsyncSession,
        user_id: UUID,
        new_password: str,
    ) -> bool:
        from app.core.security import hash_password

        password_hash = hash_password(new_password)
        r = await db.execute(
            text(
                "UPDATE users SET password_hash = :ph, updated_at = NOW() WHERE id = :uid AND user_type IN ('ADMIN','CCO','VERIFICATION_OFFICER','FINANCE_MANAGER','SUPER_ADMIN')"
            ),
            {"ph": password_hash, "uid": str(user_id)},
        )
        await db.commit()
        return r.rowcount > 0

    # ── Staff working hours (Admin Portal §39 — leave / attendance) ──────────
    @staticmethod
    async def get_staff_work_hours(
        db: AsyncSession,
        *,
        date_from: date,
        date_to: date,
        user_id: Optional[UUID] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> Dict[str, Any]:
        """Per staff member, per day: when the portal opened, when it ended and
        how many of those minutes were actually worked.

        The day rows and the period totals come from one repository so the
        table and the totals footer cannot disagree. Read-only: nothing here
        flushes or commits.
        """
        zone = await get_platform_tz(db)
        repo = StaffWorkDailyRepository(db)
        total = await repo.count_days(
            date_from=date_from, date_to=date_to, user_id=user_id
        )
        items = await repo.list_days(
            tz_name=str(zone),
            date_from=date_from,
            date_to=date_to,
            user_id=user_id,
            limit=page_size,
            offset=(page - 1) * page_size,
        )
        totals = await repo.summarize_days(
            date_from=date_from, date_to=date_to, user_id=user_id
        )
        totals["active_minutes"] = int(round(totals["active_seconds"] / 60))
        return {
            "items": items,
            "period_totals": totals,
            "total": total,
            "page": page,
            "page_size": page_size,
            "date_from": date_from,
            "date_to": date_to,
        }

    # ── Sidebar action counts (Admin Portal §5 — Operations badges) ──────────
    @staticmethod
    async def get_action_counts(db: AsyncSession, scope: BranchScope) -> Dict[str, int]:
        """How many items in each queue are waiting on somebody right now.

        One round trip, not eleven: the sidebar polls this every minute for
        every portal that is open, and most of those queues belong to a role
        the caller cannot read, so eleven calls would be eleven 403s and eleven
        chances for one slow table to stall a badge.

        Each fragment is the WHERE clause the matching queue page already uses,
        plus this caller's branch, so a badge and the list under it cannot
        disagree. A nav key with no honest pending concept is left out rather
        than given a number that means something else: hotel switches are an
        append-only audit (no row ever waits for a switch), and settlements are
        still writing `status` while every reader filters on `settlement_status`.
        """
        cache = RedisCache(await get_redis())
        cache_key = action_counts_cache_key(scope)
        try:
            cached = await cache.get(cache_key)
        except Exception:  # Redis being down must not blank the sidebar
            cached = None
        if isinstance(cached, dict):
            return {k: int(v) for k, v in cached.items()}

        union_sql, params = _action_counts_statement(scope)
        rows = (await db.execute(text(union_sql), params)).mappings().all()
        counts = {row["nav_key"]: int(row["c"] or 0) for row in rows}
        try:
            await cache.set(cache_key, counts, ACTION_COUNTS_TTL_SECONDS)
        except Exception:
            pass
        return counts
