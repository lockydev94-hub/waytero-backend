"""Live chat — conversations + messages (customer ↔ support/admin)

Revision ID: 0054
Revises: 0053

Smart live chat between website customers and admin support staff.

  - ``chat_conversations`` — one row per support thread. The thread is
    keyed by either a logged-in customer (``customer_user_id``) or a
    guest identity (``guest_key`` client-generated UUID + name/mobile).
    ``status`` mirrors the smart routing decision: OPEN when an admin is
    online and the thread is being handled, WAITING when the customer
    started it while everyone was offline, CLOSED when an admin ends it.
    Unread counters are split per side so the widget and the admin
    sidebar can show badges without scanning messages.
  - ``chat_messages`` — the messages themselves. ``sender_type`` is
    CUSTOMER / ADMIN / SYSTEM. ``is_read`` is flipped by the recipient
    side (admin reads thread / customer reads thread).

Routing + presence are not stored here — online support staff live in a
Redis set (``chat:online_admins``) maintained by the admin portal's
heartbeat, so "is anyone online?" is a fast cache read at chat-start.

Doc Ref: BRD Part 7 §155 (realtime channel), Website Chat §1 — Schema
"""

from alembic import op
import sqlalchemy as sa

revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    conn.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS chat_conversations (
                id                   UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
                customer_user_id     UUID         REFERENCES users(id) ON DELETE SET NULL,
                guest_key            VARCHAR(64),
                guest_name           VARCHAR(255),
                guest_email          VARCHAR(255),
                guest_mobile         VARCHAR(15),
                subject              VARCHAR(50),
                status               VARCHAR(20)  NOT NULL DEFAULT 'WAITING',
                assigned_admin_id    UUID         REFERENCES users(id) ON DELETE SET NULL,
                last_message_at      TIMESTAMPTZ,
                last_message_preview VARCHAR(300),
                unread_customer_count INTEGER     NOT NULL DEFAULT 0,
                unread_admin_count   INTEGER     NOT NULL DEFAULT 0,
                created_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),
                updated_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),
                CONSTRAINT chk_chat_conv_identity CHECK (
                    customer_user_id IS NOT NULL OR guest_key IS NOT NULL
                )
            )
            """
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_chat_conv_status ON chat_conversations (status, updated_at DESC)"
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_chat_conv_customer ON chat_conversations (customer_user_id)"
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_chat_conv_guest ON chat_conversations (guest_key)"
        )
    )

    conn.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS chat_messages (
                id               BIGSERIAL   PRIMARY KEY,
                conversation_id  UUID        NOT NULL REFERENCES chat_conversations(id) ON DELETE CASCADE,
                sender_type      VARCHAR(20) NOT NULL,
                sender_user_id   UUID        REFERENCES users(id) ON DELETE SET NULL,
                body             TEXT        NOT NULL,
                is_read          BOOLEAN     NOT NULL DEFAULT false,
                created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_chat_msg_conv ON chat_messages (conversation_id, created_at)"
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP TABLE IF EXISTS chat_messages"))
    conn.execute(sa.text("DROP TABLE IF EXISTS chat_conversations"))
