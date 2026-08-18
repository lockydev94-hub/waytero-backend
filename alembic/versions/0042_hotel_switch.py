"""0042_hotel_switch — hotel switch / mid-stay split support

Revision ID: 0042_hotel_switch
Revises: 0041_breakdown_swap_support
Create Date: 2026-08-09

Doc Ref:
  BRD Part 4 §57-92 — Hotel Booking Management
  Spec: Hotel Switch / Mid-Stay Relocation

Why:
  A confirmed customer sometimes wants to change hotels — either before
  check-in ("cancel X, book Y instead") or after partial stay ("stayed one
  night at X, want to leave for Y for the rest"). Today there is no way to
  express this in the data model: hotel_reservations has no link back to a
  "predecessor" row, no flag for split stays, and no audit row recording
  what was transferred / refunded.

What:
  1. Five columns on hotel_reservations for the split-stay lineage:
       is_split_stay, original_reservation_id, switched_at,
       switched_by_user_id, switched_reason, split_advance_strategy
  2. New table hotel_reservation_split_events — append-only audit of every
     switch / split with the money moves (refund, advance redistributed,
     original/nights consumed, new total). One UNIQUE constraint on the
     (original, new) pair guarantees we never double-record a switch.

Notes:
  All writes are idempotent (ADD COLUMN IF NOT EXISTS, CREATE TABLE IF NOT
  EXISTS, CREATE INDEX IF NOT EXISTS). Partial indexes target only the rows
  the feature touches; the index doesn't grow when the flag is off.
"""

import sqlalchemy as sa
from alembic import op


revision = "0042_hotel_switch"
down_revision = "0041_breakdown_swap_support"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # ════════════════════════════════════════════════════════════════
    # 1. Split-stay lineage columns on hotel_reservations
    #    is_split_stay is set on BOTH halves of a split (original + new).
    #    original_reservation_id points from the new row back to the
    #    original; on the original row itself the column stays NULL.
    #    switched_reason is informational — used for filtering / reports.
    #    split_advance_strategy captures which advance-redistribution rule
    #    applied (currently only ROLLOVER is shipped).
    # ════════════════════════════════════════════════════════════════
    conn.execute(
        sa.text(
            """
            ALTER TABLE hotel_reservations
                ADD COLUMN IF NOT EXISTS is_split_stay           BOOLEAN NOT NULL DEFAULT FALSE,
                ADD COLUMN IF NOT EXISTS original_reservation_id BIGINT
                    REFERENCES hotel_reservations(id),
                ADD COLUMN IF NOT EXISTS switched_at             TIMESTAMPTZ,
                ADD COLUMN IF NOT EXISTS switched_by_user_id     UUID REFERENCES users(id),
                ADD COLUMN IF NOT EXISTS switched_reason         VARCHAR(50),
                ADD COLUMN IF NOT EXISTS split_advance_strategy  VARCHAR(20)
        """
        )
    )

    # Partial index — only rows with the flag on, so the index stays small.
    conn.execute(
        sa.text(
            """
            CREATE INDEX IF NOT EXISTS idx_hotel_res_split_stay
                ON hotel_reservations (is_split_stay)
                WHERE is_split_stay = TRUE
        """
        )
    )
    conn.execute(
        sa.text(
            """
            CREATE INDEX IF NOT EXISTS idx_hotel_res_original_link
                ON hotel_reservations (original_reservation_id)
                WHERE original_reservation_id IS NOT NULL
        """
        )
    )

    # ════════════════════════════════════════════════════════════════
    # 2. hotel_reservation_split_events — append-only audit
    #    Records every money-move: refund issued, advance redistributed,
    #    original's final amount (now nights-consumed only), new total,
    #    and the nights that crossed the boundary.
    #    UNIQUE (original, new) guarantees idempotent re-runs of a switch
    #    endpoint produce exactly one event row.
    # ════════════════════════════════════════════════════════════════
    conn.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS hotel_reservation_split_events (
                id                       BIGSERIAL PRIMARY KEY,
                original_reservation_id  BIGINT NOT NULL REFERENCES hotel_reservations(id),
                new_reservation_id       BIGINT NOT NULL REFERENCES hotel_reservations(id),

                split_type               VARCHAR(20) NOT NULL,  -- PRE_CHECKIN_SWITCH | POST_CHECKIN_SPLIT
                nights_transferred       INTEGER NOT NULL,
                original_nights_consumed INTEGER NOT NULL,

                original_final_amount    NUMERIC(14,2) NOT NULL DEFAULT 0,
                new_total_amount         NUMERIC(14,2) NOT NULL DEFAULT 0,
                refund_issued            NUMERIC(14,2) NOT NULL DEFAULT 0,
                advance_redistributed    NUMERIC(14,2) NOT NULL DEFAULT 0,
                advance_split_strategy   VARCHAR(20) NOT NULL,  -- ROLLOVER | NONE

                notes                    TEXT,
                created_by_user_id       UUID REFERENCES users(id),
                created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),

                CONSTRAINT uq_split_event UNIQUE (original_reservation_id, new_reservation_id)
            )
        """
        )
    )

    conn.execute(
        sa.text(
            """
            CREATE INDEX IF NOT EXISTS idx_split_events_original
                ON hotel_reservation_split_events (original_reservation_id)
        """
        )
    )
    conn.execute(
        sa.text(
            """
            CREATE INDEX IF NOT EXISTS idx_split_events_new
                ON hotel_reservation_split_events (new_reservation_id)
        """
        )
    )
    conn.execute(
        sa.text(
            """
            CREATE INDEX IF NOT EXISTS idx_split_events_created
                ON hotel_reservation_split_events (created_at DESC)
        """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()

    conn.execute(
        sa.text("DROP INDEX IF EXISTS idx_split_events_created")
    )
    conn.execute(
        sa.text("DROP INDEX IF EXISTS idx_split_events_new")
    )
    conn.execute(
        sa.text("DROP INDEX IF EXISTS idx_split_events_original")
    )
    conn.execute(sa.text("DROP TABLE IF EXISTS hotel_reservation_split_events"))

    conn.execute(
        sa.text("DROP INDEX IF EXISTS idx_hotel_res_original_link")
    )
    conn.execute(
        sa.text("DROP INDEX IF EXISTS idx_hotel_res_split_stay")
    )
    conn.execute(
        sa.text(
            """
            ALTER TABLE hotel_reservations
                DROP COLUMN IF EXISTS split_advance_strategy,
                DROP COLUMN IF EXISTS switched_reason,
                DROP COLUMN IF EXISTS switched_by_user_id,
                DROP COLUMN IF EXISTS switched_at,
                DROP COLUMN IF EXISTS original_reservation_id,
                DROP COLUMN IF EXISTS is_split_stay
        """
        )
    )
