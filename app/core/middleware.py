# ============================================================
# WAY TERO — MIDDLEWARE
# File: app/core/middleware.py
# Doc Ref: Backend Architecture Part 2, Section 22
# Middleware: Request ID, Logging, Security Headers, Auth Guard
# ============================================================

import time
import uuid
from typing import Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

import structlog
from app.core.config import settings
from app.core.logging import get_logger
from app.core.security import decode_access_token

logger = get_logger(__name__)

# Endpoints that are intentionally public (no auth required).
# Everything else under /api/v1 is DENY-BY-DEFAULT: a valid access token
# is mandatory even when the route itself declares no auth dependency.
# Doc Ref: Security Hardening — Default-Deny Authentication.
PUBLIC_API_PREFIXES = (
    "/api/v1/auth",  # login, OTP, refresh, forgot/reset password, auth config
    "/api/v1/public",  # CMS, cab/hotel/tour search, blog, legal, leads, coupon validate
    "/api/v1/chat/availability",  # customer-web chat widget (smart routing probe)
)

# Non-API paths that must stay open (infra probes, docs in non-prod).
PUBLIC_PATHS = {"/health", "/", "/docs", "/redoc", "/openapi.json", "/favicon.ico"}


class AuthenticationGuardMiddleware(BaseHTTPMiddleware):
    """
    Default-deny gate: every /api/v1 request needs a valid Bearer access token
    unless the path is in the explicit public allowlist.

    The decoded payload is cached on request.state.user_payload so the
    get_current_user dependency can reuse it instead of re-decoding.

    Doc Ref: Security Hardening — S1 unauthenticated admin endpoint closure.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        method = request.method
        path = request.url.path

        # CORS preflight must never require auth.
        if method == "OPTIONS":
            return await call_next(request)

        # WebSocket channel does its own token handshake (query param check).
        if path.startswith("/ws"):
            return await call_next(request)

        if path in PUBLIC_PATHS:
            return await call_next(request)

        if path.startswith("/api/v1") and not path.startswith(PUBLIC_API_PREFIXES):
            auth_header = request.headers.get("Authorization", "")
            token = None
            if auth_header.lower().startswith("bearer "):
                token = auth_header[7:].strip()
            if not token:
                return self._unauthorized()

            payload = decode_access_token(token)
            if not payload:
                return self._unauthorized()

            request.state.user_payload = payload
            request.state.user_id = payload.get("sub")

        return await call_next(request)

    @staticmethod
    def _unauthorized() -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content={
                "success": False,
                "message": "Authentication required",
                "code": "AUTH_FAILED",
                "errors": [],
            },
            headers={"WWW-Authenticate": "Bearer"},
        )


class RequestIDMiddleware(BaseHTTPMiddleware):
    """
    Generates a unique X-Request-ID for every incoming request.
    Doc Ref: Section 22 — Request ID middleware.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id

        # Bind to structlog context for this request
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """
    Logs every request with endpoint, method, status, execution time.
    Doc Ref: Section 22 — Logging Middleware.
    Doc Ref: Section 31 — Log format.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        start_time = time.perf_counter()

        response = await call_next(request)

        execution_time_ms = round((time.perf_counter() - start_time) * 1000, 2)

        # Get user_id if set by auth dependency
        user_id = getattr(request.state, "user_id", None)

        logger.info(
            "request_processed",
            method=request.method,
            endpoint=str(request.url.path),
            status_code=response.status_code,
            execution_time_ms=execution_time_ms,
            user_id=user_id,
            client_ip=request.client.host if request.client else None,
        )

        response.headers["X-Response-Time"] = f"{execution_time_ms}ms"
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """
    Adds security headers to every response.
    Doc Ref: Section 22 — Security Middleware.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        response = await call_next(request)

        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = (
            "geolocation=(), microphone=(), camera=()"
        )

        if settings.is_production:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
            response.headers["Content-Security-Policy"] = "default-src 'self'"

        return response
