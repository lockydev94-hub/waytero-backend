# ============================================================
# WAY TERO — ADMIN LIVE CHAT API
# File: app/modules/chat/admin_api.py
# Doc Ref: Website Chat §4 — Admin chat surface
# Prefix: /admin/chat  (registered in api/router.py)
#
# Admin endpoints for the portal's chat board. Returns raw objects
# (paginated {items,total,...}) matching the admin-portal service
# convention — NOT the {success,message,data} envelope.
#
# POST /admin/chat/presence is the heartbeat that keeps the admin in
# Redis `chat:admins_presence` so customer chats can route to online
# support (smart routing).
# ============================================================

from __future__ import annotations

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import require_roles
from app.modules.chat.schemas import AdminPresenceRequest, AdminReplyRequest
from app.modules.chat.services import (
    assign_conversation,
    close_conversation,
    get_conversation_detail,
    list_conversations,
    send_admin_message,
)
from app.modules.chat.services.presence import is_support_online, mark_presence

router = APIRouter()

SUPPORT_ROLES = ("SUPER_ADMIN", "ADMIN", "CCO")


@router.post(
    "/presence",
    tags=["Chat"],
    summary="Admin presence heartbeat (online/away/offline)",
)
async def update_presence(
    body: AdminPresenceRequest,
    admin=Depends(require_roles(*SUPPORT_ROLES)),
):
    """Called by the admin portal on open, every ~30s and on close.
    Offline removes the admin from the online set; online/away refresh
    the heartbeat timestamp."""
    await mark_presence(admin["sub"], body.status)
    return {"online": await is_support_online()}


@router.get(
    "/conversations",
    tags=["Chat"],
    summary="Admin chat inbox",
)
async def list_chat_conversations(
    status: Optional[str] = Query(None, description="WAITING | OPEN | CLOSED"),
    q: Optional[str] = Query(None, description="Search name / mobile"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    admin=Depends(require_roles(*SUPPORT_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    return await list_conversations(
        db, status=status, q=q, page=page, page_size=page_size
    )


@router.get(
    "/conversations/{conversation_id}",
    tags=["Chat"],
    summary="Conversation detail + customer context panel",
)
async def chat_detail(
    conversation_id: str,
    admin=Depends(require_roles(*SUPPORT_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    return await get_conversation_detail(
        db, conversation_id=conversation_id, admin_user_id=UUID(admin["sub"])
    )


@router.post(
    "/conversations/{conversation_id}/messages",
    status_code=201,
    tags=["Chat"],
    summary="Reply to a conversation as admin",
)
async def admin_reply(
    conversation_id: str,
    body: AdminReplyRequest,
    admin=Depends(require_roles(*SUPPORT_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    return await send_admin_message(
        db,
        conversation_id=conversation_id,
        admin_user_id=UUID(admin["sub"]),
        body=body.body,
    )


@router.post(
    "/conversations/{conversation_id}/assign",
    tags=["Chat"],
    summary="Claim / assign a conversation to the current admin",
)
async def claim_conversation(
    conversation_id: str,
    admin=Depends(require_roles(*SUPPORT_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    return await assign_conversation(
        db, conversation_id=conversation_id, admin_user_id=UUID(admin["sub"])
    )


@router.post(
    "/conversations/{conversation_id}/close",
    tags=["Chat"],
    summary="Close a conversation",
)
async def close_chat(
    conversation_id: str,
    admin=Depends(require_roles(*SUPPORT_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    return await close_conversation(
        db, conversation_id=conversation_id, admin_user_id=UUID(admin["sub"])
    )
