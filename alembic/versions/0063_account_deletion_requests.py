"""add account_deletion_requests (customer account deletion)

Doc Ref: BRD Part 7 §134 (notifications), Privacy Policy — account deletion
Revision: 0063
DownRevision: 0062

Customers request account deletion from the website (public or logged-in).
Admin reviews the request in the admin portal and approves — approval
soft-deletes the user (status + deleted_at), anonymises personal
identifiers, deactivates sessions, and keeps booking/financial records
anonymised as required by law.

One PENDING request per mobile at a time (unique partial index).
"""

from alembic import op
import sqlalchemy as sa

revision = "0063"
down_revision = "0062"


def upgrade() -> None:
    conn = op.get_bind()

    conn.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS account_deletion_requests (
                id                   BIGSERIAL PRIMARY KEY,
                customer_user_id     UUID REFERENCES users(id) ON DELETE SET NULL,
                customer_id          BIGINT REFERENCES customers(id) ON DELETE SET NULL,
                mobile               VARCHAR(20) NOT NULL,
                email                VARCHAR(255),
                full_name            VARCHAR(255),
                reason               TEXT,
                request_source       VARCHAR(20) NOT NULL DEFAULT 'PUBLIC',
                status               VARCHAR(20) NOT NULL DEFAULT 'PENDING',
                requested_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                reviewed_by_user_id  UUID REFERENCES users(id),
                reviewed_at          TIMESTAMPTZ,
                review_note          TEXT
            )
            """
        )
    )
    conn.execute(
        sa.text(
            """
            CREATE INDEX IF NOT EXISTS idx_deletion_req_status
                ON account_deletion_requests (status, requested_at DESC)
            """
        )
    )
    conn.execute(
        sa.text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_deletion_req_pending_mobile
                ON account_deletion_requests (mobile)
                WHERE status = 'PENDING'
            """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP INDEX IF EXISTS uq_deletion_req_pending_mobile"))
    conn.execute(sa.text("DROP INDEX IF EXISTS idx_deletion_req_status"))
    conn.execute(sa.text("DROP TABLE IF EXISTS account_deletion_requests"))
