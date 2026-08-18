# ============================================================
# WAY TERO — FASTAPI DEPENDENCIES
# File: app/core/dependencies.py
# Doc Ref: Backend Architecture Part 2, Section 19
# FastAPI dependency injection: get_db, get_current_user,
# role-specific guards.
# ============================================================

from typing import Optional
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import decode_access_token

# Re-export get_db for convenience
__all__ = [
    "get_db",
    "get_current_user",
    "require_admin",
    "require_partner",
    "require_driver",
    "require_permission",
]

security_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security_scheme),
    db: AsyncSession = Depends(get_db),
):
    """
    Decode JWT and return current authenticated user dict.
    Used as: current_user = Depends(get_current_user)

    Returns the JWT payload dict containing:
      - sub: user_id (UUID)
      - role: user role string
      - user_type: CUSTOMER | PARTNER | DRIVER | ADMIN | etc.

    The AuthenticationGuardMiddleware already decoded the token and cached it
    on request.state.user_payload — reuse it to avoid a second decode. When
    the middleware didn't run (unit tests call this directly), fall back to
    decoding the provided credentials.
    """
    payload = getattr(request.state, "user_payload", None)
    if payload is None:
        if not credentials:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication credentials not provided",
                headers={"WWW-Authenticate": "Bearer"},
            )

        payload = decode_access_token(credentials.credentials)
        if not payload:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired access token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    from app.modules.auth.repositories import UserRepository

    user_repo = UserRepository(db)
    user = await user_repo.get_by_id(UUID(payload["sub"]))
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is inactive",
        )

    payload["status"] = user.status.value
    return payload


def require_roles(*allowed_roles: str):
    """
    Dependency factory — restricts endpoint to specific roles.
    Doc Ref: Auth Flow — RBAC.

    Usage:
        @router.get("/admin-only")
        async def admin_endpoint(user = Depends(require_roles("ADMIN", "SUPER_ADMIN"))):
            ...
    """

    async def role_checker(current_user: dict = Depends(get_current_user)):
        user_roles = current_user.get("roles") or [current_user.get("role", "")]
        if not any(role in allowed_roles for role in user_roles):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied. Required roles: {', '.join(allowed_roles)}",
            )
        return current_user

    return role_checker


def require_permission(*required_permissions: str):
    """Dependency factory for permission-based access checks."""

    async def permission_checker(current_user: dict = Depends(get_current_user)):
        granted_permissions = set(current_user.get("permissions", []))
        if not all(
            permission in granted_permissions for permission in required_permissions
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied. Required permissions: {', '.join(required_permissions)}",
            )
        return current_user

    return permission_checker


# --- Shorthand role dependencies (Doc Ref: Auth — User Types)
def require_admin():
    return require_roles("ADMIN", "SUPER_ADMIN")


def require_super_admin():
    return require_roles("SUPER_ADMIN")


def require_partner():
    return require_roles("PARTNER", "ADMIN", "SUPER_ADMIN")


def require_driver():
    return require_roles("DRIVER")


def require_cco():
    return require_roles("CCO", "ADMIN", "SUPER_ADMIN")


def require_finance():
    return require_roles("FINANCE_MANAGER", "ADMIN", "SUPER_ADMIN")


def require_verification_officer():
    return require_roles("VERIFICATION_OFFICER", "ADMIN", "SUPER_ADMIN")


def require_customer():
    return require_roles("CUSTOMER", "ADMIN", "SUPER_ADMIN")
