"""disable homepage bands whose backing feature does not exist yet

Doc Ref: owner rule 2026-10-15 — "if database not have then will be not
         show in website as placeholder"; brand master (WAYTERO-PROJECT-MASTER).
Revision: 0074
DownRevision: 0073

Two 0055-era default homepage sections advertise capabilities the platform
does not have: NEWSLETTER (no subscribe endpoint exists — the form captured
emails into nothing and claimed "12,000+ subscribers") and APP_DOWNLOAD
("available on iOS and Android" with empty app_store_links everywhere).
Deactivate them so the public homepage renders neither; rows are kept and
admins can re-enable from the CMS once the features are real. Guarded on
section_key; safe to re-run. Downgrade preserves admin visibility choices.
"""

from alembic import op

revision = "0074"
down_revision = "0073"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE page_sections
        SET is_active = FALSE, updated_at = NOW()
        WHERE section_key IN ('NEWSLETTER', 'APP_DOWNLOAD')
          AND is_active = TRUE
        """
    )


def downgrade() -> None:
    # Prior admin-disabled sections cannot be distinguished from this migration's changes.
    pass
