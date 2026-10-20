# ============================================================
# WAY TERO — APPLICATION LIFESPAN
# File: app/core/lifespan.py
# Doc Ref: Backend Architecture Part 2, Section 15
# Phase: 0 — Foundation
# Handles startup and shutdown events.
#
# Auto-migration strategy:
#   - Uses subprocess to run `alembic upgrade head` — avoids
#     the asyncio.run() re-entrancy crash that occurs when
#     calling asyncio.run() inside an already-running event loop
#     (e.g., Uvicorn's event loop at startup).
#   - Seeder (scripts/seed_admin.py) is also invoked via
#     subprocess so it runs with its own clean event loop.
# ============================================================

import asyncio
import subprocess
import sys
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from sqlalchemy import text
from app.core.logging import configure_logging, get_logger
from app.core.config import settings

logger = get_logger(__name__)

# How often the in-process acceptance watcher looks for offers that expired.
# The Celery beat schedule runs the same pass every 60 s; this loop exists so
# auto-assign also works on a machine that is not running a Celery worker, and
# 30 s keeps a partner's 3-minute window honest without hammering the database.
_ACCEPTANCE_SWEEP_SECONDS = 30.0


async def _acceptance_expiry_loop() -> None:
    """Expire unanswered partner offers while the API process is up.

    Runs the sweeper's own async core, in this event loop, so no second loop is
    created (the Celery entry-point needs `run_worker_async` precisely because
    it has no loop of its own). Two guards matter:

      * the pass only touches bookings when `auto_assign_enabled` is on, so a
        disabled chain costs one small SELECT per interval and nothing else,
      * the sweep selects `FOR UPDATE SKIP LOCKED`, so running here while a
        Celery worker also runs cannot double-revert a booking.

    Failures are logged and the loop continues: an offer not expired on this
    tick is expired on the next one, and the API must not die over a background
    janitor.
    """
    from app.core.database import AsyncSessionLocal
    from app.modules.booking.services.auto_assign import settings as auto_assign_settings
    from app.workers.timeout_sweeper import _expire_pending_acceptances_async

    while True:
        try:
            async with AsyncSessionLocal() as db:
                enabled = (await auto_assign_settings(db)).enabled
            if enabled:
                reverted = await _expire_pending_acceptances_async()
                if reverted:
                    logger.info(
                        "acceptance_expiry_pass",
                        reverted=reverted,
                        source="lifespan",
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — background loop must survive
            logger.error("acceptance_expiry_failed", error=str(exc))
        await asyncio.sleep(_ACCEPTANCE_SWEEP_SECONDS)


class DatabaseUnavailableError(RuntimeError):
    """Raised at startup when PostgreSQL cannot be reached — aborts boot."""


async def _check_database_connectivity(timeout: float = 5.0) -> None:
    """
    Verify PostgreSQL is reachable before running migrations/seeders.

    Fails fast: if the DB refuses the connection (WinError 1225 /
    ConnectionRefusedError) or is unreachable within ``timeout`` seconds,
    this raises DatabaseUnavailableError, which aborts application startup.
    Without this guard the app would log every migration/seeder failure and
    still report ``waytero_ready``, masking a dead database.
    """
    # Import here so the module still loads even if the engine can't be built.
    from app.core.database import engine

    try:

        async def _ping() -> None:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))

        await asyncio.wait_for(_ping(), timeout=timeout)
        logger.info(
            "db_connectivity_ok",
            host=settings.DB_HOST,
            port=settings.DB_PORT,
            db=settings.DB_NAME,
        )
    except Exception as exc:  # noqa: BLE001 — surface any connect failure
        logger.error(
            "db_connectivity_failed",
            host=settings.DB_HOST,
            port=settings.DB_PORT,
            db=settings.DB_NAME,
            error=str(exc),
            hint=(
                "PostgreSQL is not reachable. Start it with: "
                "docker-compose up -d db  (from Infrastructure/docker/development/)"
            ),
        )
        raise DatabaseUnavailableError(
            f"Cannot reach PostgreSQL at {settings.DB_HOST}:{settings.DB_PORT} "
            f"({settings.DB_NAME}). Aborting startup. Original error: {exc}"
        ) from exc


def _clean_output(raw: str) -> str:
    """Strip SQLAlchemy engine INFO lines from subprocess stdout — only keep app-level messages."""
    lines = [
        line
        for line in raw.splitlines()
        if line.strip()
        and "sqlalchemy.engine" not in line
        and not line.startswith("BEGIN")
        and not line.startswith("SELECT")
        and not line.startswith("INSERT")
        and not line.startswith("UPDATE")
        and not line.startswith("COMMIT")
        and not line.startswith("ROLLBACK")
        and not line.startswith("[generated in")
        and not line.startswith("[raw sql]")
    ]
    return "\n".join(lines).strip() or "(no output)"


# Root of the Backend/ directory — alembic.ini lives here
_BACKEND_DIR = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)


def _run_migrations() -> None:
    """
    Run `alembic upgrade head` as a subprocess.
    Using subprocess avoids the asyncio re-entrancy issue:
      asyncio.run() cannot be called from a running event loop.
    Safe to call every restart — Alembic is idempotent.

    Raises DatabaseUnavailableError on failure — a broken/incomplete
    schema must not serve requests, so this aborts startup.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=_BACKEND_DIR,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            logger.info(
                "db_migrations_ok",
                stdout=result.stdout.strip() or "(no output)",
            )
        else:
            logger.error(
                "db_migrations_failed",
                returncode=result.returncode,
                stderr=result.stderr.strip(),
                stdout=result.stdout.strip(),
            )
            raise DatabaseUnavailableError(
                "Alembic migrations failed — aborting startup. "
                "See db_migrations_failed log above for the traceback."
            )
    except subprocess.TimeoutExpired:
        logger.error(
            "db_migrations_timeout",
            hint="Migration took > 120 s — check DB connectivity",
        )
        raise DatabaseUnavailableError(
            "Alembic migrations timed out — aborting startup."
        )


def _run_legal_seeder() -> None:
    """
    Seed the customer-website page bodies (privacy / terms / refund /
    cookies / booking-instructions) into system_configurations. The copy
    itself lives in scripts/legal_page_content.py, which is also what the
    website ships as its built-in fallback.
    Idempotent — ON CONFLICT DO NOTHING, never overwrites admin edits.
    """
    seeder_path = os.path.join(_BACKEND_DIR, "scripts", "seed_legal_content.py")
    if not os.path.exists(seeder_path):
        logger.warning("legal_seeder_not_found", path=seeder_path)
        return
    try:
        result = subprocess.run(
            [sys.executable, seeder_path],
            cwd=_BACKEND_DIR,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            logger.info("legal_seeder_ok", output=_clean_output(result.stdout))
        else:
            logger.error(
                "legal_seeder_failed",
                returncode=result.returncode,
                stderr=result.stderr.strip(),
            )
    except subprocess.TimeoutExpired:
        logger.error("legal_seeder_timeout")
    except Exception as exc:
        logger.error("legal_seeder_error", error=str(exc))


def _run_wallet_seeder() -> None:
    """
    Ensure every partner and customer has a wallet row (idempotent).
    Runs after migrations so customer_wallets table is guaranteed to exist.
    """
    seeder_path = os.path.join(_BACKEND_DIR, "scripts", "seed_wallets.py")
    if not os.path.exists(seeder_path):
        logger.warning("wallet_seeder_not_found", path=seeder_path)
        return
    try:
        result = subprocess.run(
            [sys.executable, seeder_path],
            cwd=_BACKEND_DIR,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            logger.info("wallet_seeder_ok", output=_clean_output(result.stdout))
        else:
            logger.error(
                "wallet_seeder_failed",
                returncode=result.returncode,
                stderr=result.stderr.strip(),
            )
    except subprocess.TimeoutExpired:
        logger.error("wallet_seeder_timeout")
    except Exception as exc:
        logger.error("wallet_seeder_error", error=str(exc))


def _run_seeders() -> None:
    """
    Run the admin seeder as a subprocess.
    The seeder is idempotent — skips if SUPER_ADMIN already exists.
    """
    seeder_path = os.path.join(_BACKEND_DIR, "scripts", "seed_admin.py")
    if not os.path.exists(seeder_path):
        logger.warning("seeder_not_found", path=seeder_path)
        return

    try:
        result = subprocess.run(
            [sys.executable, seeder_path],
            cwd=_BACKEND_DIR,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            logger.info(
                "db_seeder_ok",
                output=_clean_output(result.stdout),
            )
        else:
            logger.error(
                "db_seeder_failed",
                returncode=result.returncode,
                stderr=result.stderr.strip(),
                stdout=result.stdout.strip(),
            )
    except subprocess.TimeoutExpired:
        logger.error("db_seeder_timeout")
    except Exception as exc:
        logger.error("db_seeder_error", error=str(exc))


def _run_partner_password_seeder() -> None:
    """
    Set default password "Waytero@15" (Argon2id) for PARTNER users
    with no password_hash, and set force_password_change = TRUE.
    Idempotent — only affects users with NULL password_hash.
    Doc Ref: Partner Portal — default password requirement
    """
    seeder_path = os.path.join(
        _BACKEND_DIR, "app", "scripts", "seed_partner_passwords.py"
    )
    if not os.path.exists(seeder_path):
        logger.warning("partner_password_seeder_not_found", path=seeder_path)
        return
    try:
        result = subprocess.run(
            [sys.executable, seeder_path],
            cwd=_BACKEND_DIR,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            logger.info(
                "partner_password_seeder_ok",
                output=result.stdout.strip() or "(no output)",
            )
        else:
            logger.error(
                "partner_password_seeder_failed",
                returncode=result.returncode,
                stderr=result.stderr.strip(),
            )
    except subprocess.TimeoutExpired:
        logger.error("partner_password_seeder_timeout")
    except Exception as exc:
        logger.error("partner_password_seeder_error", error=str(exc))


def _run_fix_stuck_bookings() -> None:
    """
    Fix any bookings that have payment_mode set but are missing invoice_number.
    Idempotent — safe to run on every restart.
    Doc Ref: BRD Part 3 §45 — post-payment state integrity
    """
    script_path = os.path.join(
        _BACKEND_DIR, "scripts", "fix_stuck_completed_bookings.py"
    )
    if not os.path.exists(script_path):
        logger.warning("fix_stuck_bookings_script_not_found", path=script_path)
        return
    try:
        result = subprocess.run(
            [sys.executable, script_path],
            cwd=_BACKEND_DIR,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            logger.info("fix_stuck_bookings_ok", output=_clean_output(result.stdout))
        else:
            logger.error(
                "fix_stuck_bookings_failed",
                returncode=result.returncode,
                stderr=result.stderr.strip(),
            )
    except subprocess.TimeoutExpired:
        logger.error("fix_stuck_bookings_timeout")
    except Exception as exc:
        logger.error("fix_stuck_bookings_error", error=str(exc))


def _run_fix_payment_mismatch() -> None:
    """
    Fix bookings where timeline shows PAYMENT_*_COLLECTED but
    payment_mode column is still NULL (partial commit on DB restart).
    Idempotent — safe every restart.
    Doc Ref: BRD Part 3 §45
    """
    script_path = os.path.join(_BACKEND_DIR, "scripts", "fix_payment_state_mismatch.py")
    if not os.path.exists(script_path):
        logger.warning("fix_payment_mismatch_script_not_found", path=script_path)
        return
    try:
        result = subprocess.run(
            [sys.executable, script_path],
            cwd=_BACKEND_DIR,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            logger.info("fix_payment_mismatch_ok", output=_clean_output(result.stdout))
        else:
            logger.error(
                "fix_payment_mismatch_failed",
                returncode=result.returncode,
                stderr=result.stderr.strip(),
            )
    except subprocess.TimeoutExpired:
        logger.error("fix_payment_mismatch_timeout")
    except Exception as exc:
        logger.error("fix_payment_mismatch_error", error=str(exc))


def _run_fix_hotel_payment_sync() -> None:
    """
    Re-roll hotel reservation totals + advance sums onto every master_booking
    whose booking_services contains a HOTEL row. Repairs legacy rows whose
    payment_status / total_paid_amount drifted out of sync because the
    partner-driven lifecycle never called the helper.
    Idempotent — re-runs the same SQL the live API uses; healthy rows produce
    unchanged values.
    Doc Ref: BRD Part 3 §35-§45, Part 7 §155
    """
    script_path = os.path.join(_BACKEND_DIR, "scripts", "fix_hotel_payment_sync.py")
    if not os.path.exists(script_path):
        logger.warning("fix_hotel_payment_sync_script_not_found", path=script_path)
        return
    try:
        result = subprocess.run(
            [sys.executable, script_path],
            cwd=_BACKEND_DIR,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            logger.info(
                "fix_hotel_payment_sync_ok", output=_clean_output(result.stdout)
            )
        else:
            logger.error(
                "fix_hotel_payment_sync_failed",
                returncode=result.returncode,
                stderr=result.stderr.strip(),
            )
    except subprocess.TimeoutExpired:
        logger.error("fix_hotel_payment_sync_timeout")
    except Exception as exc:
        logger.error("fix_hotel_payment_sync_error", error=str(exc))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI lifespan context manager.
    Runs startup tasks before yield, shutdown tasks after yield.
    """
    # ---- STARTUP ----

    # Configure structured logging first — must be called before any logger use
    configure_logging()

    logger.info(
        "waytero_startup",
        app_name=settings.APP_NAME,
        version=settings.APP_VERSION,
        environment=settings.APP_ENV,
    )

    # ---- Database Connectivity (fail fast — abort if DB is unreachable) ----
    # Without this guard the migration/seeder steps below each fail against a
    # dead DB but startup still reaches waytero_ready, masking the outage.
    logger.info("db_connectivity_starting", hint="Verifying PostgreSQL is reachable…")
    await _check_database_connectivity()

    # ---- Database Migrations (subprocess — safe in async context) ----
    # In production migrations run via a dedicated one-shot "migrate" compose
    # service (RUN_MIGRATIONS_ON_STARTUP=false) so uvicorn workers never race.
    if settings.RUN_MIGRATIONS_ON_STARTUP:
        logger.info("db_migrations_starting", hint="Running alembic upgrade head…")
        _run_migrations()
    else:
        logger.info(
            "db_migrations_skipped",
            hint="RUN_MIGRATIONS_ON_STARTUP=false — expecting the migrate service to have run.",
        )

    # ---- Default Seeders (idempotent — skips if data already exists) ----
    # Doc Ref: Security Hardening — H2. The admin seeder plants a super-admin
    # with known dev credentials, and the partner password seeder sets a
    # published default password. Neither may run in production — those
    # credentials are provisioned explicitly by an operator instead.
    if not settings.is_production:
        logger.info("db_seeders_starting", hint="Running default seeders…")
        _run_seeders()

        logger.info(
            "partner_password_seeder_starting",
            hint="Seeding default passwords for partners without passwords…",
        )
        _run_partner_password_seeder()
    else:
        logger.info(
            "db_seeders_skipped",
            hint="Production — admin/partner seeders disabled. "
            "Provision credentials via scripts/seed_admin.py or the forgot-password flow.",
        )

    # ---- Wallet Seeder (ensures every partner + customer has a wallet) ----
    logger.info(
        "wallet_seeder_starting", hint="Ensuring all partner/customer wallets exist…"
    )
    _run_wallet_seeder()

    # ---- Legal Content Seeder (privacy / terms / refund / cookies / instructions) ----
    logger.info("legal_seeder_starting", hint="Seeding legal page content…")
    _run_legal_seeder()

    # ---- Fix Stuck Bookings (payment collected but invoice missing) ----
    logger.info(
        "fix_stuck_bookings_starting",
        hint="Checking for bookings with payment but missing invoice…",
    )
    _run_fix_stuck_bookings()

    # ---- Fix Payment State Mismatch (timeline says paid, column still NULL) ----
    logger.info(
        "fix_payment_mismatch_starting", hint="Checking for payment state mismatches…"
    )
    _run_fix_payment_mismatch()

    # ---- Fix Hotel Payment Sync (re-roll hotel totals/collections onto master) ----
    # Repairs legacy master rows whose payment_status / total_paid_amount drifted
    # out of sync with their hotel_reservations / hotel_advance_payments rows
    # because the partner-driven lifecycle never called the helper. Idempotent.
    logger.info(
        "fix_hotel_payment_sync_starting",
        hint="Re-rolling hotel payment fields onto master bookings…",
    )
    _run_fix_hotel_payment_sync()

    # ---- Redis (optional in Phase 0 — warns cleanly if not running) ----
    # Doc Ref: Phase 1 Foundation — Redis is part of Data Services setup
    # In Phase 0, Redis is not required — startup continues without it.
    app.state.redis = None
    app.state.redis_available = False

    try:
        import redis.asyncio as aioredis

        redis_client = aioredis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=2,  # fail fast — do not hang startup
            socket_timeout=2,
        )
        await redis_client.ping()
        app.state.redis = redis_client
        app.state.redis_available = True
        logger.info("redis_connected", url=settings.REDIS_URL)

    except Exception:
        # Redis is not running — Phase 0 continues without it.
        # Start Redis with: docker run -d -p 6379:6379 redis:7-alpine
        logger.warning(
            "redis_unavailable",
            url=settings.REDIS_URL,
            hint="Start Redis: docker run -d -p 6379:6379 redis:7-alpine",
            impact="OTP, caching and session features will not work until Redis is available.",
        )

    logger.info(
        "waytero_ready",
        message="WayTero backend is ready to accept requests",
        redis=app.state.redis_available,
    )

    # ---- Acceptance expiry watcher (auto-assign timeout chain) ----
    # A fan-out round is only honest if an unanswered offer actually ends. The
    # Celery beat schedule runs the same sweep every minute; this in-process
    # loop is what makes the chain work on a machine with no worker running
    # (the owner's dev box), and it does nothing to bookings until
    # auto_assign_enabled is switched on.
    app.state.acceptance_expiry_task = asyncio.create_task(_acceptance_expiry_loop())

    yield  # Application runs here

    # ---- SHUTDOWN ----
    logger.info("waytero_shutdown", message="Shutting down WayTero backend...")

    task = getattr(app.state, "acceptance_expiry_task", None)
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        logger.info("acceptance_expiry_stopped")

    if app.state.redis is not None:
        await app.state.redis.aclose()
        logger.info("redis_disconnected")

    logger.info("waytero_shutdown_complete")
