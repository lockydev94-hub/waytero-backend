# ============================================================
# WAYTERO — AUDIT LOGGER (WRITE-SIDE HELPER)
# File: app/modules/admin/services/audit_logger.py
# Doc Ref:
#   Docs/04_API_Documentation/12_ADMIN_API.md §24 (Audit Logs)
#   Docs/05_Database/09_DATABASE_SCHEMA_PART_8_AUDIT_NOTIFICATION.md §76-104
#   BRD Part 8 §213 (Audit Compliance, 7-year retention)
#
# Use this helper from any service or endpoint that mutates platform state.
# It writes one row to audit_logs, returns the new id, and crucially NEVER
# raises — audit must never break the parent transaction. A failure to write
# the audit row is logged at WARNING and swallowed; the original action is
# still considered successful.
#
# Typical usage:
#
#     async def approve_partner(...):
#         partner.status = "APPROVED"
#         await db.commit()
#         await AuditLogger.log_partner_event(
#             db,
#             user_id=current_user["sub"],
#             action_type="PARTNER_APPROVED",
#             entity_id=partner.id,
#             old_values={"status": "PENDING"},
#             new_values={"status": "APPROVED"},
#             ip_address=request.client.host,
#             user_agent=request.headers.get("user-agent"),
#         )
# ============================================================

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("waytero.audit")


class AuditLogger:
    """
    Thin static facade over the raw SQL INSERT into audit_logs.

    The free `log_event` function is the workhorse. The convenience methods
    (log_partner_event, log_hotel_event, etc.) just pre-fill `module_name`
    so callers don't have to remember the constant.
    """

    # ── Module name constants ─────────────────────────────────────────────────
    # Mirror the AUDIT_MODULES list in the admin-portal constants file so the
    # filter dropdown and the writer side agree on the vocabulary.
    MODULE_AUTH = "AUTH"
    MODULE_PARTNER = "PARTNER"
    MODULE_DRIVER = "DRIVER"
    MODULE_VEHICLE = "VEHICLE"
    MODULE_HOTEL = "HOTEL"
    MODULE_TOUR = "TOUR"
    MODULE_BOOKING = "BOOKING"
    MODULE_PAYMENT = "PAYMENT"
    MODULE_WALLET = "WALLET"
    MODULE_SETTLEMENT = "SETTLEMENT"
    MODULE_COUPON = "COUPON"
    MODULE_NOTIFICATION = "NOTIFICATION"
    MODULE_ROLE = "ROLE"
    MODULE_CONFIG = "CONFIG"
    MODULE_STAFF = "STAFF"

    # ── Raw event writer ─────────────────────────────────────────────────────
    @staticmethod
    async def log_event(
        db: AsyncSession,
        *,
        module_name: str,
        action_type: str,
        user_id: Optional[int] = None,
        entity_name: Optional[str] = None,
        entity_id: Optional[int] = None,
        old_values: Optional[Dict[str, Any]] = None,
        new_values: Optional[Dict[str, Any]] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> Optional[int]:
        """
        Insert one audit row. Returns the new id, or None if the insert failed.

        Audit must NEVER raise — the calling code path has already committed
        its work, and we don't want a missing audit row to surface as a 500
        to the operator. Failures are logged and swallowed.
        """
        params: Dict[str, Any] = {
            "user_id": user_id,
            "module_name": module_name,
            "entity_name": entity_name,
            "entity_id": entity_id,
            "action_type": action_type,
            "old_values": _jsonb(old_values),
            "new_values": _jsonb(new_values),
            "ip_address": ip_address,
            "user_agent": user_agent,
            "request_id": request_id,
            "created_at": datetime.now(timezone.utc),
        }

        try:
            result = await db.execute(
                sa.text(
                    """
                    INSERT INTO audit_logs (
                        user_id, module_name, entity_name, entity_id,
                        action_type, old_values, new_values,
                        ip_address, user_agent, request_id, created_at
                    ) VALUES (
                        :user_id, :module_name, :entity_name, :entity_id,
                        :action_type, :old_values, :new_values,
                        :ip_address, :user_agent, :request_id, :created_at
                    )
                    RETURNING id
                    """
                ),
                params,
            )
            row = result.scalar_one_or_none()
            return int(row) if row is not None else None
        except Exception as exc:  # noqa: BLE001 — audit must never raise
            logger.warning(
                "audit_log_insert_failed",
                extra={
                    "module_name": module_name,
                    "action_type": action_type,
                    "user_id": user_id,
                    "entity_name": entity_name,
                    "entity_id": entity_id,
                    "error": str(exc),
                },
            )
            return None

    # ── Per-module sugar ─────────────────────────────────────────────────────
    @classmethod
    async def log_auth_event(
        cls,
        db: AsyncSession,
        *,
        action_type: str,
        user_id: Optional[int] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> Optional[int]:
        return await cls.log_event(
            db,
            user_id=user_id,
            module_name=cls.MODULE_AUTH,
            action_type=action_type,
            ip_address=ip_address,
            user_agent=user_agent,
        )

    @classmethod
    async def log_partner_event(
        cls,
        db: AsyncSession,
        *,
        action_type: str,
        user_id: Optional[int] = None,
        partner_id: Optional[int] = None,
        old_values: Optional[Dict[str, Any]] = None,
        new_values: Optional[Dict[str, Any]] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> Optional[int]:
        return await cls.log_event(
            db,
            user_id=user_id,
            module_name=cls.MODULE_PARTNER,
            action_type=action_type,
            entity_name="partner",
            entity_id=partner_id,
            old_values=old_values,
            new_values=new_values,
            ip_address=ip_address,
            user_agent=user_agent,
            request_id=request_id,
        )

    @classmethod
    async def log_hotel_event(
        cls,
        db: AsyncSession,
        *,
        action_type: str,
        user_id: Optional[int] = None,
        hotel_id: Optional[int] = None,
        old_values: Optional[Dict[str, Any]] = None,
        new_values: Optional[Dict[str, Any]] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> Optional[int]:
        return await cls.log_event(
            db,
            user_id=user_id,
            module_name=cls.MODULE_HOTEL,
            action_type=action_type,
            entity_name="hotel",
            entity_id=hotel_id,
            old_values=old_values,
            new_values=new_values,
            ip_address=ip_address,
            user_agent=user_agent,
            request_id=request_id,
        )

    @classmethod
    async def log_driver_event(
        cls,
        db: AsyncSession,
        *,
        action_type: str,
        user_id: Optional[int] = None,
        driver_id: Optional[int] = None,
        old_values: Optional[Dict[str, Any]] = None,
        new_values: Optional[Dict[str, Any]] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> Optional[int]:
        return await cls.log_event(
            db,
            user_id=user_id,
            module_name=cls.MODULE_DRIVER,
            action_type=action_type,
            entity_name="driver",
            entity_id=driver_id,
            old_values=old_values,
            new_values=new_values,
            ip_address=ip_address,
            user_agent=user_agent,
            request_id=request_id,
        )

    @classmethod
    async def log_vehicle_event(
        cls,
        db: AsyncSession,
        *,
        action_type: str,
        user_id: Optional[int] = None,
        vehicle_id: Optional[int] = None,
        old_values: Optional[Dict[str, Any]] = None,
        new_values: Optional[Dict[str, Any]] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> Optional[int]:
        return await cls.log_event(
            db,
            user_id=user_id,
            module_name=cls.MODULE_VEHICLE,
            action_type=action_type,
            entity_name="vehicle",
            entity_id=vehicle_id,
            old_values=old_values,
            new_values=new_values,
            ip_address=ip_address,
            user_agent=user_agent,
            request_id=request_id,
        )

    @classmethod
    async def log_staff_event(
        cls,
        db: AsyncSession,
        *,
        action_type: str,
        user_id: Optional[int] = None,
        staff_id: Optional[int] = None,
        old_values: Optional[Dict[str, Any]] = None,
        new_values: Optional[Dict[str, Any]] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> Optional[int]:
        return await cls.log_event(
            db,
            user_id=user_id,
            module_name=cls.MODULE_STAFF,
            action_type=action_type,
            entity_name="staff_user",
            entity_id=staff_id,
            old_values=old_values,
            new_values=new_values,
            ip_address=ip_address,
            user_agent=user_agent,
            request_id=request_id,
        )


def _jsonb(value: Optional[Dict[str, Any]]) -> Optional[str]:
    """
    Serialise a dict for a JSONB column as a JSON string.

    asyncpg requires a *string* for JSONB binds — passing a raw dict raises
    DataError ("'dict' object has no attribute 'encode'"), which is why
    every audit insert silently failed (log_event swallows errors) since
    migration 0037. Returning None for None keeps the column nullable.
    """
    if value is None:
        return None
    import json

    return json.dumps(value, default=str, ensure_ascii=False)
