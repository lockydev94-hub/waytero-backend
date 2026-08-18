"""0046_city_coordinates — add latitude/longitude to cities

Revision ID: 0046_city_coordinates
Revises: 0045_section_service_type
Create Date: 2026-08-11

Doc Ref:
  Customer cab search filters Google Places predictions to only places
  inside an active city. Without lat/lng on cities we cannot bias the
  Places API call (`location` + `radius` params) and we cannot compute
  nearest-city on the backend.

  This migration:
    • adds nullable latitude / longitude (NUMERIC(10,7)) to cities
    • seeds known coordinates for the 15 active cities (so customer-web
      works on the day-of-deployment without manual data entry)
    • is idempotent — ADD COLUMN IF NOT EXISTS + ON CONFLICT-safe UPDATE
"""

import sqlalchemy as sa
from alembic import op

revision = "0046_city_coordinates"
down_revision = "0045_section_service_type"


# (city_code → (lat, lng)). Approximate city-centre coordinates. These are
# used to (a) bias Google Places autocomplete to that city and (b) match
# user-selected places back to our cities when admin hasn't pinned a
# custom coordinate. Precision is 7 decimal places (~1cm) — overkill but
# matches the column type.
_CITY_COORDS: dict[str, tuple[float, float]] = {
    "AMD": (23.0225050, 72.5713621),  # Ahmedabad
    "BLR": (12.9715987, 77.5945627),  # Bengaluru
    "BRH": (19.3149618, 84.7940881),  # Berhampur
    "BBS": (20.2960587, 85.8245398),  # Bhubaneswar
    "CHN": (13.0826802, 80.2707184),  # Chennai
    "CTK": (20.4625210, 85.8829895),  # Cuttack
    "HYD": (17.3850440, 78.4866710),  # Hyderabad
    "JAI": (26.9124336, 75.7872709),  # Jaipur
    "KOL": (22.5726460, 88.3638950),  # Kolkata
    "LKO": (26.8466937, 80.9461660),  # Lucknow
    "MUM": (19.0759837, 72.8776559),  # Mumbai
    "DEL": (28.6139391, 77.2090212),  # New Delhi
    "PUN": (18.5204303, 73.8567437),  # Pune
    "PRI": (19.8133822, 85.8314656),  # Puri
    "RKL": (22.2604230, 84.8535840),  # Rourkela
}


def upgrade() -> None:
    # 1. Add columns (idempotent — re-running is safe).
    op.execute(
        sa.text("ALTER TABLE cities ADD COLUMN IF NOT EXISTS latitude  NUMERIC(10,7)")
    )
    op.execute(
        sa.text("ALTER TABLE cities ADD COLUMN IF NOT EXISTS longitude NUMERIC(10,7)")
    )

    # 2. Seed known coordinates. The city_code is the join key so we don't
    #    accidentally remap a city whose name was edited.
    for code, (lat, lng) in _CITY_COORDS.items():
        op.execute(
            sa.text(
                """
                UPDATE cities
                SET latitude = :lat, longitude = :lng
                WHERE city_code = :code
                  AND (latitude IS NULL OR longitude IS NULL)
            """
            ).bindparams(code=code, lat=lat, lng=lng)
        )


def downgrade() -> None:
    op.execute(sa.text("ALTER TABLE cities DROP COLUMN IF EXISTS longitude"))
    op.execute(sa.text("ALTER TABLE cities DROP COLUMN IF EXISTS latitude"))
