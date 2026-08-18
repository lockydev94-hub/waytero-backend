# ============================================================
# WAY TERO — LIVE CHAT SERVICE
# File: app/modules/chat/services/__init__.py
# Doc Ref: Website Chat §2/§3 — API + smart routing
#
# Orchestrates conversations end-to-end:
#   - Identity: logged-in customer (JWT sub) or guest (guest_key).
#   - Smart routing: OPEN + assigned agent when support is online
#     (Redis presence), otherwise WAITING (offline).
#   - Realtime: pushes chat.message WS events to the recipient and
#     fan-outs CHAT_NEW_MESSAGE notifications to online admins so the
#     portal can raise the reply modal.
# ============================================================

from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select, text as _text, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    BusinessException,
    PermissionDeniedException,
    ResourceNotFoundException,
    ValidationException,
)
from app.modules.auth.models.user import User
from app.modules.chat.models import ChatConversation, ChatMessage
from app.modules.chat.services.presence import (
    is_support_online,
    online_admin_ids,
    pick_agent,
)
from app.modules.notification.realtime import manager
from app.modules.notification.services import dispatch_many

logger = logging.getLogger("waytero.chat")

CHAT_MESSAGE_EVENT = "chat.message"
CHAT_NEW_MESSAGE_NOTIF = "CHAT_NEW_MESSAGE"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _msg_payload(message: ChatMessage, sender_name: str | None = None) -> dict:
    return {
        "id": message.id,
        "conversation_id": str(message.conversation_id),
        "sender_type": message.sender_type,
        "sender_user_id": (
            str(message.sender_user_id) if message.sender_user_id else None
        ),
        "sender_name": sender_name,
        "body": message.body,
        "is_read": message.is_read,
        "created_at": message.created_at.isoformat() if message.created_at else None,
    }


async def _user_display_name(
    db: AsyncSession, user_id, default: str = "Team Member"
) -> str:
    """First + last name of a user (admin, support agent, customer)."""
    row = (
        await db.execute(
            select(
                func.coalesce(
                    func.nullif(
                        func.trim(
                            func.coalesce(User.first_name, "")
                            + " "
                            + func.coalesce(User.last_name, "")
                        ),
                        "",
                    ),
                    default,
                )
            ).where(User.id == user_id)
        )
    ).scalar_one_or_none()
    return row or default


async def _resolve_sender_names(
    db: AsyncSession, messages: list[ChatMessage]
) -> dict[UUID, str]:
    """sender_user_id -> display name for every message that has one."""
    ids = {m.sender_user_id for m in messages if m.sender_user_id}
    if not ids:
        return {}
    rows = (
        (
            await db.execute(
                select(
                    User.id,
                    func.coalesce(
                        func.nullif(
                            func.trim(
                                func.coalesce(User.first_name, "")
                                + " "
                                + func.coalesce(User.last_name, "")
                            ),
                            "",
                        ),
                        "Team Member",
                    ).label("name"),
                ).where(User.id.in_(ids))
            )
        )
        .mappings()
        .all()
    )
    return {UUID(str(r["id"])): str(r["name"]) for r in rows}


def _serialize_messages(
    messages: list[ChatMessage],
    names: dict[UUID, str],
    guest_name: str | None,
) -> list[dict]:
    """Messages enriched with sender_name: the registered user name, or
    the guest's self-declared name for customer messages with no account."""
    out = []
    for m in messages:
        sender_name = names.get(m.sender_user_id) if m.sender_user_id else None
        if sender_name is None and m.sender_type == "CUSTOMER":
            sender_name = guest_name
        out.append(_msg_payload(m, sender_name))
    return out


async def _display_name(db: AsyncSession, conversation: ChatConversation) -> str:
    """Customer-facing name for the conversation (used in notifications)."""
    if conversation.guest_name:
        return conversation.guest_name
    if conversation.customer_user_id:
        return await _user_display_name(db, conversation.customer_user_id, "Customer")
    return "Customer"


async def resolve_identity(
    current_user: dict | None, body
) -> tuple[UUID | None, str | None]:
    """Return (customer_user_id, guest_key) from the request."""
    if current_user and current_user.get("sub"):
        return UUID(current_user["sub"]), None
    if body.guest and body.guest.guest_key:
        return None, body.guest.guest_key
    raise ValidationException("Log in to chat, or provide your name and mobile.")


async def _find_open_conversation(
    db: AsyncSession, customer_user_id: UUID | None, guest_key: str | None
) -> ChatConversation | None:
    if customer_user_id:
        q = select(ChatConversation).where(
            ChatConversation.customer_user_id == customer_user_id,
            ChatConversation.status != "CLOSED",
        )
    elif guest_key:
        q = select(ChatConversation).where(
            ChatConversation.guest_key == guest_key,
            ChatConversation.status != "CLOSED",
        )
    else:
        return None
    return (
        await db.execute(q.order_by(ChatConversation.updated_at.desc()).limit(1))
    ).scalar_one_or_none()


async def create_or_open_conversation(
    db: AsyncSession, *, current_user: dict | None, body
) -> dict:
    """Create a new support thread or return the customer's existing one.

    Smart routing: if support is online the thread opens (OPEN) and is
    assigned to an available agent; otherwise it sits WAITING and any
    message the customer sends is queued for when someone comes online.
    """
    customer_user_id, guest_key = await resolve_identity(current_user, body)

    existing = await _find_open_conversation(db, customer_user_id, guest_key)
    if existing:
        conversation = existing
        is_new = False
    else:
        online = await is_support_online()
        conversation = ChatConversation(
            customer_user_id=customer_user_id,
            guest_key=guest_key,
            guest_name=body.guest.name.strip() if body.guest else None,
            guest_email=(
                str(body.guest.email) if body.guest and body.guest.email else None
            ),
            guest_mobile=body.guest.mobile if body.guest else None,
            subject=body.subject.strip() if body.subject else None,
            status="OPEN" if online else "WAITING",
            assigned_admin_id=await pick_agent() if online else None,
        )
        db.add(conversation)
        await db.flush()
        is_new = True

    message_payload = None
    if body.message and body.message.strip():
        message_payload = await send_customer_message(
            db, conversation=conversation, body=body.message.strip()
        )

    support_online = await is_support_online()
    return {
        "conversation": _conversation_summary(conversation),
        "support_online": support_online,
        "is_new": is_new,
        "first_message": message_payload,
    }


def _conversation_summary(conversation: ChatConversation) -> dict:
    return {
        "id": str(conversation.id),
        "status": conversation.status,
        "subject": conversation.subject,
        "guest_name": conversation.guest_name,
        "assigned_admin_id": (
            str(conversation.assigned_admin_id)
            if conversation.assigned_admin_id
            else None
        ),
        "last_message_at": (
            conversation.last_message_at.isoformat()
            if conversation.last_message_at
            else None
        ),
        "last_message_preview": conversation.last_message_preview,
        "unread_customer_count": conversation.unread_customer_count,
        "unread_admin_count": conversation.unread_admin_count,
        "created_at": (
            conversation.created_at.isoformat() if conversation.created_at else None
        ),
    }


async def get_customer_conversation(
    db: AsyncSession,
    *,
    conversation_id: str,
    current_user: dict | None,
    guest_key: str | None,
) -> dict:
    """Fetch a conversation + its messages for the customer widget.

    Ownership is enforced: the requester must be the customer who owns it
    (or hold the matching guest_key). Fetching marks customer-side unread
    as read so the widget badge clears.
    """
    conversation = await _get_conversation_or_404(db, conversation_id)
    _assert_owner(conversation, current_user, guest_key)

    messages = list(
        (
            await db.execute(
                select(ChatMessage)
                .where(ChatMessage.conversation_id == conversation.id)
                .order_by(ChatMessage.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    names = await _resolve_sender_names(db, messages)

    if conversation.unread_customer_count:
        conversation.unread_customer_count = 0
        await db.flush()

    return {
        "conversation": _conversation_summary(conversation),
        "support_online": await is_support_online(),
        "messages": _serialize_messages(messages, names, conversation.guest_name),
    }


async def send_customer_message(
    db: AsyncSession, *, conversation: ChatConversation, body: str
) -> dict:
    """Persist a customer message, bump the admin unread counter and
    notify every online admin (modal + dropdown) in realtime."""
    if conversation.status == "CLOSED":
        raise BusinessException(
            "This conversation is closed. Start a new chat to continue."
        )

    message = ChatMessage(
        conversation_id=conversation.id,
        sender_type="CUSTOMER",
        sender_user_id=conversation.customer_user_id,
        body=body,
    )
    db.add(message)
    conversation.last_message_at = _utcnow()
    conversation.last_message_preview = body[:297]
    conversation.unread_admin_count = (conversation.unread_admin_count or 0) + 1
    await db.flush()

    name = await _display_name(db, conversation)
    payload = {
        **_msg_payload(message, sender_name=name),
        "customer_name": name,
        "subject": conversation.subject,
    }

    # Realtime for the assigned agent's open chat page.
    if conversation.assigned_admin_id:
        await manager.send_to_user(
            conversation.assigned_admin_id,
            {"event": CHAT_MESSAGE_EVENT, "data": payload},
        )

    # Fan-out notification to every online admin (drives the reply modal).
    online = await online_admin_ids()
    if online:
        await dispatch_many(
            db,
            user_ids=online,
            event_type=CHAT_NEW_MESSAGE_NOTIF,
            title=f"New message from {name}",
            body=body[:160],
            data={
                "conversation_id": str(conversation.id),
                "subject": conversation.subject,
                "customer_name": name,
                "preview": body[:160],
            },
        )

    return payload


async def send_customer_message_to_conversation(
    db: AsyncSession,
    *,
    conversation_id: str,
    current_user: dict | None,
    guest_key: str | None,
    body: str,
) -> dict:
    conversation = await _get_conversation_or_404(db, conversation_id)
    _assert_owner(conversation, current_user, guest_key)
    return await send_customer_message(db, conversation=conversation, body=body)


async def _get_conversation_or_404(
    db: AsyncSession, conversation_id: str
) -> ChatConversation:
    try:
        cid = UUID(conversation_id)
    except (ValueError, AttributeError):
        raise ResourceNotFoundException("Chat conversation", conversation_id)
    conversation = (
        await db.execute(select(ChatConversation).where(ChatConversation.id == cid))
    ).scalar_one_or_none()
    if not conversation:
        raise ResourceNotFoundException("Chat conversation", conversation_id)
    return conversation


def _assert_owner(
    conversation: ChatConversation, current_user: dict | None, guest_key: str | None
) -> None:
    if conversation.customer_user_id:
        if (
            not current_user
            or UUID(current_user.get("sub")) != conversation.customer_user_id
        ):
            raise PermissionDeniedException("You can only access your own chat.")
        return
    if conversation.guest_key:
        if not guest_key or guest_key != conversation.guest_key:
            raise PermissionDeniedException("You can only access your own chat.")
        return
    raise PermissionDeniedException("You can only access your own chat.")


# ── Admin side ──────────────────────────────────────────────────


async def list_conversations(
    db: AsyncSession, *, status: str | None, q: str | None, page: int, page_size: int
) -> dict:
    """Admin inbox: conversations with the customer display name/mobile."""
    conditions = ["1 = 1"]
    params: dict = {}

    if status:
        conditions.append("cc.status = :status")
        params["status"] = status
    if q:
        conditions.append(
            "(cc.guest_name ILIKE :q OR u.mobile_number ILIKE :q "
            "OR gu.mobile_number ILIKE :q "
            "OR COALESCE(u.first_name,'') || ' ' || COALESCE(u.last_name,'') ILIKE :q "
            "OR COALESCE(gu.first_name,'') || ' ' || COALESCE(gu.last_name,'') ILIKE :q)"
        )
        params["q"] = f"%{q.strip()}%"

    where_sql = " AND ".join(conditions)
    total = int(
        (
            await db.execute(
                _text(
                    f"""
                    SELECT COUNT(*) FROM chat_conversations cc
                    LEFT JOIN users u ON u.id = cc.customer_user_id
                    LEFT JOIN users gu ON gu.mobile_number = cc.guest_mobile
                    WHERE {where_sql}
                    """
                ),
                params,
            )
        ).scalar_one()
    )

    rows = (
        (
            await db.execute(
                _text(
                    f"""
                SELECT cc.id, cc.status, cc.subject,
                       cc.guest_name, cc.guest_email, cc.guest_mobile,
                       cc.customer_user_id, cc.assigned_admin_id,
                       cc.last_message_at, cc.last_message_preview,
                       cc.unread_admin_count, cc.unread_customer_count,
                       cc.created_at,
                       COALESCE(
                           NULLIF(TRIM(COALESCE(u.first_name,'') || ' ' || COALESCE(u.last_name,'')), ''),
                           NULLIF(TRIM(COALESCE(gu.first_name,'') || ' ' || COALESCE(gu.last_name,'')), ''),
                           cc.guest_name
                       ) AS customer_name,
                       COALESCE(u.mobile_number, cc.guest_mobile) AS customer_mobile
                FROM chat_conversations cc
                LEFT JOIN users u ON u.id = cc.customer_user_id
                LEFT JOIN users gu ON gu.mobile_number = cc.guest_mobile
                WHERE {where_sql}
                ORDER BY cc.updated_at DESC
                LIMIT :limit OFFSET :offset
                """
                ),
                {**params, "limit": page_size, "offset": (page - 1) * page_size},
            )
        )
        .mappings()
        .all()
    )

    return {
        "items": [dict(r) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size,
    }


async def get_conversation_detail(
    db: AsyncSession, *, conversation_id: str, admin_user_id: UUID
) -> dict:
    """Admin view of a conversation + messages + the customer context
    panel payload. Opening the thread clears the admin unread counter."""
    conversation = await _get_conversation_or_404(db, conversation_id)

    messages = list(
        (
            await db.execute(
                select(ChatMessage)
                .where(ChatMessage.conversation_id == conversation.id)
                .order_by(ChatMessage.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    names = await _resolve_sender_names(db, messages)

    if conversation.unread_admin_count:
        conversation.unread_admin_count = 0
        conversation.assigned_admin_id = conversation.assigned_admin_id or admin_user_id
        await db.flush()

    from app.modules.chat.services.customer_context import customer_context

    context = await customer_context(
        db,
        customer_user_id=conversation.customer_user_id,
        guest_mobile=conversation.guest_mobile,
        guest_email=conversation.guest_email,
    )

    summary = _conversation_summary(conversation)
    summary["guest_email"] = conversation.guest_email
    summary["guest_mobile"] = conversation.guest_mobile
    summary["customer_user_id"] = (
        str(conversation.customer_user_id) if conversation.customer_user_id else None
    )
    summary["customer_name"] = await _detail_customer_name(db, conversation, context)

    return {
        "conversation": summary,
        "messages": _serialize_messages(messages, names, conversation.guest_name),
        "customer_context": context,
    }


async def _detail_customer_name(
    db: AsyncSession, conversation: ChatConversation, context: dict
) -> str | None:
    """Registered customer name for the admin thread header. Prefers the
    linked user account, then a mobile-matched profile, then guest name."""
    if conversation.customer_user_id:
        return await _user_display_name(db, conversation.customer_user_id, "Customer")
    profile = context.get("profile") or {}
    registered = (
        str(profile.get("first_name") or "").strip()
        + " "
        + str(profile.get("last_name") or "").strip()
    ).strip()
    return registered or conversation.guest_name or None


async def send_admin_message(
    db: AsyncSession, *, conversation_id: str, admin_user_id: UUID, body: str
) -> dict:
    """Admin reply. Marks the conversation OPEN, claims it for this admin
    and pushes the message to the customer in realtime (guests poll)."""
    conversation = await _get_conversation_or_404(db, conversation_id)
    if conversation.status == "CLOSED":
        raise BusinessException("This conversation is closed. Reopen it to reply.")

    message = ChatMessage(
        conversation_id=conversation.id,
        sender_type="ADMIN",
        sender_user_id=admin_user_id,
        body=body,
    )
    db.add(message)
    conversation.last_message_at = _utcnow()
    conversation.last_message_preview = body[:297]
    conversation.unread_customer_count = (conversation.unread_customer_count or 0) + 1
    conversation.assigned_admin_id = admin_user_id
    conversation.status = "OPEN"
    await db.flush()

    payload = _msg_payload(
        message,
        sender_name=await _user_display_name(db, admin_user_id, "Support Team"),
    )

    # Realtime to the logged-in customer (guests poll for new messages).
    if conversation.customer_user_id:
        await manager.send_to_user(
            conversation.customer_user_id,
            {"event": CHAT_MESSAGE_EVENT, "data": payload},
        )

    return payload


async def assign_conversation(
    db: AsyncSession, *, conversation_id: str, admin_user_id: UUID
) -> dict:
    conversation = await _get_conversation_or_404(db, conversation_id)
    conversation.assigned_admin_id = admin_user_id
    conversation.status = "OPEN"
    await db.flush()
    return _conversation_summary(conversation)


async def close_conversation(
    db: AsyncSession, *, conversation_id: str, admin_user_id: UUID
) -> dict:
    conversation = await _get_conversation_or_404(db, conversation_id)
    if conversation.status == "CLOSED":
        raise BusinessException("This conversation is already closed.")
    conversation.status = "CLOSED"
    conversation.assigned_admin_id = conversation.assigned_admin_id or admin_user_id
    await db.flush()

    # Let the customer know the thread is done (guests poll the summary).
    if conversation.customer_user_id:
        await manager.send_to_user(
            conversation.customer_user_id,
            {
                "event": CHAT_MESSAGE_EVENT,
                "data": {
                    "conversation_id": str(conversation.id),
                    "status": "CLOSED",
                    "system": True,
                },
            },
        )
    return _conversation_summary(conversation)
