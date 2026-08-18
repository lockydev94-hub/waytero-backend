# ============================================================
# WAY TERO — CELERY BACKGROUND WORKERS
# File: app/workers/celery_app.py
# Doc Ref: Backend Architecture Part 2, Section 24
# Doc Ref: System Architecture Section 15 — Background Processing
# Tasks: Notifications, Settlements, Invoices, Reports
# ============================================================

from celery import Celery
from app.core.config import settings

celery_app = Celery(
    "waytero",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=[
        "app.workers.notifications",
        "app.workers.settlements",
        "app.workers.invoices",
        "app.workers.reports",
        "app.workers.timeout_sweeper",
        "app.workers.inventory",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Kolkata",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    # Retry policy
    task_max_retries=3,
    task_default_retry_delay=60,
    # Beat schedule — the timeout sweeper is the first periodic job. Runs
    # every 60 seconds so the 10-minute default acceptance window never
    # misses by more than a minute. The actual comparison uses UTC server
    # time, not the Asia/Kolkata timezone above; the timezone setting
    # affects beat tick scheduling only.
    #
    # Inventory horizon extender runs hourly so that
    # HOTEL_INVENTORY_HORIZON_DAYS (default 365) of forward-looking rows
    # are always present for every ACTIVE hotel — that is what powers the
    # < 2-second availability search requirement (SRS Part 5 §192).
    beat_schedule={
        "expire-pending-partner-acceptances": {
            "task": "waytero.timeout_sweeper.expire_pending_partner_acceptances",
            "schedule": 60.0,
        },
        "extend-hotel-inventory-horizon": {
            "task": "waytero.inventory.extend_horizon",
            "schedule": 3600.0,
        },
    },
)
