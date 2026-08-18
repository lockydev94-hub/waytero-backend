# ============================================================
# WAYTERO — EMAIL LOGS + EMAIL CONFIG DEFAULTS
# File: alembic/versions/0065_email_logs.py
#
#   * email_logs — append-only audit of every outbound email
#     (event type, recipient, subject, status SENT|FAILED,
#     error message, render payload for resend).
#   * Seeded SMTP config keys in system_configurations so the
#     admin Email Settings page can read/write them in one place.
#     EMAIL_ENABLED defaults to 'false' — nothing is sent (or
#     logged) until an admin flips it on and saves SMTP details.
# ============================================================
from alembic import op
import sqlalchemy as sa

revision = "0065"
down_revision = "0064"


def upgrade() -> None:
    conn = op.get_bind()

    conn.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS email_logs (
                id            BIGSERIAL PRIMARY KEY,
                event_type    VARCHAR(100)  NOT NULL,
                recipient     VARCHAR(255)  NOT NULL,
                recipient_name VARCHAR(255),
                subject       VARCHAR(500)  NOT NULL,
                template_code VARCHAR(100),
                status        VARCHAR(20)   NOT NULL DEFAULT 'SENT',
                error_message TEXT,
                attempt_count INTEGER       NOT NULL DEFAULT 1,
                payload       JSONB,
                related_type  VARCHAR(50),
                related_id    VARCHAR(100),
                sent_at       TIMESTAMPTZ,
                created_at    TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
                updated_at    TIMESTAMPTZ   NOT NULL DEFAULT NOW()
            )
            """
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_email_logs_status ON email_logs (status)"
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_email_logs_created ON email_logs (created_at DESC)"
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_email_logs_recipient ON email_logs (recipient)"
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_email_logs_event ON email_logs (event_type)"
        )
    )

    # ── Default email config keys (values live in system_configurations) ──
    defaults = [
        ("EMAIL_ENABLED", "false", "Master switch — emails are only sent/logged when true"),
        ("SMTP_HOST", "smtp.gmail.com", "SMTP server hostname"),
        ("SMTP_PORT", "587", "SMTP server port (587 STARTTLS / 465 SSL)"),
        ("SMTP_USER", "", "SMTP username (usually the sender account)"),
        ("SMTP_PASSWORD", "", "SMTP password / app password"),
        ("SMTP_FROM_EMAIL", "noreply@waytero.com", "Sender email address shown to recipients"),
        ("SMTP_FROM_NAME", "WayTero", "Sender display name"),
        ("SMTP_USE_TLS", "true", "Use STARTTLS when connecting"),
    ]
    for key, value, desc in defaults:
        conn.execute(
            sa.text(
                """
                INSERT INTO system_configurations (config_key, config_value, description, updated_at)
                VALUES (:k, :v, :d, NOW())
                ON CONFLICT (config_key) DO NOTHING
                """
            ),
            {"k": key, "v": value, "d": desc},
        )


def downgrade() -> None:
    op.get_bind().execute(sa.text("DROP TABLE IF EXISTS email_logs"))
