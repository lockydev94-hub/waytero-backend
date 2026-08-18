# ============================================================
# WAYTERO — PARTNER NOTICES
# File: alembic/versions/0066_partner_notices.py
#
# Admin → Partner broadcast / targeted notices shown as a banner
# at the top of the partner portal (wallet insufficient balance,
# driver assignment pending, document expiry, policy updates…).
#
#   partner_notices         — the notice itself. audience is
#                             ALL_PARTNERS (everyone) or PARTNER
#                             (a single partner_id). status is
#                             ACTIVE | ARCHIVED (admin hides it).
#   partner_notice_reads    — per-partner dismissal record so the
#                             banner disappears once the partner
#                             has seen it (idempotent ON CONFLICT).
# ============================================================
from alembic import op
import sqlalchemy as sa

revision = "0066"
down_revision = "0065"


def upgrade() -> None:
    conn = op.get_bind()

    conn.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS partner_notices (
                id            BIGSERIAL PRIMARY KEY,
                notice_type   VARCHAR(80)   NOT NULL,
                title         VARCHAR(255)  NOT NULL,
                body          TEXT,
                priority      VARCHAR(20)   NOT NULL DEFAULT 'NORMAL',
                audience      VARCHAR(20)   NOT NULL DEFAULT 'ALL_PARTNERS',
                partner_id    BIGINT        REFERENCES partners(id) ON DELETE CASCADE,
                status        VARCHAR(20)   NOT NULL DEFAULT 'ACTIVE',
                created_by    UUID          REFERENCES users(id) ON DELETE SET NULL,
                created_at    TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
                updated_at    TIMESTAMPTZ   NOT NULL DEFAULT NOW()
            )
            """
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_partner_notices_status "
            "ON partner_notices (status, created_at DESC)"
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_partner_notices_audience "
            "ON partner_notices (audience, partner_id)"
        )
    )

    conn.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS partner_notice_reads (
                notice_id   BIGINT NOT NULL REFERENCES partner_notices(id) ON DELETE CASCADE,
                partner_id  BIGINT NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
                read_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (notice_id, partner_id)
            )
            """
        )
    )


def downgrade() -> None:
    op.get_bind().execute(sa.text("DROP TABLE IF EXISTS partner_notice_reads"))
    op.get_bind().execute(sa.text("DROP TABLE IF EXISTS partner_notices"))
