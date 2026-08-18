"""seed tour cancellation config keys into system_configurations

Doc Ref: BRD Part 5 §119 — Tour cancellation policy
Revision: 0060
DownRevision: 0059
"""

from alembic import op
import sqlalchemy as sa

revision = "0060"
down_revision = "0059"


def upgrade() -> None:
    conn = op.get_bind()

    # Free window: 30 days → 100% refund
    conn.execute(
        sa.text(
            "INSERT INTO system_configurations (config_key, config_value, description) "
            "VALUES (:k, :v, :d) ON CONFLICT (config_key) DO NOTHING"
        ),
        {
            "k": "TOUR_CANCELLATION_FREE_DAYS",
            "v": "30",
            "d": "Days before travel_start_date for 100% refund",
        },
    )

    # Tier‑1: 15 days → 75% refund
    conn.execute(
        sa.text(
            "INSERT INTO system_configurations (config_key, config_value, description) "
            "VALUES (:k, :v, :d) ON CONFLICT (config_key) DO NOTHING"
        ),
        {
            "k": "TOUR_CANCELLATION_TIER_1_DAYS",
            "v": "15",
            "d": "Days before travel_start_date for 75% refund",
        },
    )

    # Tier‑2: 7 days → 50% refund
    conn.execute(
        sa.text(
            "INSERT INTO system_configurations (config_key, config_value, description) "
            "VALUES (:k, :v, :d) ON CONFLICT (config_key) DO NOTHING"
        ),
        {
            "k": "TOUR_CANCELLATION_TIER_2_DAYS",
            "v": "7",
            "d": "Days before travel_start_date for 50% refund",
        },
    )

    # Tier‑3 (platform default): 25% refund if any advance collected
    conn.execute(
        sa.text(
            "INSERT INTO system_configurations (config_key, config_value, description) "
            "VALUES (:k, :v, :d) ON CONFLICT (config_key) DO NOTHING"
        ),
        {
            "k": "TOUR_CANCELLATION_TIER_3_PERCENT",
            "v": "25",
            "d": "Refund percent for 7‑<30 day window (platform default)",
        },
    )

    # Last‑minute: <48 hours → 0% refund
    conn.execute(
        sa.text(
            "INSERT INTO system_configurations (config_key, config_value, description) "
            "VALUES (:k, :v, :d) ON CONFLICT (config_key) DO NOTHING"
        ),
        {
            "k": "TOUR_CANCELLATION_LAST_MINUTE_PERCENT",
            "v": "0",
            "d": "Refund percent for last‑minute cancellations (<48h)",
        },
    )

    # Tier‑1 percentage override
    conn.execute(
        sa.text(
            "INSERT INTO system_configurations (config_key, config_value, description) "
            "VALUES (:k, :v, :d) ON CONFLICT (config_key) DO NOTHING"
        ),
        {
            "k": "TOUR_CANCELLATION_TIER_1_PERCENT",
            "v": "75",
            "d": "Refund percent for Tier‑1 window",
        },
    )

    # Tier‑2 percentage override
    conn.execute(
        sa.text(
            "INSERT INTO system_configurations (config_key, config_value, description) "
            "VALUES (:k, :v, :d) ON CONFLICT (config_key) DO NOTHING"
        ),
        {
            "k": "TOUR_CANCELLATION_TIER_2_PERCENT",
            "v": "50",
            "d": "Refund percent for Tier‑2 window",
        },
    )

    # Tier‑3 percentage override
    conn.execute(
        sa.text(
            "INSERT INTO system_configurations (config_key, config_value, description) "
            "VALUES (:k, :v, :d) ON CONFLICT (config_key) DO NOTHING"
        ),
        {
            "k": "TOUR_CANCELLATION_TIER_3_PERCENT",
            "v": "5",
            "d": "Refund percent for Tier‑3 window (fallback)",
        },
    )


def downgrade() -> None:
    conn = op.get_bind()

    keys = [
        "TOUR_CANCELLATION_FREE_DAYS",
        "TOUR_CANCELLATION_TIER_1_DAYS",
        "TOUR_CANCELLATION_TIER_2_DAYS",
        "TOUR_CANCELLATION_TIER_3_PERCENT",
        "TOUR_CANCELLATION_LAST_MINUTE_PERCENT",
        "TOUR_CANCELLATION_TIER_1_PERCENT",
        "TOUR_CANCELLATION_TIER_2_PERCENT",
    ]
    for k in keys:
        conn.execute(
            sa.text("DELETE FROM system_configurations WHERE config_key = :k"),
            {"k": k},
        )
