# ============================================================
# WAYTERO — DRIVER IDENTITY LINK
# File: app/modules/driver/identity.py
# Doc Ref: 06_Backend_Architecture/11_DRIVER_APP_BACKEND.md §driver users
#
# A Driver is a roster row. The app signs in as a User. This helper keeps those
# two facts together whenever a partner or admin creates a driver after
# migration 0103.
# ============================================================

from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.auth.models.user import User
from app.modules.auth.repositories import RoleRepository, UserRepository
from app.modules.driver.models import Driver
from app.shared.enums.user_types import UserStatus, UserType


async def ensure_driver_user(db: AsyncSession, driver: Driver) -> Optional[User]:
    """Create or link the DRIVER user for a roster driver.

    Returns None when the mobile already belongs to a different user type; we
    refuse to turn a customer or partner account into a driver login.
    """
    user_repo = UserRepository(db)
    role_repo = RoleRepository(db)

    if driver.user_id is not None:
        user = await user_repo.get_by_id(driver.user_id)
        if user is not None:
            return user

    user = await user_repo.get_by_mobile(driver.mobile)
    if user is not None:
        if user.user_type != UserType.DRIVER:
            return None
        driver.user_id = user.id
        if user.email is None and driver.email:
            email_owner = await user_repo.get_by_email(driver.email.lower())
            if email_owner is None:
                user.email = driver.email.lower()
        await db.flush()
        return user

    email = None
    if driver.email:
        email = driver.email.strip().lower()
        if await user_repo.get_by_email(email) is not None:
            email = None

    parts = (driver.full_name or "Driver").split()
    user = await user_repo.create(
        user_code=f"DRV-{driver.driver_code}"[:30],
        mobile_number=driver.mobile,
        email=email,
        user_type=UserType.DRIVER,
        status=UserStatus.ACTIVE,
        first_name=parts[0] if parts else "Driver",
        last_name=" ".join(parts[1:]) if len(parts) > 1 else None,
        is_mobile_verified=True,
        is_email_verified=False,
    )
    await role_repo.ensure_default_role_for_user(user)
    driver.user_id = user.id
    await db.flush()
    return user


async def sync_driver_user_email(db: AsyncSession, driver: Driver) -> None:
    """Keep the linked login email in step with a partner's edit."""
    if driver.user_id is None or not driver.email:
        return
    user = await db.get(User, driver.user_id)
    if user is None:
        return
    email = driver.email.strip().lower()
    owner = (
        await db.execute(
            select(User.id).where(
                func.lower(User.email) == email,
                User.id != user.id,
            )
        )
    ).first()
    if owner is None:
        user.email = email
        await db.flush()
