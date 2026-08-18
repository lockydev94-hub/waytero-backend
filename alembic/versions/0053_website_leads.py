"""Website lead capture — partner applications + contact messages

Revision ID: 0053
Revises: 0052

Creates two lead-capture tables fed by the public website (customer-web):

  - ``partner_applications`` — "Become a Partner" form on /partner.
    Admin reviews in the portal and either converts the lead into a real
    partner (via the existing POST /admin/partners flow) or rejects it.
  - ``contact_messages`` — "Send us a message" form on /contact.

Both endpoints are public (no auth), so the tables carry no FKs to users.
Status flow for applications: NEW → CONTACTED → CONVERTED | REJECTED.

Doc Ref: Website Lead Capture §1 — Database Schema
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    conn.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS partner_applications (
                id              BIGSERIAL PRIMARY KEY,
                business_name   VARCHAR(255) NOT NULL,
                business_type   VARCHAR(50)  NOT NULL,
                contact_person  VARCHAR(255) NOT NULL,
                mobile          VARCHAR(15)  NOT NULL,
                email           VARCHAR(255) NOT NULL,
                city            VARCHAR(150),
                details         TEXT,
                status          VARCHAR(20)  NOT NULL DEFAULT 'NEW',
                admin_notes     TEXT,
                reviewed_by     UUID REFERENCES users(id) ON DELETE SET NULL,
                reviewed_at     TIMESTAMPTZ,
                created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
                updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
            )
            """
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_partner_apps_status ON partner_applications (status)"
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_partner_apps_created ON partner_applications (created_at DESC)"
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_partner_apps_mobile ON partner_applications (mobile)"
        )
    )

    conn.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS contact_messages (
                id          BIGSERIAL PRIMARY KEY,
                name        VARCHAR(255) NOT NULL,
                email       VARCHAR(255) NOT NULL,
                mobile      VARCHAR(15),
                subject     VARCHAR(300) NOT NULL,
                message     TEXT         NOT NULL,
                is_read     BOOLEAN      NOT NULL DEFAULT false,
                created_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
            )
            """
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_contact_msgs_unread ON contact_messages (is_read, created_at DESC)"
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP TABLE IF EXISTS contact_messages"))
    conn.execute(sa.text("DROP TABLE IF EXISTS partner_applications"))
