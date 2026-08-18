# ============================================================
# WAY TERO — ALEMBIC MIGRATION ENVIRONMENT
# File: alembic/env.py
# Doc Ref: Backend Architecture Part 2, Section 28
# Doc Ref: DB Architecture — Alembic, one migration per change
# ============================================================

import asyncio
from logging.config import fileConfig
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config
from alembic import context

# Import Base to detect all models
from app.core.database import Base
from app.core.config import settings

# Import ALL models here so Alembic can detect them
# (add new model imports as each phase is built)
from app.modules.auth.models import *
# from app.modules.customer.models import *
from app.modules.admin.coupon_models import Coupon, CouponServiceRule, CouponCityRule, CouponCustomerRule, CouponUsage  # noqa: F401
from app.modules.admin.models import AuditLog  # noqa: F401  (0037_platform_audit_logs)
# Hotel models — migration 0042_hotel_switch added columns + HotelReservationSplitEvent;
# without this import alembic check / autogenerate are blind to hotel schema drift.
from app.modules.hotel.models import *  # noqa: F401,F403  (0042_hotel_switch)
from app.modules.tour.models import *  # noqa: F401,F403  (0056_tour_module)
# Tour foreign keys point at these existing tables.  Import the owning models so
# Alembic's dependency sorter sees their metadata when generating/checking 0056.
from app.modules.master.models import City  # noqa: F401
from app.modules.partner.models import Partner  # noqa: F401
from app.modules.customer.models import Customer  # noqa: F401
from app.modules.booking.models import MasterBooking, BookingService  # noqa: F401

config = context.config

# Override sqlalchemy.url from our settings
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
