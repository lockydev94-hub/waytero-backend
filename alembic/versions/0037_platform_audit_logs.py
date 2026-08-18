"""0037_platform_audit_logs — platform-wide audit trail

The /admin/audit-logs endpoint in api_dashboard.py §24 has been calling
SELECT … FROM audit_logs since the admin dashboard module shipped, but no
migration ever created the table. The endpoint therefore 500s on every call.
The only persisted audit-shaped table today is financial_audit_logs (0008),
which is finance-only and uses different column names (old_data / new_data,
performed_by as UUID) — not what the service queries.

This migration creates the canonical audit_logs table, matching the schema
in Docs/05_Database/09_DATABASE_SCHEMA_PART_8_AUDIT_NOTIFICATION.md §76-104
plus the user_agent / request_id pair documented in
Docs/04_API_Documentation/12_ADMIN_API.md §24.

Indexes are aligned with the most-frequent filters:
  - user_id (admin "show me everything actor X did")
  - module_name + action_type (filter dropdowns in the UI)
  - (entity_name, entity_id) (drill-down from an entity page)
  - created_at DESC (the default sort for the table)

Idempotent: CREATE TABLE / INDEX IF NOT EXISTS, so re-runs are safe.

Doc Ref: Docs/04_API_Documentation/12_ADMIN_API.md §24, §28
         Docs/05_Database/09_DATABASE_SCHEMA_PART_8_AUDIT_NOTIFICATION.md §76-104
         BRD Part 8 §213 (Audit Compliance, 7-year retention)
"""

import sqlalchemy as sa
from alembic import op

revision = "0037_platform_audit_logs"
down_revision = "0036_hotel_settlement"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # ── audit_logs ─────────────────────────────────────────────────────────────
    # Append-only platform audit trail. No UPDATE or DELETE allowed at the
    # application layer (compliance: 7-year retention per BRD Part 8 §213).
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS audit_logs (
            id              BIGSERIAL PRIMARY KEY,
            user_id         BIGINT,
            module_name     VARCHAR(100) NOT NULL,
            entity_name     VARCHAR(100),
            entity_id       BIGINT,
            action_type     VARCHAR(100) NOT NULL,
            old_values      JSONB,
            new_values      JSONB,
            ip_address      VARCHAR(100),
            user_agent      TEXT,
            request_id      VARCHAR(100),
            created_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))

    # Most-frequent filters — keep these narrow so the planner can use them.
    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_audit_user
            ON audit_logs(user_id)
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_audit_module
            ON audit_logs(module_name)
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_audit_action_type
            ON audit_logs(action_type)
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_audit_entity
            ON audit_logs(entity_name, entity_id)
    """))

    # Default sort is created_at DESC — the operator sees the latest first.
    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_audit_created_at
            ON audit_logs(created_at DESC)
    """))


def downgrade() -> None:
    conn = op.get_bind()

    conn.execute(sa.text("DROP INDEX IF EXISTS idx_audit_created_at"))
    conn.execute(sa.text("DROP INDEX IF EXISTS idx_audit_entity"))
    conn.execute(sa.text("DROP INDEX IF EXISTS idx_audit_action_type"))
    conn.execute(sa.text("DROP INDEX IF EXISTS idx_audit_module"))
    conn.execute(sa.text("DROP INDEX IF EXISTS idx_audit_user"))
    conn.execute(sa.text("DROP TABLE IF EXISTS audit_logs"))
