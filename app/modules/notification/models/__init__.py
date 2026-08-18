# ============================================================
# WAY TERO — NOTIFICATION MODELS
# File: app/modules/notification/models/__init__.py
# Doc Ref:
#   BRD Part 3 §42 — Partner acceptance real-time channel
#   BRD Part 7 §155 — Notification engine
#
# Tables (migration 0040):
#   fcm_tokens          — registered FCM tokens per (user_id, device)
#   notification_outbox — durable record of every dispatched notification
# ============================================================

from datetime import datetime, timezone
from sqlalchemy import (
    Column,
    String,
    Boolean,
    DateTime,
    Text,
    BigInteger,
    ForeignKey,
    Index,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from app.core.database import Base


class FCMToken(Base):
    """
    A device or browser that wants push notifications for a user.

    `platform` is informational — the value sent by the client when it
    registered (WEB | ANDROID | IOS). The FCM client library handles
    routing internally, so the server doesn't branch on it.
    """

    __tablename__ = "fcm_tokens"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    fcm_token = Column(Text, nullable=False)
    platform = Column(String(20), nullable=False, default="WEB")
    user_agent = Column(Text)
    is_active = Column(Boolean, nullable=False, default=True)
    last_seen_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint("user_id", "fcm_token", name="uq_fcm_user_token"),
        Index("idx_fcm_user_active", "user_id", "is_active"),
    )


class NotificationOutbox(Base):
    """
    Append-only log of notifications dispatched to a user.

    delivered_via is a CSV of channels that succeeded
    ('ws', 'fcm', 'ws,fcm'). A row exists even when every channel failed —
    it's the audit record.

    read_at flips when the user opens the notification in the partner/admin
    portal — drives the badge count in the topbar.
    """

    __tablename__ = "notification_outbox"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    event_type = Column(String(80), nullable=False)
    title = Column(String(255), nullable=False)
    body = Column(Text)
    data = Column(JSONB)
    booking_id = Column(BigInteger)
    delivered_via = Column(String(20), nullable=False, default="ws")
    read_at = Column(DateTime(timezone=True))
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("idx_outbox_user_created", "user_id", "created_at"),
        Index(
            "idx_outbox_user_unread",
            "user_id",
            postgresql_where=Column("read_at").is_(None),
        ),
    )
