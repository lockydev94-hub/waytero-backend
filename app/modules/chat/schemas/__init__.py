# ============================================================
# WAY TERO — LIVE CHAT SCHEMAS
# File: app/modules/chat/schemas/__init__.py
# Doc Ref: Website Chat §2 — API contracts
# ============================================================

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.modules.admin.public_leads_api import _normalize_mobile


class GuestIdentity(BaseModel):
    """Identity for a chat started while not logged in.

    ``guest_key`` is a client-generated UUID persisted in localStorage —
    it's what lets the customer come back to the same thread later.
    """

    guest_key: str = Field(..., min_length=8, max_length=64)
    name: str = Field(..., min_length=1, max_length=255)
    email: Optional[EmailStr] = None
    mobile: Optional[str] = None

    @field_validator("guest_key")
    @classmethod
    def clean_guest_key(cls, v: str) -> str:
        return v.strip()

    @field_validator("mobile")
    @classmethod
    def clean_mobile(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not v.strip():
            return None
        try:
            return _normalize_mobile(v)
        except ValueError:
            raise ValueError("Enter a valid 10-digit Indian mobile number")


class ConversationCreateRequest(BaseModel):
    subject: Optional[str] = Field(None, max_length=50)
    message: Optional[str] = Field(None, min_length=1, max_length=5000)
    guest: Optional[GuestIdentity] = None


class SendMessageRequest(BaseModel):
    body: str = Field(..., min_length=1, max_length=5000)
    guest_key: Optional[str] = Field(None, max_length=64)


class AdminReplyRequest(BaseModel):
    body: str = Field(..., min_length=1, max_length=5000)


class AdminPresenceRequest(BaseModel):
    status: Literal["online", "away", "offline"] = "online"
