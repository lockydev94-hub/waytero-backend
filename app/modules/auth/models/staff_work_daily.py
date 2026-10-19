# ============================================================
# WAY TERO — STAFF WORK DAILY MODEL
# File: app/modules/auth/models/staff_work_daily.py
# Doc Ref: Admin Portal §39 Security — session tracking / leave details
# Migration: 0084
# ============================================================

import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class StaffWorkDaily(Base):
    """One row per staff member per platform-timezone day.

    Not derived from `sessions` or `audit_logs`: sessions have one row per
    device and never write logout_at, and audit login rows carry a BIGINT
    user_id that the UUID-keyed auth layer inserts as NULL — neither can
    answer "how long was this person in the dashboard on Tuesday".
    """

    __tablename__ = "staff_work_daily"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Bucketed in PLATFORM_TIMEZONE, never the browser's zone, so the daily
    # forced-logout hour and the day boundary land together.
    work_date = Column(Date, nullable=False)

    first_login_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    last_logout_at = Column(DateTime(timezone=True))
    last_seen_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    # Active seconds only — idle gaps are not work time. The 86400 ceiling
    # makes a broken or dishonest client unable to write a day longer than a day.
    active_seconds = Column(
        Integer, nullable=False, default=0, server_default="0"
    )
    session_count = Column(
        Integer, nullable=False, default=1, server_default="1"
    )
    heartbeat_count = Column(
        Integer, nullable=False, default=0, server_default="0"
    )

    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint("user_id", "work_date", name="uq_staff_work_daily_user_date"),
        CheckConstraint(
            "active_seconds >= 0 AND active_seconds <= 86400",
            name="ck_swd_active_seconds",
        ),
        Index("idx_staff_work_daily_date", "work_date"),
        Index("idx_staff_work_daily_user", "user_id", "work_date"),
    )

    @property
    def active_minutes(self) -> int:
        return round(self.active_seconds / 60)

    def __repr__(self):
        return (
            f"<StaffWorkDaily user_id={self.user_id} date={self.work_date} "
            f"active={self.active_seconds}s>"
        )
