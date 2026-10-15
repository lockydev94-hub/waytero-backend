"""seed owner-approved brand master into system_configurations + CMS socials

Doc Ref: "no fake or demo data" owner rule; brand facts supplied by the
         owner 2026-10-15 (WAYTERO-PROJECT-MASTER).
Revision: 0072
DownRevision: 0071

Every contact datum the website shows must come from the DB, with the
owner-approved master only as a last-resort fallback. The previous seeds
and CMS defaults carried invented values (fake social profile URLs like
x.com/waytero, "1800-WAYTERO", a CIN that never existed). This migration
stores the real ones:

  phone 7978322649 (kept only if admin hasn't set one), email
  support@waytero.com, office "Avinue Appartment, Gothapatana,
  Bhubaneswar - 751003", tagline "Explore More. Travel Better.",
  and the three genuine social profiles (YouTube/Instagram/Facebook).

Also seeds SEO_PAGE_* keys so page <head> metadata is DB-editable, and
replaces the fabricated social_links JSON in the header/footer singletons.
All statements are guarded/idempotent — admin-edited values are never
overwritten.
"""

from alembic import op

revision = "0072"
down_revision = "0071"
depends_on = None
branch_labels = None


REAL_SOCIALS = (
    '{"youtube":"https://www.youtube.com/@WayTero",'
    '"instagram":"https://www.instagram.com/waytero.official/",'
    '"facebook":"https://www.facebook.com/WayTero"}'
)


def upgrade() -> None:
    # ── brand master keys (never clobber admin-entered values) ────────────
    op.execute(
        """
        INSERT INTO system_configurations (config_key, config_value, description, updated_at)
        VALUES
            ('OFFICE_ADDRESS',   'Avinue Appartment, Gothapatana, Bhubaneswar - 751003', 'Registered office address shown on the website', NOW()),
            ('SOCIAL_YOUTUBE',   'https://www.youtube.com/@WayTero',                      'Official YouTube channel',                    NOW()),
            ('SOCIAL_INSTAGRAM', 'https://www.instagram.com/waytero.official/',           'Official Instagram profile',                  NOW()),
            ('SOCIAL_FACEBOOK',  'https://www.facebook.com/WayTero',                      'Official Facebook page',                      NOW())
        ON CONFLICT (config_key) DO NOTHING
        """
    )
    # Fill contact fields only when they were never configured.
    op.execute(
        """
        UPDATE system_configurations
        SET config_value = 'support@waytero.com', updated_at = NOW()
        WHERE config_key = 'SUPPORT_EMAIL'
          AND (config_value IS NULL OR btrim(config_value) = '')
        """
    )
    op.execute(
        """
        UPDATE system_configurations
        SET config_value = '7978322649', updated_at = NOW()
        WHERE config_key = 'SUPPORT_PHONE'
          AND (config_value IS NULL OR btrim(config_value) = '')
          AND NOT EXISTS (
              SELECT 1 FROM site_header_config WHERE btrim(coalesce(support_phone, '')) <> ''
          )
          AND NOT EXISTS (
              SELECT 1 FROM site_footer_config WHERE btrim(coalesce(support_phone, '')) <> ''
          )
        """
    )
    # Official tagline replaces the original seed default only if untouched.
    op.execute(
        """
        UPDATE system_configurations
        SET config_value = 'Explore More. Travel Better.', updated_at = NOW()
        WHERE config_key = 'PLATFORM_TAGLINE'
          AND config_value = 'Your Way, Our Route'
        """
    )

    # ── CMS singletons: replace fabricated social sets, fill empties ──────
    for table in ("site_header_config", "site_footer_config"):
        op.execute(
            f"""
            UPDATE {table}
            SET social_links = '{REAL_SOCIALS}'::jsonb || coalesce((
                SELECT jsonb_object_agg(key, value)
                FROM jsonb_each_text(coalesce(nullif(social_links, 'null'::jsonb), '{{}}'::jsonb))
                WHERE value NOT IN (
                    'https://facebook.com/waytero',
                    'https://twitter.com/waytero',
                    'https://x.com/waytero',
                    'https://instagram.com/waytero',
                    'https://linkedin.com/waytero',
                    'https://linkedin.com/company/waytero',
                    'https://youtube.com/waytero'
                )
            ), '{{}}'::jsonb)
            """
        )
    op.execute(
        """
        UPDATE site_footer_config
        SET company_address = 'Avinue Appartment, Gothapatana, Bhubaneswar - 751003'
        WHERE company_address IS NULL OR btrim(company_address) = ''
        """
    )
    op.execute(
        """
        UPDATE site_footer_config
        SET contact_email = 'support@waytero.com'
        WHERE contact_email IS NULL OR btrim(contact_email) = ''
        """
    )

    # ── per-page SEO keys (admin-editable via system configurations) ──────
    op.execute(
        """
        INSERT INTO system_configurations (config_key, config_value, description, updated_at)
        VALUES
            ('SEO_PAGE_ABOUT',
             '{"title":"About Us — WayTero","description":"WayTero connects customers, partners, drivers and hotels on one travel platform — cabs, hotels and tour packages with transparent, GST-inclusive pricing. Built in Bhubaneswar."}',
             'Head title/description for /about', NOW()),
            ('SEO_PAGE_CAREERS',
             '{"title":"Careers — WayTero","description":"Work with WayTero in Bhubaneswar. Open roles are announced on this page — reach our team any time."}',
             'Head title/description for /careers', NOW()),
            ('SEO_PAGE_CONTACT',
             '{"title":"Contact Us — WayTero","description":"Reach WayTero support by phone, email or WhatsApp. Head office in Bhubaneswar, Odisha."}',
             'Head title/description for /contact', NOW()),
            ('SEO_PAGE_PRESS',
             '{"title":"Press & Media — WayTero","description":"Media resources and press enquiries for WayTero — cabs, hotels and tour packages in the cities we serve."}',
             'Head title/description for /press', NOW()),
            ('SEO_PAGE_PARTNER',
             '{"title":"Partner With WayTero — Cabs, Hotels & Tours","description":"List your cab fleet, hotel or tour business on WayTero and reach travellers in the cities we serve."}',
             'Head title/description for /partner', NOW()),
            ('SEO_PAGE_SUPPORT',
             '{"title":"Help & Support — WayTero","description":"Answers for WayTero cab, hotel and tour bookings, payments, wallet and refunds — plus how to reach our team."}',
             'Head title/description for /support', NOW()),
            ('SEO_PAGE_SAFETY',
             '{"title":"Travel Safety — WayTero","description":"How WayTero keeps travellers safe: verified partners, live trip tracking and payment protection."}',
             'Head title/description for /safety', NOW()),
            ('SEO_PAGE_SECURITY',
             '{"title":"Security — WayTero","description":"How WayTero protects your data — encryption in transit and at rest, secure payments and account safety."}',
             'Head title/description for /security', NOW()),
            ('SEO_PAGE_COOKIES',
             '{"title":"Cookie Policy — WayTero","description":"Which cookies WayTero stores and why."}',
             'Head title/description for /cookies', NOW()),
            ('SEO_PAGE_REFUND',
             '{"title":"Cancellation & Refund Policy — WayTero","description":"WayTero cancellation and refund terms for cabs, hotels and tour packages."}',
             'Head title/description for /refund', NOW()),
            ('SEO_PAGE_TERMS',
             '{"title":"Terms of Service — WayTero","description":"Terms for booking cabs, hotels and tour packages on WayTero."}',
             'Head title/description for /terms', NOW()),
            ('SEO_PAGE_PRIVACY',
             '{"title":"Privacy Policy — WayTero","description":"How WayTero collects, uses and protects your personal data."}',
             'Head title/description for /privacy', NOW()),
            ('SEO_PAGE_TRACK',
             '{"title":"Track Your Booking — WayTero","description":"Track your WayTero trip live — booking status and driver details by booking ID."}',
             'Head title/description for /track', NOW()),
            ('SEO_PAGE_WALLET',
             '{"title":"Travel Wallet — WayTero","description":"WayTero travel wallet — balance, transactions and refunds in one place."}',
             'Head title/description for /wallet', NOW()),
            ('SEO_PAGE_SITEMAP',
             '{"title":"Sitemap — WayTero","description":"All WayTero pages, city guides and service links."}',
             'Head title/description for /sitemap', NOW()),
            ('SEO_PAGE_BOOKING_INSTRUCTIONS',
             '{"title":"How Booking Works — WayTero","description":"Step-by-step: how to book cabs, hotels and tours on WayTero, including payments and cancellation."}',
             'Head title/description for /booking-instructions', NOW())
        ON CONFLICT (config_key) DO NOTHING
        """
    )


def downgrade() -> None:
    # ON CONFLICT preserves preexisting admin keys; rollback must preserve them too.
    pass
