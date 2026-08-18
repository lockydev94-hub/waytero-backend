"""fix audit_logs.user_id type (bigint -> uuid)

Doc Ref: BRD Part 8 §213 (7-year compliance retention)
Revision: 0064
DownRevision: 0063

audit_logs.user_id was created as BIGINT but users.id is UUID. Every
caller passes a UUID string, so every audit insert silently failed
(AuditLogger swallows errors by design) and the audit trail has been
writing user_id = NULL since migration 0037. All existing rows are NULL,
so the cast is safe: ALTER COLUMN ... TYPE UUID USING NULL.
"""

from alembic import op
import sqlalchemy as sa

revision = "0064"
down_revision = "0063"


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            ALTER TABLE audit_logs
                ALTER COLUMN user_id TYPE UUID USING NULL
            """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            ALTER TABLE audit_logs
                ALTER COLUMN user_id TYPE BIGINT USING NULL
            """
        )
    )
