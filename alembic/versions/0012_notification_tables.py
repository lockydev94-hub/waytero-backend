"""Add notification tables: notifications, notification_templates, sms_queue, whatsapp_queue, email_queue, push_notifications

Revision ID: 0012_notification_tables
Revises: 0011_default_vehicle_pricing
Create Date: 2026-07-28

Doc Ref:
  - DB Schema Part 8 §9  — notifications
  - DB Schema Part 8 §11 — notification_templates
  - DB Schema Part 8 §12 — sms_queue
  - DB Schema Part 8 §13 — whatsapp_queue
  - DB Schema Part 8 §14 — email_queue
  - DB Schema Part 8 §15 — push_notifications
  - Notification API §9  — Create Template (seed defaults)
  - Admin API §22        — Notification Management

NOTE: users.id is UUID (see migration 0001_phase1_auth_tables).
      All user_id FK columns in this migration use UUID, not BIGINT.
"""

from alembic import op
import sqlalchemy as sa

revision = "0012_notification_tables"
down_revision = "0011_default_vehicle_pricing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # ── notifications ─────────────────────────────────────────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS notifications (
            id                  BIGSERIAL PRIMARY KEY,
            uuid                UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
            user_id             UUID REFERENCES users(id) ON DELETE SET NULL,
            notification_type   VARCHAR(50),
            channel             VARCHAR(50),
            title               VARCHAR(255),
            message             TEXT,
            status              VARCHAR(50) DEFAULT 'QUEUED',
            sent_at             TIMESTAMP,
            delivered_at        TIMESTAMP,
            read_at             TIMESTAMP,
            created_at          TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """))

    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_notification_user ON notifications(user_id)"))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_notification_status ON notifications(status)"))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_notification_channel ON notifications(channel)"))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_notification_created ON notifications(created_at DESC)"))

    # ── notification_templates ────────────────────────────────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS notification_templates (
            id               BIGSERIAL PRIMARY KEY,
            template_code    VARCHAR(100) NOT NULL,
            channel          VARCHAR(50) NOT NULL,
            template_name    VARCHAR(255),
            template_content TEXT,
            is_active        BOOLEAN NOT NULL DEFAULT TRUE,
            created_at       TIMESTAMP NOT NULL DEFAULT NOW(),
            UNIQUE (template_code, channel)
        )
    """))

    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_notif_template_code ON notification_templates(template_code)"))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_notif_template_channel ON notification_templates(channel)"))

    # ── sms_queue ─────────────────────────────────────────────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS sms_queue (
            id                BIGSERIAL PRIMARY KEY,
            mobile_number     VARCHAR(15),
            template_code     VARCHAR(100),
            message           TEXT,
            status            VARCHAR(50) DEFAULT 'QUEUED',
            retry_count       INTEGER NOT NULL DEFAULT 0,
            provider_response TEXT,
            scheduled_at      TIMESTAMP,
            sent_at           TIMESTAMP,
            created_at        TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """))

    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_sms_queue_status ON sms_queue(status)"))

    # ── whatsapp_queue ────────────────────────────────────────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS whatsapp_queue (
            id                    BIGSERIAL PRIMARY KEY,
            mobile_number         VARCHAR(15),
            template_code         VARCHAR(100),
            payload               JSONB,
            status                VARCHAR(50) DEFAULT 'QUEUED',
            retry_count           INTEGER NOT NULL DEFAULT 0,
            provider_message_id   VARCHAR(255),
            sent_at               TIMESTAMP,
            created_at            TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """))

    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_wa_queue_status ON whatsapp_queue(status)"))

    # ── email_queue ───────────────────────────────────────────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS email_queue (
            id            BIGSERIAL PRIMARY KEY,
            email_address VARCHAR(255),
            subject       VARCHAR(255),
            body          TEXT,
            status        VARCHAR(50) DEFAULT 'QUEUED',
            retry_count   INTEGER NOT NULL DEFAULT 0,
            sent_at       TIMESTAMP,
            created_at    TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """))

    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_email_queue_status ON email_queue(status)"))

    # ── push_notifications ────────────────────────────────────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS push_notifications (
            id           BIGSERIAL PRIMARY KEY,
            user_id      UUID REFERENCES users(id) ON DELETE SET NULL,
            device_token TEXT,
            title        VARCHAR(255),
            message      TEXT,
            status       VARCHAR(50) DEFAULT 'QUEUED',
            sent_at      TIMESTAMP,
            created_at   TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """))

    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_push_notif_user ON push_notifications(user_id)"))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_push_notif_status ON push_notifications(status)"))

    # ── Seed default notification templates ───────────────────────────────────
    # UNIQUE is now (template_code, channel) so ON CONFLICT targets both columns
    conn.execute(sa.text("""
        INSERT INTO notification_templates (template_code, channel, template_name, template_content, is_active)
        VALUES
          ('BOOKING_CONFIRMED',    'SMS',      'Booking Confirmed (SMS)',
           'Dear {{customer_name}}, your booking {{booking_number}} is confirmed. Track your trip on WayTero. Thank you!', TRUE),
          ('BOOKING_CONFIRMED',    'WHATSAPP', 'Booking Confirmed (WhatsApp)',
           'Hi {{customer_name}}! Your booking *{{booking_number}}* is confirmed. Your driver will be assigned shortly. Have a great trip!', TRUE),
          ('BOOKING_CANCELLED',    'SMS',      'Booking Cancelled (SMS)',
           'Dear {{customer_name}}, your booking {{booking_number}} has been cancelled. Refund (if any) will be processed in 3-5 business days.', TRUE),
          ('DRIVER_ASSIGNED',      'SMS',      'Driver Assigned (SMS)',
           'Your driver {{driver_name}} is on the way. Vehicle: {{vehicle_number}}. Track on WayTero app.', TRUE),
          ('DRIVER_ASSIGNED',      'PUSH',     'Driver Assigned (Push)',
           'Driver {{driver_name}} has been assigned to your booking {{booking_number}}. Vehicle: {{vehicle_number}}.', TRUE),
          ('TRIP_STARTED',         'PUSH',     'Trip Started (Push)',
           'Your trip {{booking_number}} has started. Have a safe journey!', TRUE),
          ('TRIP_COMPLETED',       'SMS',      'Trip Completed (SMS)',
           'Your trip {{booking_number}} is completed. Thank you for choosing WayTero! Please rate your experience.', TRUE),
          ('PAYMENT_SUCCESS',      'SMS',      'Payment Success (SMS)',
           'Payment of Rs.{{payment_amount}} received for booking {{booking_number}}. Thank you!', TRUE),
          ('PAYMENT_SUCCESS',      'PUSH',     'Payment Success (Push)',
           'Payment of Rs.{{payment_amount}} received for {{booking_number}}.', TRUE),
          ('PAYMENT_FAILED',       'SMS',      'Payment Failed (SMS)',
           'Payment for booking {{booking_number}} failed. Please retry or contact support.', TRUE),
          ('REFUND_PROCESSED',     'SMS',      'Refund Processed (SMS)',
           'Refund of Rs.{{payment_amount}} for booking {{booking_number}} has been processed. It will reflect in 3-5 business days.', TRUE),
          ('SETTLEMENT_GENERATED', 'SMS',      'Settlement Generated (SMS)',
           'Your settlement for Rs.{{payment_amount}} has been generated. It will be transferred within 2 business days.', TRUE),
          ('SETTLEMENT_PAID',      'SMS',      'Settlement Paid (SMS)',
           'Settlement of Rs.{{payment_amount}} has been transferred to your account. Check your bank statement.', TRUE),
          ('HOTEL_CONFIRMED',      'EMAIL',    'Hotel Booking Confirmed (Email)',
           'Dear {{customer_name}}, your reservation at {{hotel_name}} is confirmed for booking {{booking_number}}. We look forward to hosting you!', TRUE),
          ('HOTEL_CHECKIN_REMINDER','PUSH',    'Hotel Check-in Reminder (Push)',
           'Reminder: Check-in at {{hotel_name}} is tomorrow. Booking: {{booking_number}}.', TRUE),
          ('TOUR_CONFIRMED',       'EMAIL',    'Tour Booking Confirmed (Email)',
           'Dear {{customer_name}}, your tour {{tour_name}} (Booking: {{booking_number}}) is confirmed! Our team will contact you 24 hours before departure.', TRUE),
          ('TOUR_STARTED',         'PUSH',     'Tour Started (Push)',
           'Your tour {{tour_name}} has started! Enjoy the journey.', TRUE),
          ('OTP_LOGIN',            'SMS',      'OTP Login (SMS)',
           '{{otp}} is your WayTero login OTP. Valid for 5 minutes. Do not share this OTP with anyone.', TRUE),
          ('PROMOTIONAL_CAMPAIGN', 'WHATSAPP', 'Promotional Campaign (WhatsApp)',
           'Hi {{customer_name}}! Exclusive offer just for you on WayTero. Book now and save big on your next trip!', TRUE)
        ON CONFLICT (template_code, channel) DO NOTHING
    """))


def downgrade() -> None:
    conn = op.get_bind()
    for tbl in [
        "push_notifications", "email_queue", "whatsapp_queue", "sms_queue",
        "notification_templates", "notifications",
    ]:
        conn.execute(sa.text(f"DROP TABLE IF EXISTS {tbl} CASCADE"))
