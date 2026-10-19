# ============================================================
# WAY TERO - AUTH REPOSITORIES
# File: app/modules/auth/repositories/__init__.py
# Phase: 1 - Authentication Module
# ============================================================

from datetime import date, datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import and_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.auth.constants import DEFAULT_ROLE_CODE_BY_USER_TYPE
from app.modules.auth.models.permission import Permission
from app.modules.auth.models.role import Role
from app.modules.auth.models.role_permission import RolePermission
from app.modules.auth.models.user import User
from app.modules.auth.models.user_role import UserRole
from app.modules.auth.models.user_session import UserSession
from app.shared.enums.user_types import UserType


class UserRepository:
    """Database operations for User model."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_by_id(self, user_id: UUID) -> Optional[User]:
        result = await self.db.execute(select(User).where(User.id == user_id))
        return result.scalar_one_or_none()

    async def get_by_mobile(self, mobile: str) -> Optional[User]:
        result = await self.db.execute(select(User).where(User.mobile_number == mobile))
        return result.scalar_one_or_none()

    async def get_by_mobile_and_type(
        self, mobile: str, user_type: UserType
    ) -> Optional[User]:
        result = await self.db.execute(
            select(User).where(
                and_(User.mobile_number == mobile, User.user_type == user_type)
            )
        )
        return result.scalar_one_or_none()

    async def get_by_email(self, email: str) -> Optional[User]:
        result = await self.db.execute(select(User).where(User.email == email))
        return result.scalar_one_or_none()

    async def get_by_email_and_type(
        self, email: str, user_type: UserType
    ) -> Optional[User]:
        """Lookup user by email restricted to a specific user_type — prevents cross-type collisions."""
        result = await self.db.execute(
            select(User).where(and_(User.email == email, User.user_type == user_type))
        )
        return result.scalar_one_or_none()

    async def create(self, **kwargs) -> User:
        user = User(**kwargs)
        self.db.add(user)
        await self.db.flush()
        await self.db.refresh(user)
        return user

    async def update_last_login(self, user_id: UUID) -> None:
        await self.db.execute(
            update(User)
            .where(User.id == user_id)
            .values(
                last_login_at=datetime.now(timezone.utc),
                failed_login_attempts=0,
                locked_until=None,
            )
        )

    async def increment_failed_attempts(self, user_id: UUID) -> int:
        user = await self.get_by_id(user_id)
        if user:
            user.failed_login_attempts += 1
            await self.db.flush()
            return user.failed_login_attempts
        return 0

    async def lock_account(self, user_id: UUID, locked_until: datetime) -> None:
        await self.db.execute(
            update(User).where(User.id == user_id).values(locked_until=locked_until)
        )

    async def mark_mobile_verified(self, user_id: UUID) -> None:
        await self.db.execute(
            update(User).where(User.id == user_id).values(is_mobile_verified=True)
        )

    async def mark_email_verified(self, user_id: UUID) -> None:
        """Doc Ref: Firebase/Google sign-in — flip the email-verified bit
        once the upstream IdP has confirmed the email."""
        await self.db.execute(
            update(User).where(User.id == user_id).values(is_email_verified=True)
        )

    async def update_profile(self, user_id: UUID, **fields) -> None:
        """Update arbitrary non-null user fields (first_name, last_name, picture, …).
        Skips None values so we don't clobber existing data with missing IdP claims."""
        cleaned = {k: v for k, v in fields.items() if v is not None}
        if not cleaned:
            return
        cleaned["updated_at"] = datetime.now(timezone.utc)
        await self.db.execute(update(User).where(User.id == user_id).values(**cleaned))

    async def update_password(self, user_id: UUID, hashed_password: str) -> None:
        await self.db.execute(
            update(User)
            .where(User.id == user_id)
            .values(
                password_hash=hashed_password,
                updated_at=datetime.now(timezone.utc),
            )
        )

    async def clear_force_password_change(self, user_id: UUID) -> None:
        """Clear the force_password_change flag after partner sets new password."""
        from sqlalchemy import update as sa_update

        await self.db.execute(
            sa_update(User)
            .where(User.id == user_id)
            .values(force_password_change=False)
        )
        await self.db.flush()

    async def set_force_password_change(self, user_id: UUID) -> None:
        """Set force_password_change flag (used when admin resets partner password)."""
        from sqlalchemy import update as sa_update

        await self.db.execute(
            sa_update(User).where(User.id == user_id).values(force_password_change=True)
        )
        await self.db.flush()

    async def exists_by_mobile(self, mobile: str) -> bool:
        result = await self.db.execute(
            select(User.id).where(User.mobile_number == mobile)
        )
        return result.scalar_one_or_none() is not None


class SessionRepository:
    """Database operations for UserSession model."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(self, **kwargs) -> UserSession:
        # Map refresh_token_hash → session_token for DB compatibility
        if "refresh_token_hash" in kwargs:
            kwargs["session_token"] = kwargs.pop("refresh_token_hash")
        session = UserSession(**kwargs)
        self.db.add(session)
        await self.db.flush()
        await self.db.refresh(session)
        return session

    async def get_by_id(self, session_id: UUID) -> Optional[UserSession]:
        result = await self.db.execute(
            select(UserSession).where(UserSession.id == session_id)
        )
        return result.scalar_one_or_none()

    async def get_by_refresh_token_hash(self, token_hash: str) -> Optional[UserSession]:
        result = await self.db.execute(
            select(UserSession).where(
                and_(
                    UserSession.session_token == token_hash,
                    UserSession.is_active.is_(True),
                )
            )
        )
        return result.scalar_one_or_none()

    async def get_active_sessions(self, user_id: UUID) -> list[UserSession]:
        result = await self.db.execute(
            select(UserSession)
            .where(
                and_(UserSession.user_id == user_id, UserSession.is_active.is_(True))
            )
            .order_by(UserSession.login_at.desc())
        )
        return list(result.scalars().all())

    async def count_active_sessions(self, user_id: UUID) -> int:
        return len(await self.get_active_sessions(user_id))

    async def deactivate_session(
        self, session_id: UUID, logout_at: Optional[datetime] = None
    ) -> None:
        """Close one session row. `logout_at` is written only when the admin
        actually signed out — an eviction by the session cap ends the row the
        same way, but recording it as a logout would misreport working hours."""
        values: dict = {"is_active": False}
        if logout_at is not None:
            values["logout_at"] = logout_at
        await self.db.execute(
            update(UserSession).where(UserSession.id == session_id).values(**values)
        )

    async def deactivate_all_user_sessions(
        self, user_id: UUID, logout_at: Optional[datetime] = None
    ) -> None:
        values: dict = {"is_active": False}
        if logout_at is not None:
            values["logout_at"] = logout_at
        await self.db.execute(
            update(UserSession)
            .where(
                and_(UserSession.user_id == user_id, UserSession.is_active.is_(True))
            )
            .values(**values)
        )

    async def deactivate_oldest_session(self, user_id: UUID) -> None:
        sessions = await self.get_active_sessions(user_id)
        if sessions:
            oldest = min(sessions, key=lambda session: session.login_at)
            await self.deactivate_session(oldest.id)

    async def update_last_used(self, session_id: UUID) -> None:
        # sessions table tracks login_at; last_used is handled by is_active state
        pass


class StaffWorkDailyRepository:
    """One accumulating row per staff member per platform-timezone day.

    Attendance cannot be derived from `sessions` (one row per device, and
    `logout_at` was never written until this round) or from `audit_logs`
    (BIGINT user_id against UUID users, so login rows land with NULL user_id).
    This table is the record the leave/working-hours report reads.

    `work_date` is computed by the caller in the platform zone rather than with
    `AT TIME ZONE` in the INSERT: a typo in the PLATFORM_TIMEZONE row would
    otherwise fail every admin login instead of merely mis-bucketing a day.
    """

    def __init__(self, db: AsyncSession):
        self.db = db

    async def open_day(self, *, user_id: UUID, work_date: date) -> None:
        """Count a sign-in. A second login on the same day is a new session on
        the same row, not a new day — and it reopens the row if the admin had
        signed out and come back."""
        await self.db.execute(
            text(
                """
                INSERT INTO staff_work_daily
                    (user_id, work_date, first_login_at, last_seen_at,
                     session_count, heartbeat_count, created_at, updated_at)
                VALUES
                    (:user_id, :work_date, NOW(), NOW(), 1, 0, NOW(), NOW())
                ON CONFLICT (user_id, work_date) DO UPDATE
                    SET session_count  = staff_work_daily.session_count + 1,
                        last_logout_at = NULL,
                        last_seen_at   = GREATEST(staff_work_daily.last_seen_at, NOW()),
                        updated_at     = NOW()
                """
            ),
            {"user_id": user_id, "work_date": work_date},
        )
        await self.db.flush()

    async def record_activity(
        self, *, user_id: UUID, work_date: date, active_seconds: int
    ) -> None:
        """Add actively-worked seconds to the day row.

        The 86400 ceiling is `ck_swd_active_seconds`; clamping here means a
        dishonest or buggy client cannot make a day longer than a day.
        """
        await self.db.execute(
            text(
                """
                INSERT INTO staff_work_daily
                    (user_id, work_date, first_login_at, last_seen_at,
                     active_seconds, session_count, heartbeat_count, created_at, updated_at)
                VALUES
                    (:user_id, :work_date, NOW(), NOW(), :active_seconds, 1, 1, NOW(), NOW())
                ON CONFLICT (user_id, work_date) DO UPDATE
                    SET active_seconds  = LEAST(86400, staff_work_daily.active_seconds + :active_seconds),
                        last_seen_at    = GREATEST(staff_work_daily.last_seen_at, NOW()),
                        heartbeat_count = staff_work_daily.heartbeat_count + 1,
                        updated_at      = NOW()
                """
            ),
            {"user_id": user_id, "work_date": work_date, "active_seconds": active_seconds},
        )
        await self.db.flush()

    async def close_day(self, *, user_id: UUID, work_date: date) -> None:
        await self.db.execute(
            text(
                """
                UPDATE staff_work_daily
                   SET last_logout_at = NOW(),
                       last_seen_at   = GREATEST(last_seen_at, NOW()),
                       updated_at     = NOW()
                 WHERE user_id = :user_id AND work_date = :work_date
                """
            ),
            {"user_id": user_id, "work_date": work_date},
        )
        await self.db.flush()

    # ── Reading the record back ──────────────────────────────────────────────
    # One SELECT shared by the super-admin report and an admin's own profile,
    # so the "when did this day actually end" rule cannot drift between them.

    _DAY_COLUMNS = """
        swd.user_id,
        u.user_code,
        TRIM(BOTH ' ' FROM COALESCE(u.first_name, '') || ' ' || COALESCE(u.last_name, '')) AS full_name,
        u.user_type,
        swd.work_date,
        swd.first_login_at,
        COALESCE(
            swd.last_logout_at,
            LEAST(
                swd.last_seen_at + interval '60 seconds',
                ((swd.work_date + 1)::timestamp AT TIME ZONE :tz)
            )
        ) AS last_logout_at,
        swd.last_seen_at,
        swd.session_count,
        swd.heartbeat_count,
        swd.active_seconds,
        -- Rounded here so the report and the profile tab cannot disagree.
        ROUND(swd.active_seconds / 60.0)::int AS active_minutes
    """

    @staticmethod
    def _day_filters(*, user_id: Optional[UUID], date_from: date, date_to: date) -> tuple[str, dict]:
        clauses = ["swd.work_date >= :date_from", "swd.work_date <= :date_to"]
        params: dict = {"date_from": date_from, "date_to": date_to}
        if user_id is not None:
            clauses.append("swd.user_id = :user_id")
            params["user_id"] = user_id
        return "WHERE " + " AND ".join(clauses), params

    async def list_days(
        self,
        *,
        tz_name: str,
        date_from: date,
        date_to: date,
        user_id: Optional[UUID] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[dict]:
        where_sql, params = self._day_filters(user_id=user_id, date_from=date_from, date_to=date_to)
        params = {**params, "tz": tz_name}
        limit_sql = ""
        if limit is not None:
            limit_sql = "LIMIT :limit OFFSET :offset"
            params["limit"] = limit
            params["offset"] = offset
        result = await self.db.execute(
            text(
                f"""
                SELECT {self._DAY_COLUMNS}
                  FROM staff_work_daily swd
                  JOIN users u ON u.id = swd.user_id
                 {where_sql}
                 ORDER BY swd.work_date DESC, full_name, swd.user_id
                 {limit_sql}
                """
            ),
            params,
        )
        return [dict(row) for row in result.mappings().all()]

    async def summarize_days(
        self,
        *,
        date_from: date,
        date_to: date,
        user_id: Optional[UUID] = None,
    ) -> dict:
        where_sql, params = self._day_filters(user_id=user_id, date_from=date_from, date_to=date_to)
        result = await self.db.execute(
            text(
                f"""
                SELECT COUNT(*)::int AS days,
                       COUNT(DISTINCT swd.user_id)::int AS staff_count,
                       COALESCE(SUM(swd.active_seconds), 0)::int AS active_seconds
                  FROM staff_work_daily swd
                 {where_sql}
                """
            ),
            params,
        )
        row = result.mappings().one()
        return {
            "days": int(row["days"]),
            "staff_count": int(row["staff_count"]),
            "active_seconds": int(row["active_seconds"]),
        }

    async def count_days(
        self,
        *,
        date_from: date,
        date_to: date,
        user_id: Optional[UUID] = None,
    ) -> int:
        where_sql, params = self._day_filters(user_id=user_id, date_from=date_from, date_to=date_to)
        result = await self.db.execute(
            text(f"SELECT COUNT(*)::int FROM staff_work_daily swd {where_sql}"), params
        )
        return int(result.scalar() or 0)


class RoleRepository:
    """Database operations for roles, permissions, and user assignments."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_role_by_code(self, role_code: str) -> Optional[Role]:
        result = await self.db.execute(select(Role).where(Role.role_code == role_code))
        return result.scalar_one_or_none()

    async def list_roles_for_user(self, user_id: UUID) -> list[str]:
        result = await self.db.execute(
            select(Role.role_code)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.user_id == user_id)
            .order_by(Role.role_code.asc())
        )
        return list(result.scalars().all())

    async def list_permissions_for_user(self, user_id: UUID) -> list[str]:
        result = await self.db.execute(
            select(Permission.permission_code)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .join(Role, Role.id == RolePermission.role_id)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.user_id == user_id)
            .order_by(Permission.permission_code.asc())
        )
        return list(dict.fromkeys(result.scalars().all()))

    async def assign_role_to_user(
        self,
        user_id: UUID,
        role_code: str,
        assigned_by: Optional[UUID] = None,
    ) -> UserRole:
        role = await self.get_role_by_code(role_code)
        if role is None:
            raise ValueError(f"Unknown role code: {role_code}")

        result = await self.db.execute(
            select(UserRole).where(
                and_(UserRole.user_id == user_id, UserRole.role_id == role.id)
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            return existing

        assignment = UserRole(user_id=user_id, role_id=role.id, assigned_by=assigned_by)
        self.db.add(assignment)
        await self.db.flush()
        await self.db.refresh(assignment)
        return assignment

    async def ensure_default_role_for_user(self, user: User) -> None:
        roles = await self.list_roles_for_user(user.id)
        if roles:
            return

        default_role_code = DEFAULT_ROLE_CODE_BY_USER_TYPE.get(user.user_type.value)
        if default_role_code:
            await self.assign_role_to_user(user.id, default_role_code)
