"""0101 — partner support ticket conversation thread

Doc Ref: 04_FINANCE_ACCOUNT.md AC4 (Help & support)

Tickets were single-shot: the partner's report and the admin's resolution
notes lived on the ticket row, with no way to ask a follow-up question.
This table holds the conversation. The opening report is copied into the
thread for existing tickets so every ticket has a first message.

Idempotent, raw SQL — matches the 0100 style.
"""

from alembic import op
import sqlalchemy as sa

revision = "0101"
down_revision = "0100"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    conn.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS partner_support_ticket_messages (
                id BIGSERIAL PRIMARY KEY,
                uuid UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
                ticket_id BIGINT NOT NULL
                    REFERENCES partner_support_tickets(id) ON DELETE CASCADE,
                author_type VARCHAR(20) NOT NULL
                    CHECK (author_type IN ('PARTNER', 'ADMIN', 'SYSTEM')),
                author_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
                author_name VARCHAR(255),
                body TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_partner_support_message_ticket "
            "ON partner_support_ticket_messages (ticket_id, created_at, id)"
        )
    )

    # Every ticket already contains the partner's opening report. Put it in
    # the thread once, preserving the ticket's own timestamp so the history
    # reads in the order it happened.
    conn.execute(
        sa.text(
            """
            INSERT INTO partner_support_ticket_messages (
                uuid, ticket_id, author_type, author_user_id, author_name,
                body, created_at
            )
            SELECT gen_random_uuid(), t.id, 'PARTNER', p.user_id,
                   COALESCE(NULLIF(p.owner_name, ''), p.business_name, 'Partner'),
                   t.description, t.created_at
              FROM partner_support_tickets t
              JOIN partners p ON p.id = t.partner_id
             WHERE NOT EXISTS (
                   SELECT 1 FROM partner_support_ticket_messages m
                    WHERE m.ticket_id = t.id
             )
            """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP TABLE IF EXISTS partner_support_ticket_messages"))
