# ============================================================
# WAY TERO - AUTH REPOSITORIES
# File: app/modules/auth/repositories/__init__.py
# Phase: 1 - Authentication Module
# ============================================================

from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import and_, select, update
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

    async def deactivate_session(self, session_id: UUID) -> None:
        await self.db.execute(
            update(UserSession)
            .where(UserSession.id == session_id)
            .values(is_active=False)
        )

    async def deactivate_all_user_sessions(self, user_id: UUID) -> None:
        await self.db.execute(
            update(UserSession)
            .where(UserSession.user_id == user_id)
            .values(is_active=False)
        )

    async def deactivate_oldest_session(self, user_id: UUID) -> None:
        sessions = await self.get_active_sessions(user_id)
        if sessions:
            oldest = min(sessions, key=lambda session: session.login_at)
            await self.deactivate_session(oldest.id)

    async def update_last_used(self, session_id: UUID) -> None:
        # sessions table tracks login_at; last_used is handled by is_active state
        pass


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
