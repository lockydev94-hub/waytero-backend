# ============================================================
# WAY TERO — USER MODEL
# File: app/modules/auth/models/user.py
# Doc Ref: Auth Flow — Supported User Types (Section 2)
# Doc Ref: DB Architecture Part 3 — users table (Section 5)
# Phase: 1 — Authentication Module
# ============================================================

import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    Column,
    String,
    Boolean,
    DateTime,
    Integer,
    Text,
    Enum as SAEnum,
    Index,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.core.database import Base
from app.shared.enums.user_types import UserType, UserStatus


class User(Base):
    """
    Central users table — every human actor in WayTero is a User.
    Customer, Partner, Driver, Admin all have a User record.
    Role-specific profiles live in their respective module tables.
    Doc Ref: DB Architecture Part 3, Section 5
    """

    __tablename__ = "users"

    # --- Primary Key
    id = Column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, nullable=False
    )

    # --- Identity (Doc Ref: Section 5)
    user_code = Column(String(30), unique=True, nullable=True)
    first_name = Column(String(100), nullable=False)
    last_name = Column(String(100), nullable=True)
    mobile_number = Column(String(20), unique=True, nullable=False, index=True)
    email = Column(String(255), unique=True, nullable=True, index=True)

    # --- Password (nullable — Customers and Drivers use OTP only)
    password_hash = Column(Text, nullable=True)

    # --- Profile
    profile_image_url = Column(Text, nullable=True)

    # --- Role & Status
    user_type = Column(
        SAEnum(UserType, name="user_type_enum", create_type=False), nullable=False
    )
    status = Column(
        SAEnum(UserStatus, name="user_status_enum", create_type=False),
        nullable=False,
        default=UserStatus.ACTIVE,
        server_default="ACTIVE",
    )

    # --- Security Flags
    is_mobile_verified = Column(Boolean, default=False, nullable=False)
    is_email_verified = Column(Boolean, default=False, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    failed_login_attempts = Column(Integer, default=0, nullable=False)
    locked_until = Column(DateTime(timezone=True), nullable=True)

    # --- Force password change on next login (partner default password flow)
    # Doc Ref: Partner Portal — first-login must-change-password requirement
    force_password_change = Column(
        Boolean, default=False, nullable=False, server_default="false"
    )

    # --- Audit
    last_login_at = Column(DateTime(timezone=True), nullable=True)
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    created_by = Column(UUID(as_uuid=True), nullable=True)
    updated_by = Column(UUID(as_uuid=True), nullable=True)

    # --- Timestamps
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # --- Relationships
    sessions = relationship(
        "UserSession", back_populates="user", cascade="all, delete-orphan"
    )
    user_roles = relationship(
        "UserRole", back_populates="user", cascade="all, delete-orphan"
    )

    # --- Indexes (Doc Ref: Section 6)
    __table_args__ = (
        Index("idx_users_status", "status"),
        Index("idx_users_created_at", "created_at"),
        Index("idx_users_user_type", "user_type"),
    )

    def __repr__(self):
        return f"<User id={self.id} mobile={self.mobile_number} type={self.user_type}>"

    @property
    def is_locked(self) -> bool:
        if self.locked_until is None:
            return False
        return datetime.now(timezone.utc) < self.locked_until

    @property
    def full_name(self) -> str:
        """Convenience property — combines first + last name."""
        if self.last_name:
            return f"{self.first_name} {self.last_name}"
        return self.first_name
