# ============================================================
# WAY TERO — STRUCTURED LOGGING
# File: app/core/logging.py
# Doc Ref: Backend Architecture Part 2, Section 31
# Doc Ref: System Architecture Section 14 — Observability
# Every request must log: request_id, user_id, endpoint,
# status_code, execution_time.
# ============================================================

import logging
import sys
from typing import Any, cast

import structlog
from app.core.config import settings


def configure_logging() -> None:
    """Configure structlog for structured JSON logging."""

    log_level = logging.DEBUG if settings.APP_DEBUG else logging.INFO

    # --- Standard library logging
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
    )

    # --- Structlog processors
    # NOTE: structlog.stdlib.add_logger_name is removed because it requires
    # a stdlib Logger with a .name attribute. PrintLoggerFactory creates
    # PrintLogger objects which do not have .name — causing AttributeError
    # on startup. Logger name is already bound via get_logger(__name__).
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    if settings.APP_DEBUG:
        # Human-readable in development
        structlog.configure(
            processors=cast(Any, shared_processors + [structlog.dev.ConsoleRenderer()]),
            wrapper_class=structlog.make_filtering_bound_logger(log_level),
            context_class=dict,
            logger_factory=structlog.PrintLoggerFactory(),
            cache_logger_on_first_use=True,
        )
    else:
        # JSON in production
        structlog.configure(
            processors=cast(
                Any,
                shared_processors
                + [
                    structlog.processors.dict_tracebacks,
                    structlog.processors.JSONRenderer(),
                ],
            ),
            wrapper_class=structlog.make_filtering_bound_logger(log_level),
            context_class=dict,
            logger_factory=structlog.PrintLoggerFactory(),
            cache_logger_on_first_use=True,
        )


def get_logger(name: str = __name__):
    """Get a structlog logger instance."""
    return structlog.get_logger(name)
