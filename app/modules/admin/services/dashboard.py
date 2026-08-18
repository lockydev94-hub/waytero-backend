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
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from app.modules.admin.schemas.dashboard import (
    StaffProfileCreate,
    StaffUserCreate,
    StaffUserUpdate,
)


class AdminDashboardService:

    # ── §3 Dashboard KPIs ────────────────────────────────────────────────────
    @staticmethod
    async def get_dashboard(db: AsyncSession) -> Dict[str, Any]:
        r = await db.execute(
            text(
                """
            SELECT
                (SELECT COUNT(*) FROM cab_bookings WHERE DATE(created_at) = CURRENT_DATE)
                    AS today_cab_bookings,
                (SELECT COUNT(*) FROM hotel_reservations WHERE DATE(created_at) = CURRENT_DATE)
                    AS today_hotel_reservations,
                (SELECT COUNT(*) FROM cab_bookings
                 WHERE booking_status IN ('STARTED','DRIVER_ASSIGNED'))
                    AS active_trips,
                COALESCE((
                    SELECT SUM(cb.final_amount)
                    FROM cab_bookings cb
                    WHERE cb.booking_status = 'COMPLETED'
                      AND DATE(cb.trip_ended_at) = CURRENT_DATE
                ), 0) +
                COALESCE((
                    SELECT SUM(hr.total_amount)
                    FROM hotel_reservations hr
                    WHERE hr.payment_collected_status = 'PAID'
                      AND DATE(hr.payment_collected_at) = CURRENT_DATE
                ), 0) AS today_revenue,
                COALESCE((
                    SELECT SUM(net_payable_amount) FROM settlements
                    WHERE settlement_status = 'PENDING'
                ), 0) AS pending_settlements,
                (SELECT COUNT(*) FROM partners
                 WHERE status IN ('PENDING','UNDER_REVIEW','DOCUMENT_PENDING'))
                    AS pending_partner_approvals
        """
            )
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
    async def get_system_summary(db: AsyncSession) -> Dict[str, Any]:
        r = await db.execute(
            text(
                """
            SELECT
                (SELECT COUNT(*) FROM users WHERE user_type = 'CUSTOMER') AS total_customers,
                (SELECT COUNT(*) FROM partners WHERE deleted_at IS NULL) AS total_partners,
                (SELECT COUNT(*) FROM partners WHERE status = 'ACTIVE' AND deleted_at IS NULL) AS active_partners,
                (SELECT COUNT(*) FROM hotels) AS total_hotels,
                (SELECT COUNT(*) FROM hotels WHERE status = 'ACTIVE') AS active_hotels,
                COALESCE((
                    SELECT SUM(cb.final_amount)
                    FROM cab_bookings cb
                    WHERE cb.booking_status = 'COMPLETED'
                      AND cb.trip_ended_at >= DATE_TRUNC('month', NOW())
                ), 0) +
                COALESCE((
                    SELECT SUM(hr.total_amount)
                    FROM hotel_reservations hr
                    WHERE hr.reservation_status NOT IN ('CANCELLED','EXPIRED')
                      AND hr.created_at >= DATE_TRUNC('month', NOW())
                ), 0) AS monthly_revenue,
                (SELECT COUNT(*) FROM hotels
                 WHERE status IN ('PENDING','UNDER_REVIEW','DOCUMENT_PENDING'))
                    AS pending_hotel_approvals
        """
            )
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
    async def get_city_performance(db: AsyncSession) -> List[Dict[str, Any]]:
        """Top 5 cities by combined cab + hotel bookings this month."""
        r = await db.execute(
            text(
                """
            SELECT ci.name AS city, COUNT(*) AS bookings
            FROM master_bookings mb
            JOIN cities ci ON ci.id = mb.city_id
            WHERE mb.created_at >= DATE_TRUNC('month', NOW())
            GROUP BY ci.name
            ORDER BY bookings DESC
            LIMIT 5
        """
            )
        )
        return [
            {"city": row["city"], "bookings": int(row["bookings"])}
            for row in r.mappings().all()
        ]

    @staticmethod
    async def get_revenue_trend(db: AsyncSession) -> List[Dict[str, Any]]:
        """Monthly revenue + booking count for the last 6 calendar months."""
        r = await db.execute(
            text(
                """
            WITH months AS (
                SELECT generate_series(
                    DATE_TRUNC('month', NOW()) - INTERVAL '5 months',
                    DATE_TRUNC('month', NOW()),
                    '1 month'
                ) AS month_start
            ),
            cab_rev AS (
                SELECT DATE_TRUNC('month', trip_ended_at) AS m,
                       COALESCE(SUM(final_amount), 0) AS rev,
                       COUNT(*) AS cnt
                FROM cab_bookings
                WHERE booking_status = 'COMPLETED'
                  AND trip_ended_at >= DATE_TRUNC('month', NOW()) - INTERVAL '5 months'
                GROUP BY m
            ),
            hotel_rev AS (
                SELECT DATE_TRUNC('month', created_at) AS m,
                       COALESCE(SUM(total_amount), 0) AS rev,
                       COUNT(*) AS cnt
                FROM hotel_reservations
                WHERE reservation_status NOT IN ('CANCELLED','EXPIRED')
                  AND created_at >= DATE_TRUNC('month', NOW()) - INTERVAL '5 months'
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
            )
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
        db: AsyncSession, limit: int = 10
    ) -> List[Dict[str, Any]]:
        """Latest meaningful events from booking_timelines + partner_verification_logs."""
        booking_events = await db.execute(
            text(
                """
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
            ORDER BY bt.event_timestamp DESC
            LIMIT :lim
        """
            ),
            {"lim": limit},
        )

        partner_events = await db.execute(
            text(
                """
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
            ORDER BY pvl.created_at DESC
            LIMIT :lim
        """
            ),
            {"lim": limit // 2},
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
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        where_clauses = []
        params: Dict[str, Any] = {"limit": page_size, "offset": (page - 1) * page_size}

        if status:
            where_clauses.append("status = :status")
            params["status"] = status

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
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        where_clauses = ["v.deleted_at IS NULL"]
        params: Dict[str, Any] = {"limit": page_size, "offset": (page - 1) * page_size}

        if status:
            where_clauses.append("v.status = :status")
            params["status"] = status

        where_sql = "WHERE " + " AND ".join(where_clauses)
        total = (
            await db.execute(
                text(f"SELECT COUNT(*) FROM vehicles v {where_sql}"), params
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
        db: AsyncSession, settlement_id: int, status: str
    ) -> bool:
        extra = ", paid_at = NOW()" if status == "PAID" else ""
        r = await db.execute(
            text(
                f"UPDATE settlements SET status = :status{extra}, updated_at = NOW() WHERE id = :id"
            ),
            {"status": status, "id": settlement_id},
        )
        await db.commit()
        return r.rowcount > 0

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
    ) -> Dict[str, Any]:
        """List internal staff members only (excludes CUSTOMER, PARTNER, DRIVER)."""
        STAFF_ROLES = (
            "ADMIN",
            "CCO",
            "VERIFICATION_OFFICER",
            "FINANCE_MANAGER",
            "SUPER_ADMIN",
        )
        where_clauses = [
            f"user_type IN {STAFF_ROLES!r}".replace("(", "(").replace(",)", ")")
        ]
        params: Dict[str, Any] = {"limit": page_size, "offset": (page - 1) * page_size}

        # Build IN clause properly
        role_placeholders = ",".join(f"'{r}'" for r in STAFF_ROLES)
        where_clauses = [f"user_type IN ({role_placeholders})"]

        if role:
            where_clauses.append("user_type = :role")
            params["role"] = role
        if status:
            where_clauses.append("status = :status")
            params["status"] = status
        if search:
            where_clauses.append(
                "(mobile_number ILIKE :search OR email ILIKE :search OR first_name ILIKE :search OR last_name ILIKE :search)"
            )
            params["search"] = f"%{search}%"

        where_sql = "WHERE " + " AND ".join(where_clauses)

        count_r = await db.execute(
            text(
                f"SELECT COUNT(*) FROM users u LEFT JOIN staff_profiles sp ON sp.user_id = u.id {where_sql.replace('WHERE ', 'WHERE u.').replace(' AND ', ' AND u.')}"
            ),
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
                    sp.id AS profile_id,
                    sp.designation, sp.department, sp.joining_date, sp.employee_id,
                    sp.city, sp.state,
                    sp.id_card_status, sp.id_card_generated_at
                FROM users u
                LEFT JOIN staff_profiles sp ON sp.user_id = u.id
                {where_sql.replace('WHERE', 'WHERE u.')}
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
        db: AsyncSession, user_id: UUID
    ) -> Optional[Dict[str, Any]]:
        """Fetch staff user with extended profile joined from staff_profiles.
        Doc Ref: DB 0014_staff_profiles
        """
        r = await db.execute(
            text(
                """
                SELECT
                    u.id, u.user_code, u.first_name, u.last_name, u.mobile_number,
                    u.email, u.user_type, u.status, u.is_active, u.profile_image_url,
                    u.created_at, u.last_login_at,
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
                LEFT JOIN staff_profiles sp ON sp.user_id = u.id
                WHERE u.id = :user_id
                  AND u.user_type IN ('ADMIN','CCO','VERIFICATION_OFFICER','FINANCE_MANAGER','SUPER_ADMIN')
            """
            ),
            {"user_id": str(user_id)},
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
    ) -> Dict[str, Any]:
        """Create an internal staff member account.
        Doc Ref: BRD Part 2 §12 — Password Login for Admin/CCO/Officer
        """
        from app.core.security import hash_password
        import uuid as _uuid

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
                    password_hash, user_type, status, is_active,
                    is_mobile_verified, is_email_verified,
                    profile_image_url, created_by, created_at, updated_at
                ) VALUES (
                    :id, :user_code, :first_name, :last_name, :mobile_number, :email,
                    :password_hash, :user_type, 'ACTIVE', TRUE,
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
    async def update_staff_user(
        db: AsyncSession,
        user_id: UUID,
        payload: "StaffUserUpdate",
    ) -> Optional[Dict[str, Any]]:
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
            updates.append("user_type = :user_type")
            params["user_type"] = payload.user_type
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
