# ============================================================
# WAY TERO - PERMISSION MODEL
# File: app/modules/auth/models/permission.py
# Phase: 1 - Authentication Module
# ============================================================

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.core.database import Base


class Permission(Base):
    """Granular permission assigned to roles."""

    __tablename__ = "permissions"

    id = Column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, nullable=False
    )
    permission_code = Column(String(100), unique=True, nullable=False, index=True)
    permission_name = Column(String(255), nullable=False)
    module_name = Column(String(100), nullable=True)
    description = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    role_permissions = relationship(
        "RolePermission",
        back_populates="permission",
        cascade="all, delete-orphan",
    )
