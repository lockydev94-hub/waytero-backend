"""add tour_package_policies (per-package cancellation ladder)

Doc Ref: BRD Part 5 §119 — Tour cancellation policy
Revision: 0062
DownRevision: 0061

Tours used a single global ladder from system_configurations
(TOUR_CANCELLATION_*). This migration introduces an optional per-package
override table, mirroring how hotels carry their own ladder in
hotel_policies. A package without a row falls back to the global ladder —
the policy engine decides, and the policy_snapshot records which source
applied. No back-fill is needed: absence of a row is the fallback signal.
"""

from alembic import op
import sqlalchemy as sa

revision = "0062"
down_revision = "0061"


def upgrade() -> None:
    conn = op.get_bind()

    conn.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS tour_package_policies (
                id                        BIGSERIAL PRIMARY KEY,
                tour_package_id           BIGINT NOT NULL UNIQUE
                                              REFERENCES tour_packages(id) ON DELETE CASCADE,

                -- Ladder: days before travel_start_date → refund percent.
                -- Mirrors BRD Part 5 §119 (30d/100, 15d/75, 7d/50, <7d/0).
                cancellation_free_days     INTEGER DEFAULT 30,
                refund_percent_tier_1      NUMERIC(5,2) DEFAULT 100.00,
                cancellation_tier_1_days   INTEGER DEFAULT 15,
                refund_percent_tier_2      NUMERIC(5,2) DEFAULT 75.00,
                cancellation_tier_2_days   INTEGER DEFAULT 7,
                refund_percent_tier_3      NUMERIC(5,2) DEFAULT 50.00,
                refund_percent_last_minute NUMERIC(5,2) DEFAULT 0.00,

                cancellation_policy_text   TEXT,
                created_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
    )
    conn.execute(
        sa.text(
            """
            CREATE INDEX IF NOT EXISTS idx_tour_pkg_policy_package
                ON tour_package_policies (tour_package_id)
            """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP INDEX IF EXISTS idx_tour_pkg_policy_package"))
    conn.execute(sa.text("DROP TABLE IF EXISTS tour_package_policies"))
