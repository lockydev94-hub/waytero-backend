# ============================================================
# WAY TERO — USER SESSION MODEL
# File: app/modules/auth/models/user_session.py
# Doc Ref: DB Architecture Part 3 — Sessions Table (Section 16)
# Phase: 1 — Authentication Module
# ============================================================

import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, Boolean, DateTime, ForeignKey, Index, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.core.database import Base


class UserSession(Base):
    """
    Tracks active user sessions.
    Doc Ref: DB Architecture Part 3, Section 16
    """

    __tablename__ = "sessions"

    id = Column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, nullable=False
    )
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Stored hashed refresh token (maps to session_token column in DB)
    session_token = Column(Text, nullable=False)

    # Device Info — Doc Ref: Section 26
    device_id = Column(String(255), nullable=True)
    device_name = Column(String(255), nullable=True)
    ip_address = Column(String(100), nullable=True)
    user_agent = Column(Text, nullable=True)

    # State
    is_active = Column(Boolean, default=True, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    logout_at = Column(DateTime(timezone=True), nullable=True)

    # Timestamps
    login_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    user = relationship("User", back_populates="sessions")

    # Indexes — Doc Ref: Section 17
    __table_args__ = (
        Index("idx_sessions_user", "user_id"),
        Index("idx_sessions_active", "is_active"),
        Index("idx_sessions_expiry", "expires_at"),
    )

    def __repr__(self):
        return (
            f"<UserSession id={self.id} user_id={self.user_id} active={self.is_active}>"
        )

    @property
    def is_expired(self) -> bool:
        return datetime.now(timezone.utc) > self.expires_at

    # Alias for backward compat with service code
    @property
    def refresh_token_hash(self) -> str:
        return self.session_token

    @refresh_token_hash.setter
    def refresh_token_hash(self, value: str) -> None:
        self.session_token = value

    @property
    def created_at(self) -> datetime:
        return self.login_at

    @property
    def last_used_at(self) -> datetime:
        return self.login_at
