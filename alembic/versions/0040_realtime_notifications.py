"""0040_realtime_notifications — fcm_tokens + notification_outbox + realtime bus

Revision ID: 0040_realtime_notifications
Revises: 0039_partner_acceptance_gate
Create Date: 2026-08-08

Doc Ref:
  BRD Part 3 §42  — Driver Assignment after partner acceptance
  BRD Part 7 §155 — Realtime / push channel
  BRD Part 8 §213 — Notification engine

What:
  1. fcm_tokens — device/browser FCM tokens, scoped by (user_id, fcm_token)
     so the same user can register multiple devices (web + mobile).
  2. notification_outbox — durable record of every notification the system
     tried to dispatch. Channels = ws (always attempted) + fcm (if user
     has a registered token). The outbox lets the partner portal show a
     history of bookings-assigned events even if the user wasn't online.
  3. system_configurations seeding:
       - WEBSOCKET_PING_INTERVAL  (default 25s)
       - FCM_DISPATCH_ENABLED     (default 'true' — env-overrideable)

The booking-status fields added by migration 0039 stay untouched; we
only add new tables.

All CREATE/ALTER statements are idempotent so a partial run is
recoverable and re-applying is safe.
"""

from alembic import op
import sqlalchemy as sa

revision = "0040_realtime_notifications"
down_revision = "0039_partner_acceptance_gate"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # ── fcm_tokens ──────────────────────────────────────────────────────────
    # One row per (user_id, fcm_token). Multiple devices per user supported
    # by allowing duplicate fcm_tokens across different user_ids but the
    # composite unique constraint keeps one canonical token per user.
    conn.execute(
        sa.text(
            """
        CREATE TABLE IF NOT EXISTS fcm_tokens (
            id            BIGSERIAL    PRIMARY KEY,
            user_id       UUID         NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            fcm_token     TEXT         NOT NULL,
            platform      VARCHAR(20)  NOT NULL DEFAULT 'WEB',
            user_agent    TEXT         NULL,
            is_active     BOOLEAN      NOT NULL DEFAULT TRUE,
            last_seen_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_fcm_user_token UNIQUE (user_id, fcm_token)
        )
    """
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_fcm_user_active ON fcm_tokens (user_id, is_active)"
        )
    )

    # ── notification_outbox ────────────────────────────────────────────────
    # Append-only log of every notification dispatched. Powering:
    #   - partner portal "Notifications" dropdown history
    #   - admin audit of who-got-notified-when
    # delivered_via is an array of channels that succeeded: ['ws','fcm'].
    conn.execute(
        sa.text(
            """
        CREATE TABLE IF NOT EXISTS notification_outbox (
            id              BIGSERIAL    PRIMARY KEY,
            user_id         UUID         NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            event_type      VARCHAR(80)  NOT NULL,
            title           VARCHAR(255) NOT NULL,
            body            TEXT         NULL,
            data            JSONB        NULL,
            booking_id      BIGINT       NULL,
            delivered_via   VARCHAR(20)  NOT NULL DEFAULT 'ws',
            read_at         TIMESTAMPTZ  NULL,
            created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
        )
    """
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_outbox_user_created ON notification_outbox (user_id, created_at DESC)"
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_outbox_user_unread ON notification_outbox (user_id) WHERE read_at IS NULL"
        )
    )

    # ── Seed realtime config ───────────────────────────────────────────────
    conn.execute(
        sa.text(
            """
        INSERT INTO system_configurations (config_key, config_value, description, updated_at)
        VALUES (
            'WEBSOCKET_PING_INTERVAL',
            '25',
            'Seconds between server-side WebSocket pings to detect dead clients',
            NOW()
        )
        ON CONFLICT (config_key) DO NOTHING
    """
        )
    )
    conn.execute(
        sa.text(
            """
        INSERT INTO system_configurations (config_key, config_value, description, updated_at)
        VALUES (
            'FCM_DISPATCH_ENABLED',
            'true',
            'When true, push notifications are also sent via Firebase FCM in addition to WebSocket',
            NOW()
        )
        ON CONFLICT (config_key) DO NOTHING
    """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()

    conn.execute(
        sa.text(
            "DELETE FROM system_configurations WHERE config_key IN ('WEBSOCKET_PING_INTERVAL', 'FCM_DISPATCH_ENABLED')"
        )
    )
    conn.execute(sa.text("DROP INDEX IF EXISTS idx_outbox_user_unread"))
    conn.execute(sa.text("DROP INDEX IF EXISTS idx_outbox_user_created"))
    conn.execute(sa.text("DROP TABLE IF EXISTS notification_outbox"))
    conn.execute(sa.text("DROP INDEX IF EXISTS idx_fcm_user_active"))
    conn.execute(sa.text("DROP TABLE IF EXISTS fcm_tokens"))
