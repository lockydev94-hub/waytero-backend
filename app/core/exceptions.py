# ============================================================
# WAY TERO — CUSTOM EXCEPTIONS
# File: app/core/exceptions.py
# Doc Ref: Backend Architecture Part 2, Section 21
# Standardized exception hierarchy for the entire platform.
# ============================================================

from typing import Optional, Any, Dict


class WayTeroException(Exception):
    """Base exception for all WayTero application errors."""

    def __init__(
        self,
        message: str,
        code: str = "WAYTERO_ERROR",
        status_code: int = 500,
        details: Optional[Dict[str, Any]] = None,
    ):
        self.message = message
        self.code = code
        self.status_code = status_code
        self.details = details or {}
        super().__init__(self.message)


class BusinessException(WayTeroException):
    """
    Business rule violation.
    Example: Partner already exists, booking limit exceeded.
    """

    def __init__(
        self, message: str, code: str = "BUSINESS_ERROR", details: Optional[Dict] = None
    ):
        super().__init__(message=message, code=code, status_code=422, details=details)


class ValidationException(WayTeroException):
    """
    Input validation failure.
    Example: Invalid phone number, missing required field.
    """

    def __init__(
        self,
        message: str,
        code: str = "VALIDATION_ERROR",
        details: Optional[Dict] = None,
    ):
        super().__init__(message=message, code=code, status_code=400, details=details)


class ResourceNotFoundException(WayTeroException):
    """
    Requested resource does not exist.
    Example: Booking not found, Partner not found.
    """

    def __init__(self, resource: str, identifier: Any = None):
        message = f"{resource} not found"
        if identifier:
            message = f"{resource} with id '{identifier}' not found"
        super().__init__(message=message, code="RESOURCE_NOT_FOUND", status_code=404)


class PermissionDeniedException(WayTeroException):
    """
    Access denied due to insufficient permissions.
    Example: Non-admin accessing admin endpoint.
    """

    def __init__(
        self, message: str = "Permission denied", code: str = "PERMISSION_DENIED"
    ):
        super().__init__(message=message, code=code, status_code=403)


class AuthenticationException(WayTeroException):
    """
    Authentication failure.
    Example: Invalid OTP, expired token, invalid credentials.
    """

    def __init__(
        self, message: str = "Authentication failed", code: str = "AUTH_FAILED"
    ):
        super().__init__(message=message, code=code, status_code=401)


class DuplicateResourceException(WayTeroException):
    """
    Resource already exists.
    Example: Phone number already registered, Vehicle RC already exists.
    """

    def __init__(self, resource: str, field: Optional[str] = None):
        message = f"{resource} already exists"
        if field:
            message = f"{resource} with this {field} already exists"
        super().__init__(message=message, code="DUPLICATE_RESOURCE", status_code=409)


class ServiceUnavailableException(WayTeroException):
    """
    External service unavailable.
    Example: SMS provider down, Payment gateway timeout.
    """

    def __init__(self, service: str):
        super().__init__(
            message=f"Service '{service}' is temporarily unavailable. Please try again.",
            code="SERVICE_UNAVAILABLE",
            status_code=503,
        )


class FinancialException(WayTeroException):
    """
    Financial operation failure.
    Example: Insufficient wallet balance, settlement already processed.
    """

    def __init__(
        self,
        message: str,
        code: str = "FINANCIAL_ERROR",
        details: Optional[Dict] = None,
    ):
        super().__init__(message=message, code=code, status_code=422, details=details)
