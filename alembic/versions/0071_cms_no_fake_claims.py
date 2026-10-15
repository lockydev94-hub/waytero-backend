"""strip unverifiable claims from seeded homepage CMS copy

Doc Ref: Hard Service Availability — "no fake or demo data" (owner rule,
         plan: rosy-vent-shrew)
Revision: 0071
DownRevision: 0070

The 0055 homepage seed shipped marketing numbers the database has never
backed: fabricated promos ("Flat ₹300 Off…", "1,200+ partner hotels"),
invented stat chips ("50,000+ Happy Travelers"), a "30+ cities" step
description, and "Join 50,000+ travellers" CTAs. The website's policy is
now: anything the DB cannot verify must not appear — an empty list hides
its section entirely (see OffersSection/StatsSection/TestimonialsSection
in customer-web, which render nothing when the CMS content is empty).

This migration empties the fabricated lists and rewrites the numeric copy
with verifiable statements. Every UPDATE is guarded on the exact seeded
marker text, so admin-edited content is never touched and the script is
re-runnable. Downgrade restores the original seed strings.
"""

from alembic import op

revision = "0071"
down_revision = "0070"
branch_labels = None
depends_on = None


def _exec(sql: str) -> None:
    op.execute(sql)


def upgrade() -> None:
    # OFFERS: drop the invented promos; the section hides when empty.
    _exec(
        """
        UPDATE section_variants v
           SET content = jsonb_set(v.content, '{offers}', '[]'::jsonb)
          FROM page_sections s
         WHERE s.id = v.page_section_id
           AND s.section_key = 'OFFERS'
           AND v.content::text LIKE '%Flat ₹300 Off on Airport Rides%';
        """
    )

    # STATS: remove the made-up counters.
    _exec(
        """
        UPDATE section_variants v
           SET content = jsonb_set(v.content, '{stats}', '[]'::jsonb)
          FROM page_sections s
         WHERE s.id = v.page_section_id
           AND s.section_key = 'STATS'
           AND v.content::text LIKE '%Happy Travelers%';
        """
    )

    # HOW_IT_WORKS: keep the procedural steps, drop the "30+ cities" claim.
    _exec(
        """
        UPDATE section_variants v
           SET content = jsonb_set(
                 v.content,
                 '{steps,0,description}',
                 '"Browse cabs, hotels and tour packages and compare the best options in one place."'::jsonb
               )
          FROM page_sections s
         WHERE s.id = v.page_section_id
           AND s.section_key = 'HOW_IT_WORKS'
           AND v.content::text LIKE '%across 30+ cities%';
        """
    )

    # CTA: "Join 50,000+ Indian travelers" → what the platform can prove.
    _exec(
        """
        UPDATE section_variants
           SET subheadline = 'Book cabs, hotels and tours with upfront, GST-inclusive pricing.'
         WHERE subheadline LIKE '%50,000+ Indian travelers%';
        """
    )

    # TESTIMONIALS: the invented 4.8/5 aggregate; section hides (no items).
    _exec(
        """
        UPDATE section_variants
           SET subheadline = NULL
         WHERE subheadline LIKE '%50,000+ verified reviews%';
        """
    )


def downgrade() -> None:
    _exec(
        """
        UPDATE section_variants v
           SET content = jsonb_set(
                 v.content,
                 '{steps,0,description}',
                 '"Browse cabs, hotels and tour packages across 30+ cities and compare the best options in one place."'::jsonb
               )
          FROM page_sections s
         WHERE s.id = v.page_section_id
           AND s.section_key = 'HOW_IT_WORKS'
           AND v.content::text LIKE '%compare the best options in one place%'
           AND v.content::text NOT LIKE '%30+ cities%';
        """
    )
    _exec(
        """
        UPDATE section_variants
           SET subheadline = 'Join 50,000+ Indian travelers who book smarter with WayTero.'
         WHERE subheadline = 'Book cabs, hotels and tours with upfront, GST-inclusive pricing.';
        """
    )
    _exec(
        """
        UPDATE section_variants
           SET subheadline = '4.8 / 5 average rating across 50,000+ verified reviews.'
         WHERE subheadline IS NULL
           AND page_section_id IN (SELECT id FROM page_sections WHERE section_key = 'TESTIMONIALS');
        """
    )
    # The emptied offers/stats lists were fabricated; downgrade deliberately
    # does NOT re-invent them — re-run 0055 if the seed copy is wanted back.
