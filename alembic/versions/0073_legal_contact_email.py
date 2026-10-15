"""route unverified role emails in seeded legal pages to the approved address

Doc Ref: owner brand master (WAYTERO-PROJECT-MASTER, 2026-10-15) — the only
         confirmed inbound email channel is support@waytero.com.
Revision: 0073
DownRevision: 0072

The 0044-era legal seeder baked privacy@waytero.com into the DB-stored
privacy policy; that mailbox was never confirmed. Replace it with the
approved support address inside the LEGAL_PAGE_* JSON values. Guarded on
the exact marker, so admin-edited legal text is untouched and the
statement is re-runnable. No downgrade: the unverified address must not
come back.
"""

from alembic import op

revision = "0073"
down_revision = "0072"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE system_configurations
        SET config_value = replace(config_value, 'privacy@waytero.com', 'support@waytero.com'),
            updated_at = NOW()
        WHERE config_key LIKE 'LEGAL_PAGE_%'
          AND config_value LIKE '%privacy@waytero.com%'
        """
    )


def downgrade() -> None:
    # Intentionally a no-op: restoring an unverified mailbox would undo the
    # brand-master rule. The replacement is idempotent and safe to keep.
    pass
