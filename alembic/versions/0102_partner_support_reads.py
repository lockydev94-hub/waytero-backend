"""0102 — partner support read receipts

Doc Ref: 04_FINANCE_ACCOUNT.md AC4 (Help & support)

A conversation needs to tell each side how many replies they have not seen.
Read state is per message and per side: an admin message is unread for the
partner until the partner opens it, and vice versa. Existing messages are
backfilled as read so deploying this does not flood both sides with badges
for conversations that predate the feature.

Idempotent, raw SQL — matches the 0100/0101 style.
"""

from alembic import op
import sqlalchemy as sa

revision = "0102"
down_revision = "0101"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    conn.execute(
        sa.text(
            """
            ALTER TABLE partner_support_ticket_messages
                ADD COLUMN IF NOT EXISTS read_by_partner_at TIMESTAMPTZ,
                ADD COLUMN IF NOT EXISTS read_by_admin_at TIMESTAMPTZ
            """
        )
    )

    # Existing history is treated as already read. New messages set the
    # author's own read timestamp and leave the other side unread.
    conn.execute(
        sa.text(
            """
            UPDATE partner_support_ticket_messages
               SET read_by_partner_at = COALESCE(read_by_partner_at, created_at),
                   read_by_admin_at = COALESCE(read_by_admin_at, created_at)
             WHERE read_by_partner_at IS NULL OR read_by_admin_at IS NULL
            """
        )
    )

    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_support_msg_admin_unread "
            "ON partner_support_ticket_messages (ticket_id) "
            "WHERE read_by_admin_at IS NULL AND author_type = 'PARTNER'"
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_support_msg_partner_unread "
            "ON partner_support_ticket_messages (ticket_id) "
            "WHERE read_by_partner_at IS NULL AND author_type = 'ADMIN'"
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP INDEX IF EXISTS idx_support_msg_partner_unread"))
    conn.execute(sa.text("DROP INDEX IF EXISTS idx_support_msg_admin_unread"))
    conn.execute(
        sa.text(
            """
            ALTER TABLE partner_support_ticket_messages
                DROP COLUMN IF EXISTS read_by_partner_at,
                DROP COLUMN IF EXISTS read_by_admin_at
            """
        )
    )
