"""0089_services_card_copy — put the redesigned card copy on the SERVICES band

Revision ID: 0089
Revises: 0088

Doc Ref: migration 0055_homepage_sections (the seed this band comes from);
BRD Part 6 §155 (homepage admin control).

Why:
  The SERVICES band was redesigned from four small icon cards to two
  full-bleed photo cards (see ServicesSection.tsx). The card layout is
  unchanged in what it reads — `items[].title`, `desc`, `link`, `tone` — but
  the seeded descriptions were written for the old icon tiles ("Cab and
  vehicle ride bookings powered by vehicle categories") and read as filler
  under a 26px headline.

  Only the two rows that actually render are touched here. The hotel card is
  hidden site-wide (the hotel vertical is off) so its copy is left alone for
  whenever it comes back.

  Guarded on the exact seeded text, so a re-run matches nothing (idempotent)
  and an admin who has since written real copy is left alone. No new keys are
  introduced, so the CMS editor still round-trips these rows unchanged.

downgrade() puts the seeded copy back, guarded the same way.
"""

import sqlalchemy as sa
from alembic import op

revision = "0089"
down_revision = "0088"
branch_labels = None
depends_on = None

COPY = {
    "CAB": {
        "old_desc": "Cab and vehicle ride bookings powered by vehicle categories",
        "new_desc": "Local, airport, one-way, outstation and round-trip cabs at best prices.",
    },
    "TOUR": {
        "old_desc": "Tour packages and guided trip bookings",
        "new_desc": "Handpicked tour packages, scenic trips and complete travel experiences.",
    },
}

_REWRITE = """
    UPDATE section_variants sv
    SET content = jsonb_set(
            sv.content,
            '{items}',
            (
                SELECT jsonb_agg(
                    CASE
                        WHEN elem->>'link' = :cab_link
                             AND elem->>'desc' = :cab_old
                            THEN jsonb_set(elem, '{desc}', to_jsonb(CAST(:cab_new AS text)))
                        WHEN elem->>'link' = :tour_link
                             AND (elem->>'desc' = :tour_old OR elem->>'title' = :tour_old_title)
                            THEN jsonb_set(
                                    jsonb_set(elem, '{desc}', to_jsonb(CAST(:tour_new AS text))),
                                    '{title}', to_jsonb(CAST(:tour_title AS text))
                                 )
                        ELSE elem
                    END
                )
                FROM jsonb_array_elements(sv.content->'items') AS elem
            )
        ),
        updated_at = NOW()
    FROM page_sections ps
    WHERE sv.page_section_id = ps.id
      AND ps.section_key = 'SERVICES'
      AND jsonb_typeof(sv.content->'items') = 'array'
      AND jsonb_array_length(sv.content->'items') > 0
      AND (
            sv.content->'items' @> jsonb_build_array(
                jsonb_build_object('link', CAST(:cab_link AS text),
                                   'desc', CAST(:cab_old  AS text))
            )
         OR sv.content->'items' @> jsonb_build_array(
                jsonb_build_object('link', CAST(:tour_link AS text),
                                   'desc', CAST(:tour_old  AS text))
            )
         OR sv.content->'items' @> jsonb_build_array(
                jsonb_build_object('link',  CAST(:tour_link AS text),
                                   'title', CAST(:tour_old_title AS text))
            )
      )
"""

# The redirect target the tour card used to be seeded with is the title the
# old tiles showed; the new badge reads "Tour Packages".
_TOUR_OLD_TITLE = "Tour Package"
_TOUR_TITLE = "Tour Packages"


def _params(forward: bool) -> dict:
    cab, tour = COPY["CAB"], COPY["TOUR"]
    return {
        "cab_link": "/cabs",
        "tour_link": "/tours",
        "cab_old": cab["old_desc"] if forward else cab["new_desc"],
        "cab_new": cab["new_desc"] if forward else cab["old_desc"],
        "tour_old": tour["old_desc"] if forward else tour["new_desc"],
        "tour_new": tour["new_desc"] if forward else tour["old_desc"],
        "tour_old_title": _TOUR_OLD_TITLE if forward else _TOUR_TITLE,
        "tour_title": _TOUR_TITLE if forward else _TOUR_OLD_TITLE,
    }


def upgrade() -> None:
    op.get_bind().execute(sa.text(_REWRITE), _params(forward=True))


def downgrade() -> None:
    op.get_bind().execute(sa.text(_REWRITE), _params(forward=False))
