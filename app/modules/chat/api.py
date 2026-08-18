# ============================================================
# WAY TERO — PUBLIC LIVE CHAT API
# File: app/modules/chat/api.py
# Doc Ref: Website Chat §2 — Public API
# Prefix: /chat  (registered in api/router.py)
#
# Public endpoints for the customer-web chat widget. Auth is OPTIONAL:
# a valid JWT identifies a logged-in customer, otherwise the request
# must carry a guest identity / guest_key. `optional_current_user`
# mirrors get_current_user but returns None instead of raising so the
# same endpoints serve both cases.
# ============================================================

from __future__ import annotations

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import decode_access_token
from app.modules.chat.schemas import ConversationCreateRequest, SendMessageRequest
from app.modules.chat.services import (
    create_or_open_conversation,
    get_customer_conversation,
    send_customer_message_to_conversation,
)
from app.modules.chat.services.presence import is_support_online
from app.shared.responses.base import success_response

router = APIRouter()

_security = HTTPBearer(auto_error=False)


async def optional_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_security),
    db: AsyncSession = Depends(get_db),
):
    """Like get_current_user but returns None for missing/invalid tokens."""
    if not credentials:
        return None
    payload = decode_access_token(credentials.credentials)
    if not payload or not payload.get("sub"):
        return None
    try:
        UUID(payload["sub"])
    except (ValueError, TypeError):
        return None
    from app.modules.auth.repositories import UserRepository

    user = await UserRepository(db).get_by_id(UUID(payload["sub"]))
    if not user or not user.is_active:
        return None
    return payload


@router.get(
    "/availability",
    tags=["Chat"],
    summary="Is support online right now? (smart routing)",
)
async def chat_availability(db: AsyncSession = Depends(get_db)):
    """Drives the widget's online/offline state before a chat is started."""
    online = await is_support_online()
    return success_response("Support status", {"support_online": online})


@router.post(
    "/conversations",
    status_code=201,
    tags=["Chat"],
    summary="Start (or reopen) a support chat",
)
async def start_conversation(
    body: ConversationCreateRequest,
    current_user=Depends(optional_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a thread for the caller (or reuse an open one). If a first
    message is included it's sent immediately. Smart routing decides
    OPEN (agent assigned) vs WAITING (support offline) — the response
    carries support_online so the widget can show the right banner."""
    result = await create_or_open_conversation(db, current_user=current_user, body=body)
    return success_response("Chat ready", result)


@router.get(
    "/conversations/{conversation_id}",
    tags=["Chat"],
    summary="Fetch a conversation + its messages",
)
async def fetch_conversation(
    conversation_id: str,
    guest_key: Optional[str] = Query(None, max_length=64),
    current_user=Depends(optional_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await get_customer_conversation(
        db,
        conversation_id=conversation_id,
        current_user=current_user,
        guest_key=guest_key,
    )
    return success_response("Conversation fetched", result)


@router.post(
    "/conversations/{conversation_id}/messages",
    status_code=201,
    tags=["Chat"],
    summary="Send a message as the customer",
)
async def send_message(
    conversation_id: str,
    body: SendMessageRequest,
    current_user=Depends(optional_current_user),
    db: AsyncSession = Depends(get_db),
):
    message = await send_customer_message_to_conversation(
        db,
        conversation_id=conversation_id,
        current_user=current_user,
        guest_key=body.guest_key,
        body=body.body,
    )
    return success_response("Message sent", {"message": message})
