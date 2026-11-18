# ============================================================
# WAYTERO — PARTNER ACCESS DEPENDENCIES
# File: app/modules/partner/dependencies.py
# Doc Ref: BRD Part 2 §17-19 — Partner onboarding & activation
#
# Route-level gates for service modules. A pending partner can sign in and
# track their application, but cannot touch operational data; an active partner
# can only touch the services they actually selected.
# ============================================================

from fastapi import Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.exceptions import PermissionDeniedException


def _service_label(service_type: str) -> str:
    return {
        "CAB": "cab",
        "HOTEL": "hotel",
        "TOUR": "tour package",
    }.get(service_type, service_type.lower())


def require_partner_service(service_type: str | None = None):
    """Require an ACTIVE partner, optionally with one active service.

    Non-PARTNER callers pass through: admin and driver routes share some of
    these routers and are protected by their own dependencies.
    """

    async def dependency(
        current_user: dict = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ) -> None:
        if str(current_user.get("user_type", "")).upper() != "PARTNER":
            return

        row = (
            (
                await db.execute(
                    text(
                        """
                        SELECT p.status,
                               EXISTS (
                                   SELECT 1 FROM partner_services ps
                                   WHERE ps.partner_id = p.id
                                     AND ps.service_type = :service_type
                                     AND ps.is_active = TRUE
                               ) AS service_active
                        FROM partners p
                        WHERE p.user_id = :user_id AND p.deleted_at IS NULL
                        """
                    ),
                    {
                        "user_id": current_user["sub"],
                        "service_type": service_type or "",
                    },
                )
            )
            .mappings()
            .one_or_none()
        )

        if row is None:
            raise PermissionDeniedException(
                message="Partner profile not found.",
                code="PARTNER_NOT_FOUND",
            )
        if str(row["status"]).upper() != "ACTIVE":
            raise PermissionDeniedException(
                message=(
                    "Your partner account is not active yet. "
                    "You can use the app once WayTero approves and activates it."
                ),
                code="PARTNER_NOT_ACTIVE",
            )
        if service_type and not row["service_active"]:
            raise PermissionDeniedException(
                message=(
                    f"Your partner account is not enabled for "
                    f"{_service_label(service_type)} services."
                ),
                code="SERVICE_NOT_ENABLED",
            )

    return dependency
