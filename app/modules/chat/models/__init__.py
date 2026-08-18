# ============================================================
# WAY TERO — LIVE CHAT MODELS
# File: app/modules/chat/models/__init__.py
# Doc Ref: BRD Part 7 §155 (realtime channel), Website Chat §1
#
# Tables (migration 0054):
#   chat_conversations — one row per support thread, keyed by a
#     logged-in customer (customer_user_id) or a guest identity
#     (guest_key + name/mobile). Status mirrors smart routing:
#     WAITING (started while support offline) / OPEN / CLOSED.
#   chat_messages — the messages. sender_type: CUSTOMER | ADMIN | SYSTEM.
# ============================================================

import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    Column,
    String,
    Text,
    Boolean,
    Integer,
    DateTime,
    BigInteger,
    ForeignKey,
    Index,
)
from sqlalchemy.dialects.postgresql import UUID
from app.core.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ChatConversation(Base):
    __tablename__ = "chat_conversations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    customer_user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    guest_key = Column(String(64), nullable=True)
    guest_name = Column(String(255), nullable=True)
    guest_email = Column(String(255), nullable=True)
    guest_mobile = Column(String(15), nullable=True)
    subject = Column(String(50), nullable=True)
    status = Column(
        String(20), nullable=False, default="WAITING"
    )  # WAITING | OPEN | CLOSED
    assigned_admin_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    last_message_at = Column(DateTime(timezone=True), nullable=True)
    last_message_preview = Column(String(300), nullable=True)
    unread_customer_count = Column(Integer, nullable=False, default=0)
    unread_admin_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        Index("idx_chat_conv_status", "status", "updated_at"),
        Index("idx_chat_conv_customer", "customer_user_id"),
        Index("idx_chat_conv_guest", "guest_key"),
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    conversation_id = Column(
        UUID(as_uuid=True),
        ForeignKey("chat_conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    sender_type = Column(String(20), nullable=False)  # CUSTOMER | ADMIN | SYSTEM
    sender_user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    body = Column(Text, nullable=False)
    is_read = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (Index("idx_chat_msg_conv", "conversation_id", "created_at"),)
