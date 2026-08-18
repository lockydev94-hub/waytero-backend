# ============================================================
# WAY TERO — FASTAPI APPLICATION ENTRY POINT
# File: app/main.py
# Doc Ref: Backend Architecture Part 1 & Part 2
# Doc Ref: System Architecture — Request Flow
#
# Architecture: Clean Architecture + Modular Monolith
# Stack: FastAPI | Python 3.13+ | PostgreSQL 17 | Redis | MinIO
# Phase: 0 — Foundation
# ============================================================

from fastapi import FastAPI, Request
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.core.lifespan import lifespan
from app.core.middleware import (
    RequestIDMiddleware,
    RequestLoggingMiddleware,
    SecurityHeadersMiddleware,
    AuthenticationGuardMiddleware,
)
from app.core.exceptions import WayTeroException
from app.api.router import api_router


def create_application() -> FastAPI:
    """
    Factory function — creates and configures the FastAPI application.
    """
    app = FastAPI(
        title=settings.APP_NAME,
        description="WayTero Travel Operating System — Backend API",
        version=settings.APP_VERSION,
        docs_url="/docs" if not settings.is_production else None,
        redoc_url="/redoc" if not settings.is_production else None,
        openapi_url="/openapi.json" if not settings.is_production else None,
        lifespan=lifespan,
    )

    # ── Realtime WebSocket connection registry (Doc Ref: BRD Part 7 §155) ──
    # Exposed on app.state so the WS endpoint + any test harness can reach
    # the same singleton without re-importing the module.
    from app.modules.notification.realtime import manager as realtime_manager

    app.state.realtime_manager = realtime_manager

    # ---- Middleware (order matters — outermost first) ----
    # Doc Ref: Backend Architecture Part 2, Section 22
    #
    # IMPORTANT: Starlette middleware is applied in REVERSE registration order.
    # The LAST middleware added becomes the OUTERMOST layer (runs first on request,
    # last on response). We need CORSMiddleware outermost so it always sets
    # Access-Control-Allow-Origin, even when inner BaseHTTPMiddleware layers
    # raise/swallow exceptions (the known Starlette CORS + BaseHTTPMiddleware issue).
    #
    # Execution order (request in → response out):
    #   CORS → AuthGuard → SecurityHeaders → RequestLogging → RequestID → Route Handler
    app.add_middleware(RequestIDMiddleware)
    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(AuthenticationGuardMiddleware)

    # ---- CORS — must be registered LAST so it runs OUTERMOST ----
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "X-Response-Time"],
    )

    # ---- Exception Handlers ----
    @app.exception_handler(WayTeroException)
    async def waytero_exception_handler(request: Request, exc: WayTeroException):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "success": False,
                "message": exc.message,
                "code": exc.code,
                "errors": exc.details,
            },
        )

    # ── HTTPException handler ────────────────────────────────────────────────
    # Many routers raise bare ``HTTPException(status_code, detail)`` (FastAPI's
    # built-in class). Without this handler, FastAPI's default would convert
    # them to ``{"detail": ...}`` instead of the standard envelope, so callers
    # of /admin/* and /partner/* endpoints would see a different shape from
    # the one the WayTeroException path produces. The 404 handler below is
    # more specific and still wins on 404; this handler covers everything
    # else raised via HTTPException.
    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        code = {
            400: "VALIDATION_ERROR",
            401: "AUTH_FAILED",
            403: "PERMISSION_DENIED",
            409: "DUPLICATE_RESOURCE",
            422: "BUSINESS_ERROR",
            500: "SERVER_ERROR",
        }.get(exc.status_code, "ERROR")
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "success": False,
                "message": str(exc.detail) if exc.detail else "Error",
                "code": code,
                "errors": [],
            },
        )

    @app.exception_handler(404)
    async def not_found_handler(request: Request, exc):
        return JSONResponse(
            status_code=404,
            content={
                "success": False,
                "message": "Endpoint not found",
                "code": "NOT_FOUND",
            },
        )

    @app.exception_handler(500)
    async def server_error_handler(request: Request, exc):
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "message": "Internal server error",
                "code": "SERVER_ERROR",
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        # Catch all unhandled exceptions so CORS middleware can still add headers.
        # Doc Ref: FastAPI BaseHTTPMiddleware known issue — raw exceptions bypass CORS.
        import logging

        logging.getLogger("waytero.api").error(
            "Unhandled exception: %s %s → %s",
            request.method,
            request.url.path,
            exc,
            exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "message": "Internal server error",
                "code": "SERVER_ERROR",
                "detail": (
                    str(exc)
                    if not __import__(
                        "app.core.config", fromlist=["settings"]
                    ).settings.is_production
                    else None
                ),
            },
        )

    # ---- API Routes (versioned) ----
    # Doc Ref: Section 5 — All APIs versioned under /api/v1/
    app.include_router(api_router, prefix="/api/v1")

    # ---- WebSocket route (NOT under /api/v1 — clients connect to /ws) ----
    # Doc Ref: BRD Part 7 §155 — Realtime channel
    from app.modules.notification.realtime.ws_endpoint import router as ws_router

    app.include_router(ws_router)

    # ---- Health Check ----
    # Doc Ref: Docker Architecture Section 29 — /health endpoint
    @app.get("/health", tags=["System"], summary="Health Check")
    async def health_check(request: Request):
        redis_available = getattr(request.app.state, "redis_available", False)

        # Live-check Redis if it was connected at startup
        if redis_available and request.app.state.redis is not None:
            try:
                await request.app.state.redis.ping()
            except Exception:
                redis_available = False

        # Realtime stats — Doc Ref: BRD Part 7 §155
        rt_stats = realtime_manager.stats()

        return {
            "success": True,
            "message": "WayTero API is running",
            "data": {
                "app": settings.APP_NAME,
                "version": settings.APP_VERSION,
                "environment": settings.APP_ENV,
                "services": {
                    "api": "healthy",
                    "redis": "healthy" if redis_available else "unavailable",
                    "websocket": "healthy",
                },
                "realtime": rt_stats,
            },
        }

    @app.get("/", tags=["System"], summary="Root")
    async def root():
        return {
            "success": True,
            "message": f"Welcome to {settings.APP_NAME} API",
            "data": {"version": settings.APP_VERSION, "docs": "/docs"},
        }

    return app


# --- Application instance
app = create_application()
