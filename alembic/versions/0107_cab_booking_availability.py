"""Admin-controlled availability for each cab booking type.

Revision ID: 0107
Revises: 0106
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0107"
down_revision = "0106"
branch_labels = None
depends_on = None

_KEYS = (
    "CAB_BOOKING_LOCAL_ENABLED",
    "CAB_BOOKING_AIRPORT_ENABLED",
    "CAB_BOOKING_OUTSTATION_ENABLED",
    "CAB_BOOKING_ONE_WAY_ENABLED",
    "CAB_BOOKING_ROUND_TRIP_ENABLED",
    "CAB_BOOKING_DAILY_ENABLED",
)


def upgrade() -> None:
    conn = op.get_bind()
    # One Way is switched off for the launch configuration. ON CONFLICT DO
    # NOTHING keeps an operator's existing choice if this key already exists.
    conn.execute(
        sa.text(
            """
            INSERT INTO system_configurations
                (config_key, config_value, description, updated_at)
            VALUES
                ('CAB_BOOKING_LOCAL_ENABLED', 'true',
                 'Allow new Local cab bookings', NOW()),
                ('CAB_BOOKING_AIRPORT_ENABLED', 'true',
                 'Allow new Airport cab bookings', NOW()),
                ('CAB_BOOKING_OUTSTATION_ENABLED', 'true',
                 'Allow new Outstation cab bookings', NOW()),
                ('CAB_BOOKING_ONE_WAY_ENABLED', 'false',
                 'Allow new One Way cab bookings', NOW()),
                ('CAB_BOOKING_ROUND_TRIP_ENABLED', 'true',
                 'Allow new Round Trip cab bookings', NOW()),
                ('CAB_BOOKING_DAILY_ENABLED', 'true',
                 'Allow new Cab Rent (daily) bookings', NOW())
            ON CONFLICT (config_key) DO NOTHING
            """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "DELETE FROM system_configurations WHERE config_key IN "
            "('CAB_BOOKING_LOCAL_ENABLED', "
            "'CAB_BOOKING_AIRPORT_ENABLED', "
            "'CAB_BOOKING_OUTSTATION_ENABLED', "
            "'CAB_BOOKING_ONE_WAY_ENABLED', "
            "'CAB_BOOKING_ROUND_TRIP_ENABLED', "
            "'CAB_BOOKING_DAILY_ENABLED')"
        )
    )
