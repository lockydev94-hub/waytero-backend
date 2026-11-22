# ============================================================
# WAYTERO — PLACES: LANDMARK KIND
# File: alembic/versions/0110_places_landmark_kind.py
#
# Places started as transport nodes only (airport, railway station) because
# those are where a cab trip begins. Every place row also provisions a
# PLACE_CABS landing page, so restricting the registry to transport nodes
# capped the number of local pages a city could have at two. LANDMARK widens
# the registry to temples, zoos, caves and monuments, each of which is a real
# search entry point ("cab to Nandankanan") and a real pickup point.
#
# Doc Ref: Place-Level Landing Pages §Phase 1
#          Migration 0070_places
# ============================================================
from alembic import op
import sqlalchemy as sa

revision = "0110"
down_revision = "0109"

_KINDS = "ARRAY['AIRPORT'::character varying, 'RAILWAY_STATION'::character varying, 'LANDMARK'::character varying]::text[]"


def upgrade() -> None:
    conn = op.get_bind()
    # Drop-then-add keeps this idempotent: the constraint may already carry
    # LANDMARK if the migration is re-run after a partial failure.
    conn.execute(
        sa.text("ALTER TABLE places DROP CONSTRAINT IF EXISTS chk_places_kind")
    )
    conn.execute(
        sa.text(
            "ALTER TABLE places ADD CONSTRAINT chk_places_kind "
            f"CHECK (kind::text = ANY ({_KINDS}))"
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    # Landmark rows have no meaning to the old constraint, so they must go
    # before it can be restored. Their landing pages cascade with them.
    conn.execute(sa.text("DELETE FROM places WHERE kind = 'LANDMARK'"))
    conn.execute(
        sa.text("ALTER TABLE places DROP CONSTRAINT IF EXISTS chk_places_kind")
    )
    conn.execute(
        sa.text(
            "ALTER TABLE places ADD CONSTRAINT chk_places_kind "
            "CHECK (kind::text = ANY ("
            "ARRAY['AIRPORT'::character varying, "
            "'RAILWAY_STATION'::character varying]::text[]))"
        )
    )
