# ============================================================
# WAYTERO — ADMIN-EDITABLE EMAIL TEMPLATES
# File: alembic/versions/0108_email_templates.py
#
# One row per customised transactional email. No rows are seeded: the
# application's built-in registry is the default, and a row here is an
# override created from Admin → Communication → Email Templates.
# Branding (logo, platform name, support footer) is never stored here —
# it is read from system_configurations on every render.
# ============================================================
from alembic import op
import sqlalchemy as sa

revision = "0108"
down_revision = "0107"


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS email_templates (
                id          BIGSERIAL PRIMARY KEY,
                event_type  VARCHAR(100) NOT NULL UNIQUE,
                label       VARCHAR(150),
                subject     VARCHAR(500) NOT NULL,
                heading     VARCHAR(255),
                body        TEXT,
                cta_label   VARCHAR(120),
                cta_url     VARCHAR(500),
                tone        VARCHAR(20),
                is_active   BOOLEAN      NOT NULL DEFAULT TRUE,
                created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
            )
            """
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_email_templates_event "
            "ON email_templates (event_type)"
        )
    )


def downgrade() -> None:
    op.get_bind().execute(sa.text("DROP TABLE IF EXISTS email_templates"))
