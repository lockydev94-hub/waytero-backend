"""0100 — partner support tickets

Doc Ref: 04_FINANCE_ACCOUNT.md AC4 (Help & support)

Partner-side help previously had no place to put an issue: the app could show
FAQs and a phone number, but "Report a trip issue" had nowhere to land. This
table is that landing place. It is deliberately partner-scoped rather than
reusing customer_care_logs, because a partner issue is not about one customer
and must not enter the customer-care queue as though it were.

Idempotent, raw SQL — matches the 0099 style.
"""

from alembic import op
import sqlalchemy as sa

revision = "0100"
down_revision = "0099"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    conn.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS partner_support_tickets (
                id BIGSERIAL PRIMARY KEY,
                uuid UUID NOT NULL UNIQUE,
                partner_id BIGINT NOT NULL
                    REFERENCES partners(id) ON DELETE CASCADE,
                ticket_number VARCHAR(50) NOT NULL UNIQUE,
                category VARCHAR(50) NOT NULL,
                subject VARCHAR(255) NOT NULL,
                description TEXT NOT NULL,
                booking_reference VARCHAR(100),
                priority VARCHAR(20) NOT NULL DEFAULT 'NORMAL',
                status VARCHAR(30) NOT NULL DEFAULT 'OPEN',
                resolution_notes TEXT,
                resolved_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_partner_support_partner "
            "ON partner_support_tickets (partner_id, created_at DESC)"
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_partner_support_status "
            "ON partner_support_tickets (status, created_at DESC)"
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP TABLE IF EXISTS partner_support_tickets"))
