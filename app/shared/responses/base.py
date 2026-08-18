# ============================================================
# WAY TERO — STANDARD API RESPONSE FORMAT
# File: app/shared/responses/base.py
# Doc Ref: Backend Architecture Part 2, Section 30
# ============================================================
from typing import Any, Optional, List
from pydantic import BaseModel


class SuccessResponse(BaseModel):
    success: bool = True
    message: str
    data: Optional[Any] = None


class ErrorResponse(BaseModel):
    success: bool = False
    message: str
    errors: Optional[List[Any]] = None
    code: Optional[str] = None


class PaginatedResponse(BaseModel):
    success: bool = True
    message: str = "Data retrieved successfully"
    data: Optional[List[Any]] = None
    total: int = 0
    page: int = 1
    per_page: int = 20
    total_pages: int = 0


def success_response(message: str, data: Any = None) -> dict:
    return {"success": True, "message": message, "data": data}


def error_response(
    message: str,
    errors: Optional[List[Any]] = None,
    code: Optional[str] = None,
) -> dict:
    return {"success": False, "message": message, "errors": errors or [], "code": code}
