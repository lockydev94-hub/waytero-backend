"""Hotel module — properties, rooms, inventory, pricing, verification, reservations

Revision ID: 0031_hotel_module
Revises: 0030_advance_refunded_amount
Create Date: 2026-08-05

Doc Ref:
  BRD Part 4 §57-92   — Hotel Booking Management
  SRS Part 5 §153-194 — Hotel Inventory & Reservation Engine
  DB Schema Part 5    — Hotel Management
  API Doc 09_HOTEL_API
  Docs/21_Hotel_Module_Implementation/01_DATABASE_AND_MIGRATION.md

Why:
  The platform was pre-wired for HOTEL everywhere except the hotel entity
  itself: service_types has a HOTEL row (0019), partner_services accepts
  service_type='HOTEL', commission_rules accepts service_type='HOTEL',
  booking_services can point at a hotel sub-booking, and 0012 seeded
  HOTEL_CONFIRMED / HOTEL_CHECKIN_REMINDER templates. This migration adds the
  missing supply side so hotels can be onboarded, priced and verified.

Changes:
  1.  hotel_categories                 — master (BUDGET…APARTMENT), seeded
  2.  hotel_amenities                  — master amenity list, seeded
  3.  hotels                           — the property, status DRAFT→…→ACTIVE
  4.  hotel_amenity_mappings
  5.  hotel_images                     — partial unique index on is_primary
  6.  hotel_documents
  7.  hotel_policies                   — check-in/out + cancellation ladder
  8.  hotel_commission_configs         — per-hotel override, supports HYBRID
  9.  hotel_gst_slabs                  — tariff-based GST slabs, seeded
  10. hotel_room_categories            — the sellable unit
  11. hotel_room_category_amenities
  12. hotel_room_category_images
  13. hotel_room_rate_plans            — weekend / seasonal / festival
  14. hotel_rooms                      — physical rooms (optional)
  15. hotel_inventory                  — per-category per-DATE counts
  16. hotel_verification_assignments   — officer assignment
  17. hotel_verification_logs          — immutable audit trail
  18. hotel_ratings
  19. hotel_performance_summary
  20. hotel_reservations               — schema only, no service this phase
  21. hotel_reservation_guests
  22. hotel_checkins
  23. hotel_checkouts
  24. hotel_code_seq                   — race-safe HTL000001 generation
  25. Seeds: 9 system_configurations keys, 6 onboarding notification templates

Notes:
  Reservation tables (20-23) are created so the schema is whole and
  booking_services.service_reference_id has a real target. No reservation
  service or endpoint is built in this phase.

  hotel_inventory.available_rooms carries a CHECK (>= 0) constraint — that
  DB-level guard, not an application check, is what actually satisfies
  SRS Rule 64 under concurrency.
"""

import sqlalchemy as sa
from alembic import op

revision = "0031_hotel_module"
down_revision = "0030_advance_refunded_amount"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # ════════════════════════════════════════════════════════════════
    # 1. hotel_categories — master. Doc Ref: SRS Part 5 §157
    #    Shape mirrors vehicle_categories post-0018 so the admin
    #    Master Data editor can be reused.
    # ════════════════════════════════════════════════════════════════
    conn.execute(
        sa.text(
            """
        CREATE TABLE IF NOT EXISTS hotel_categories (
            id              BIGSERIAL PRIMARY KEY,
            category_code   VARCHAR(50)  NOT NULL UNIQUE,
            label           VARCHAR(150) NOT NULL,
            description     TEXT,
            image_url       TEXT,
            icon_url        TEXT,
            seo_title       VARCHAR(120),
            seo_description VARCHAR(320),
            seo_keywords    VARCHAR(500),
            display_order   INTEGER NOT NULL DEFAULT 0,
            is_active       BOOLEAN NOT NULL DEFAULT TRUE,
            created_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """
        )
    )
    conn.execute(
        sa.text(
            """
        CREATE INDEX IF NOT EXISTS idx_hotel_categories_code
            ON hotel_categories (category_code)
    """
        )
    )

    # Union of SRS §157 and BRD §59 category lists
    conn.execute(
        sa.text(
            """
        INSERT INTO hotel_categories (category_code, label, description, display_order, is_active, created_at)
        VALUES
            ('BUDGET',            'Budget Hotel',       'Economy accommodation with essential amenities',      0, TRUE, NOW()),
            ('STANDARD',          'Standard Hotel',     'Mid-range accommodation with standard facilities',    1, TRUE, NOW()),
            ('PREMIUM',           'Premium Hotel',      'Upscale accommodation with premium facilities',       2, TRUE, NOW()),
            ('LUXURY',            'Luxury Hotel',       'Luxury accommodation with full-service amenities',    3, TRUE, NOW()),
            ('RESORT',            'Resort',             'Leisure resort property with recreation facilities',  4, TRUE, NOW()),
            ('HOMESTAY',          'Homestay',           'Host-managed home accommodation',                     5, TRUE, NOW()),
            ('GUEST_HOUSE',       'Guest House',        'Small guest accommodation property',                  6, TRUE, NOW()),
            ('VILLA',             'Villa',              'Private villa with independent living space',          7, TRUE, NOW()),
            ('APARTMENT',         'Apartment',          'Self-contained apartment accommodation',              8, TRUE, NOW()),
            ('SERVICE_APARTMENT', 'Service Apartment',  'Serviced apartment with housekeeping',                9, TRUE, NOW())
        ON CONFLICT (category_code) DO NOTHING
    """
        )
    )

    # ════════════════════════════════════════════════════════════════
    # 2. hotel_amenities — master. Doc Ref: DB Part 5 §10, BRD §64
    #    icon_name stores a MUI icon key, not a URL: amenity icons are
    #    UI furniture, not content, so Cloudinary assets would be waste.
    # ════════════════════════════════════════════════════════════════
    conn.execute(
        sa.text(
            """
        CREATE TABLE IF NOT EXISTS hotel_amenities (
            id            BIGSERIAL PRIMARY KEY,
            amenity_code  VARCHAR(50)  NOT NULL UNIQUE,
            amenity_name  VARCHAR(100) NOT NULL,
            icon_name     VARCHAR(100),
            amenity_group VARCHAR(50),
            display_order INTEGER NOT NULL DEFAULT 0,
            is_active     BOOLEAN NOT NULL DEFAULT TRUE,
            created_at    TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """
        )
    )

    conn.execute(
        sa.text(
            """
        INSERT INTO hotel_amenities (amenity_code, amenity_name, icon_name, amenity_group, display_order, is_active, created_at)
        VALUES
            ('WIFI',              'Free WiFi',            'Wifi',              'PROPERTY',      0,  TRUE, NOW()),
            ('AC',                'Air Conditioning',     'AcUnit',            'ROOM',          1,  TRUE, NOW()),
            ('TV',                'Television',           'Tv',                'ROOM',          2,  TRUE, NOW()),
            ('PARKING',           'Parking',              'LocalParking',      'PROPERTY',      3,  TRUE, NOW()),
            ('POOL',              'Swimming Pool',        'Pool',              'PROPERTY',      4,  TRUE, NOW()),
            ('GYM',               'Fitness Centre',       'FitnessCenter',     'PROPERTY',      5,  TRUE, NOW()),
            ('SPA',               'Spa',                  'Spa',               'PROPERTY',      6,  TRUE, NOW()),
            ('RESTAURANT',        'Restaurant',           'Restaurant',        'FOOD',          7,  TRUE, NOW()),
            ('ROOM_SERVICE',      'Room Service',         'RoomService',       'FOOD',          8,  TRUE, NOW()),
            ('BREAKFAST',         'Breakfast Included',   'FreeBreakfast',     'FOOD',          9,  TRUE, NOW()),
            ('BAR',               'Bar / Lounge',         'LocalBar',          'FOOD',          10, TRUE, NOW()),
            ('LIFT',              'Elevator',             'Elevator',          'PROPERTY',      11, TRUE, NOW()),
            ('POWER_BACKUP',      'Power Backup',         'Power',             'PROPERTY',      12, TRUE, NOW()),
            ('LAUNDRY',           'Laundry Service',      'LocalLaundryService','PROPERTY',     13, TRUE, NOW()),
            ('CCTV',              'CCTV Surveillance',    'Videocam',          'PROPERTY',      14, TRUE, NOW()),
            ('RECEPTION_24H',     '24-Hour Reception',    'SupportAgent',      'PROPERTY',      15, TRUE, NOW()),
            ('HOT_WATER',         'Hot Water',            'Shower',            'BATHROOM',      16, TRUE, NOW()),
            ('TOILETRIES',        'Toiletries',           'Soap',              'BATHROOM',      17, TRUE, NOW()),
            ('HAIRDRYER',         'Hair Dryer',           'Dry',               'BATHROOM',      18, TRUE, NOW()),
            ('MINI_BAR',          'Mini Bar',             'Kitchen',           'ROOM',          19, TRUE, NOW()),
            ('SAFE',              'In-Room Safe',         'Lock',              'ROOM',          20, TRUE, NOW()),
            ('KITCHENETTE',       'Kitchenette',          'Countertops',       'ROOM',          21, TRUE, NOW()),
            ('BALCONY',           'Balcony',              'Balcony',           'ROOM',          22, TRUE, NOW()),
            ('WHEELCHAIR_ACCESS', 'Wheelchair Access',    'Accessible',        'ACCESSIBILITY', 23, TRUE, NOW()),
            ('PET_FRIENDLY',      'Pet Friendly',         'Pets',              'PROPERTY',      24, TRUE, NOW()),
            ('CONFERENCE_HALL',   'Conference Hall',      'MeetingRoom',       'PROPERTY',      25, TRUE, NOW()),
            ('GARDEN',            'Garden',               'Yard',              'PROPERTY',      26, TRUE, NOW()),
            ('BEACH_ACCESS',      'Beach Access',         'BeachAccess',       'PROPERTY',      27, TRUE, NOW()),
            ('AIRPORT_SHUTTLE',   'Airport Shuttle',      'AirportShuttle',    'PROPERTY',      28, TRUE, NOW()),
            ('DOCTOR_ON_CALL',    'Doctor On Call',       'MedicalServices',   'PROPERTY',      29, TRUE, NOW()),
            ('FIRE_SAFETY',       'Fire Safety',          'LocalFireDepartment','PROPERTY',     30, TRUE, NOW())
        ON CONFLICT (amenity_code) DO NOTHING
    """
        )
    )

    # ════════════════════════════════════════════════════════════════
    # 24. hotel_code_seq — race-safe HTL000001 generation.
    #     MAX(id)+1 outside a lock collides under concurrent admins,
    #     and hotel_code is UNIQUE, so the loser gets an opaque error.
    # ════════════════════════════════════════════════════════════════
    conn.execute(
        sa.text(
            "CREATE SEQUENCE IF NOT EXISTS hotel_code_seq START WITH 1 INCREMENT BY 1"
        )
    )

    # ════════════════════════════════════════════════════════════════
    # 3. hotels — Doc Ref: DB Part 5 §2, SRS §155
    #    status is VARCHAR(50) not a DB enum, matching partners.status /
    #    vehicles.status — keeps status changes out of migrations.
    # ════════════════════════════════════════════════════════════════
    conn.execute(
        sa.text(
            """
        CREATE TABLE IF NOT EXISTS hotels (
            id                   BIGSERIAL PRIMARY KEY,
            uuid                 UUID NOT NULL UNIQUE,
            partner_id           BIGINT NOT NULL REFERENCES partners(id),
            hotel_code           VARCHAR(50) NOT NULL UNIQUE,

            hotel_name           VARCHAR(255) NOT NULL,
            hotel_type           VARCHAR(100),
            hotel_category_id    BIGINT REFERENCES hotel_categories(id),
            star_rating          INTEGER,
            description          TEXT,
            short_description    VARCHAR(500),

            gst_number           VARCHAR(20),
            pan_number           VARCHAR(20),

            city_id              BIGINT NOT NULL REFERENCES cities(id),
            state_id             BIGINT REFERENCES states(id),
            address              TEXT,
            address_line_2       VARCHAR(255),
            landmark             VARCHAR(255),
            postal_code          VARCHAR(20),
            latitude             NUMERIC(10,7),
            longitude            NUMERIC(10,7),

            contact_person       VARCHAR(255),
            contact_number       VARCHAR(15),
            alternate_number     VARCHAR(15),
            email                VARCHAR(255),
            website_url          TEXT,

            total_rooms          INTEGER NOT NULL DEFAULT 0,
            confirmation_mode    VARCHAR(30) NOT NULL DEFAULT 'MANUAL_CONFIRMATION',
            room_allocation_mode VARCHAR(30) NOT NULL DEFAULT 'AT_CHECK_IN',

            tax_mode             VARCHAR(20) NOT NULL DEFAULT 'EXCLUSIVE',
            is_gst_registered    BOOLEAN NOT NULL DEFAULT FALSE,

            slug                 VARCHAR(255) UNIQUE,
            seo_title            VARCHAR(120),
            seo_description      VARCHAR(320),
            seo_keywords         VARCHAR(500),
            is_featured          BOOLEAN NOT NULL DEFAULT FALSE,
            display_order        INTEGER NOT NULL DEFAULT 0,

            status               VARCHAR(50) NOT NULL DEFAULT 'DRAFT',
            rejection_reason     TEXT,
            submitted_at         TIMESTAMP WITH TIME ZONE,
            approved_at          TIMESTAMP WITH TIME ZONE,
            approved_by          UUID REFERENCES users(id),
            activated_at         TIMESTAMP WITH TIME ZONE,
            is_own_risk_approved BOOLEAN NOT NULL DEFAULT FALSE,

            created_by           UUID REFERENCES users(id),
            created_at           TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            updated_at           TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            deleted_at           TIMESTAMP WITH TIME ZONE
        )
    """
        )
    )
    for stmt in (
        "CREATE INDEX IF NOT EXISTS idx_hotel_partner  ON hotels (partner_id)",
        "CREATE INDEX IF NOT EXISTS idx_hotel_city     ON hotels (city_id)",
        "CREATE INDEX IF NOT EXISTS idx_hotel_status   ON hotels (status)",
        "CREATE INDEX IF NOT EXISTS idx_hotel_category ON hotels (hotel_category_id)",
        "CREATE INDEX IF NOT EXISTS idx_hotel_slug     ON hotels (slug)",
    ):
        conn.execute(sa.text(stmt))

    # ════════════════════════════════════════════════════════════════
    # 4. hotel_amenity_mappings — Doc Ref: DB Part 5 §11
    # ════════════════════════════════════════════════════════════════
    conn.execute(
        sa.text(
            """
        CREATE TABLE IF NOT EXISTS hotel_amenity_mappings (
            id         BIGSERIAL PRIMARY KEY,
            hotel_id   BIGINT NOT NULL REFERENCES hotels(id) ON DELETE CASCADE,
            amenity_id BIGINT NOT NULL REFERENCES hotel_amenities(id),
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_hotel_amenity UNIQUE (hotel_id, amenity_id)
        )
    """
        )
    )

    # ════════════════════════════════════════════════════════════════
    # 5. hotel_images — Doc Ref: DB Part 5 §17
    #    The partial unique index holds "at most one cover image" — an
    #    invariant that silently breaks via concurrent edits, and the DB
    #    can enforce it for free.
    # ════════════════════════════════════════════════════════════════
    conn.execute(
        sa.text(
            """
        CREATE TABLE IF NOT EXISTS hotel_images (
            id            BIGSERIAL PRIMARY KEY,
            hotel_id      BIGINT NOT NULL REFERENCES hotels(id) ON DELETE CASCADE,
            image_url     TEXT NOT NULL,
            thumbnail_url TEXT,
            caption       VARCHAR(255),
            image_type    VARCHAR(50) NOT NULL DEFAULT 'GALLERY',
            display_order INTEGER NOT NULL DEFAULT 0,
            is_primary    BOOLEAN NOT NULL DEFAULT FALSE,
            created_at    TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """
        )
    )
    conn.execute(
        sa.text(
            """
        CREATE INDEX IF NOT EXISTS idx_hotel_images_hotel ON hotel_images (hotel_id)
    """
        )
    )
    conn.execute(
        sa.text(
            """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_hotel_primary_image
            ON hotel_images (hotel_id) WHERE is_primary
    """
        )
    )

    # ════════════════════════════════════════════════════════════════
    # 6. hotel_documents — Doc Ref: DB Part 5 §18, BRD §60, SRS §158
    #    Mirrors partner_documents field-for-field plus issue_date, so
    #    the officer verify endpoint is a copy of the partner one.
    # ════════════════════════════════════════════════════════════════
    conn.execute(
        sa.text(
            """
        CREATE TABLE IF NOT EXISTS hotel_documents (
            id                  BIGSERIAL PRIMARY KEY,
            hotel_id            BIGINT NOT NULL REFERENCES hotels(id) ON DELETE CASCADE,
            document_type       VARCHAR(100) NOT NULL,
            document_number     VARCHAR(100),
            file_url            TEXT NOT NULL,
            issue_date          DATE,
            expiry_date         DATE,
            verification_status VARCHAR(50) NOT NULL DEFAULT 'PENDING',
            remarks             TEXT,
            uploaded_at         TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            verified_at         TIMESTAMP WITH TIME ZONE,
            verified_by         UUID REFERENCES users(id)
        )
    """
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_hotel_doc_hotel  ON hotel_documents (hotel_id)"
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_hotel_doc_status ON hotel_documents (verification_status)"
        )
    )

    # ════════════════════════════════════════════════════════════════
    # 7. hotel_policies — Doc Ref: BRD §82 (cancellation tiers), §77
    #    Three fixed tiers rather than a child table: BRD §82 specifies
    #    exactly three plus same-day, so an arbitrary-N table would add a
    #    join and an editor for flexibility nobody asked for.
    # ════════════════════════════════════════════════════════════════
    conn.execute(
        sa.text(
            """
        CREATE TABLE IF NOT EXISTS hotel_policies (
            id                        BIGSERIAL PRIMARY KEY,
            hotel_id                  BIGINT NOT NULL UNIQUE REFERENCES hotels(id) ON DELETE CASCADE,

            check_in_time             VARCHAR(10) DEFAULT '12:00',
            check_out_time            VARCHAR(10) DEFAULT '11:00',
            early_check_in_allowed    BOOLEAN NOT NULL DEFAULT FALSE,
            late_check_out_allowed    BOOLEAN NOT NULL DEFAULT FALSE,

            cancellation_free_hours   INTEGER DEFAULT 72,
            refund_percent_tier_1     NUMERIC(5,2) DEFAULT 100.00,
            cancellation_tier_2_hours INTEGER DEFAULT 48,
            refund_percent_tier_2     NUMERIC(5,2) DEFAULT 75.00,
            cancellation_tier_3_hours INTEGER DEFAULT 24,
            refund_percent_tier_3     NUMERIC(5,2) DEFAULT 50.00,
            refund_percent_same_day   NUMERIC(5,2) DEFAULT 0.00,
            no_show_refund_percent    NUMERIC(5,2) DEFAULT 0.00,

            couples_allowed           BOOLEAN NOT NULL DEFAULT TRUE,
            unmarried_couples_allowed BOOLEAN NOT NULL DEFAULT FALSE,
            local_id_accepted         BOOLEAN NOT NULL DEFAULT TRUE,
            pets_allowed              BOOLEAN NOT NULL DEFAULT FALSE,
            smoking_allowed           BOOLEAN NOT NULL DEFAULT FALSE,
            alcohol_allowed           BOOLEAN NOT NULL DEFAULT TRUE,
            min_guest_age             INTEGER,
            extra_bed_charge          NUMERIC(12,2),
            child_free_age_limit      INTEGER DEFAULT 5,
            house_rules               TEXT,
            cancellation_policy_text  TEXT,

            created_at                TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            updated_at                TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """
        )
    )

    # ════════════════════════════════════════════════════════════════
    # 8. hotel_commission_configs — Doc Ref: BRD §87
    #    commission_rules cannot express HYBRID (one type, one value), so
    #    hotels get their own override table. Superseding deactivates the
    #    old row rather than updating it, keeping historical bookings
    #    explainable.
    # ════════════════════════════════════════════════════════════════
    conn.execute(
        sa.text(
            """
        CREATE TABLE IF NOT EXISTS hotel_commission_configs (
            id                 BIGSERIAL PRIMARY KEY,
            hotel_id           BIGINT NOT NULL REFERENCES hotels(id) ON DELETE CASCADE,

            commission_type    VARCHAR(20) NOT NULL,
            commission_percent NUMERIC(6,3) NOT NULL DEFAULT 0,
            commission_flat    NUMERIC(12,2) NOT NULL DEFAULT 0,
            min_commission     NUMERIC(12,2),
            max_commission     NUMERIC(12,2),

            applies_to         VARCHAR(20) NOT NULL DEFAULT 'PER_BOOKING',
            effective_from     DATE,
            effective_to       DATE,
            is_active          BOOLEAN NOT NULL DEFAULT TRUE,
            remarks            TEXT,
            created_by         UUID REFERENCES users(id),
            created_at         TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """
        )
    )
    conn.execute(
        sa.text(
            """
        CREATE INDEX IF NOT EXISTS idx_hotel_comm_hotel
            ON hotel_commission_configs (hotel_id, is_active)
    """
        )
    )

    # ════════════════════════════════════════════════════════════════
    # 9. hotel_gst_slabs — Doc Ref: BRD §88 ("rates shall be configurable")
    #    Slab is a function of the nightly room tariff, not of the hotel,
    #    so one property can have a 5% Standard room and an 18% Suite.
    #    Seeded, not hardcoded; effective_from-versioned so a rate change
    #    does not rewrite history.
    # ════════════════════════════════════════════════════════════════
    conn.execute(
        sa.text(
            """
        CREATE TABLE IF NOT EXISTS hotel_gst_slabs (
            id               BIGSERIAL PRIMARY KEY,
            slab_name        VARCHAR(100) NOT NULL,
            tariff_from      NUMERIC(12,2) NOT NULL,
            tariff_to        NUMERIC(12,2),
            gst_percent      NUMERIC(5,2) NOT NULL,
            has_input_credit BOOLEAN NOT NULL DEFAULT FALSE,
            hsn_code         VARCHAR(20),
            effective_from   DATE NOT NULL,
            effective_to     DATE,
            is_active        BOOLEAN NOT NULL DEFAULT TRUE,
            notes            TEXT,
            created_at       TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """
        )
    )
    conn.execute(
        sa.text(
            """
        CREATE INDEX IF NOT EXISTS idx_hotel_gst_slab_tariff
            ON hotel_gst_slabs (tariff_from, tariff_to)
    """
        )
    )
    conn.execute(
        sa.text(
            """
        INSERT INTO hotel_gst_slabs
            (slab_name, tariff_from, tariff_to, gst_percent, has_input_credit, hsn_code, effective_from, is_active, notes)
        SELECT 'Room tariff up to Rs.7,500', 0.00, 7500.00, 5.00, FALSE, '996311', CURRENT_DATE, TRUE,
               'Applies per room per night. Rate without input tax credit.'
        WHERE NOT EXISTS (SELECT 1 FROM hotel_gst_slabs WHERE slab_name = 'Room tariff up to Rs.7,500')
    """
        )
    )
    conn.execute(
        sa.text(
            """
        INSERT INTO hotel_gst_slabs
            (slab_name, tariff_from, tariff_to, gst_percent, has_input_credit, hsn_code, effective_from, is_active, notes)
        SELECT 'Room tariff above Rs.7,500', 7500.01, NULL, 18.00, TRUE, '996311', CURRENT_DATE, TRUE,
               'Applies per room per night. Rate with input tax credit.'
        WHERE NOT EXISTS (SELECT 1 FROM hotel_gst_slabs WHERE slab_name = 'Room tariff above Rs.7,500')
    """
        )
    )

    # ════════════════════════════════════════════════════════════════
    # 10. hotel_room_categories — Doc Ref: DB Part 5 §5, BRD §64, SRS §160
    #     meal_plan (EP/CP/MAP/AP) is standard Indian hotel vocabulary;
    #     without it "does the price include breakfast" becomes a support
    #     ticket per booking.
    # ════════════════════════════════════════════════════════════════
    conn.execute(
        sa.text(
            """
        CREATE TABLE IF NOT EXISTS hotel_room_categories (
            id                 BIGSERIAL PRIMARY KEY,
            hotel_id           BIGINT NOT NULL REFERENCES hotels(id) ON DELETE CASCADE,
            category_name      VARCHAR(150) NOT NULL,
            room_type          VARCHAR(100),
            description        TEXT,

            base_occupancy     INTEGER NOT NULL DEFAULT 2,
            max_adults         INTEGER NOT NULL DEFAULT 2,
            max_children       INTEGER NOT NULL DEFAULT 1,
            max_occupancy      INTEGER NOT NULL DEFAULT 3,
            extra_bed_allowed  BOOLEAN NOT NULL DEFAULT FALSE,
            extra_bed_charge   NUMERIC(12,2) NOT NULL DEFAULT 0,
            extra_adult_charge NUMERIC(12,2) NOT NULL DEFAULT 0,
            extra_child_charge NUMERIC(12,2) NOT NULL DEFAULT 0,

            bed_type           VARCHAR(100),
            room_size_sqft     INTEGER,
            view_type          VARCHAR(100),
            floor_range        VARCHAR(50),

            base_price         NUMERIC(12,2) NOT NULL DEFAULT 0,
            published_price    NUMERIC(12,2),
            min_sellable_price NUMERIC(12,2),
            meal_plan          VARCHAR(20) NOT NULL DEFAULT 'EP',
            is_refundable      BOOLEAN NOT NULL DEFAULT TRUE,

            total_rooms        INTEGER NOT NULL DEFAULT 0,
            display_order      INTEGER NOT NULL DEFAULT 0,
            is_active          BOOLEAN NOT NULL DEFAULT TRUE,
            created_at         TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            updated_at         TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """
        )
    )
    conn.execute(
        sa.text(
            """
        CREATE INDEX IF NOT EXISTS idx_hrc_hotel ON hotel_room_categories (hotel_id, is_active)
    """
        )
    )

    # ════════════════════════════════════════════════════════════════
    # 11-12. Room category amenities & images
    # ════════════════════════════════════════════════════════════════
    conn.execute(
        sa.text(
            """
        CREATE TABLE IF NOT EXISTS hotel_room_category_amenities (
            id               BIGSERIAL PRIMARY KEY,
            room_category_id BIGINT NOT NULL REFERENCES hotel_room_categories(id) ON DELETE CASCADE,
            amenity_id       BIGINT NOT NULL REFERENCES hotel_amenities(id),
            created_at       TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_room_cat_amenity UNIQUE (room_category_id, amenity_id)
        )
    """
        )
    )
    conn.execute(
        sa.text(
            """
        CREATE TABLE IF NOT EXISTS hotel_room_category_images (
            id               BIGSERIAL PRIMARY KEY,
            room_category_id BIGINT NOT NULL REFERENCES hotel_room_categories(id) ON DELETE CASCADE,
            image_url        TEXT NOT NULL,
            caption          VARCHAR(255),
            display_order    INTEGER NOT NULL DEFAULT 0,
            is_primary       BOOLEAN NOT NULL DEFAULT FALSE,
            created_at       TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """
        )
    )
    conn.execute(
        sa.text(
            """
        CREATE INDEX IF NOT EXISTS idx_hrci_cat ON hotel_room_category_images (room_category_id)
    """
        )
    )

    # ════════════════════════════════════════════════════════════════
    # 13. hotel_room_rate_plans — Doc Ref: BRD §70
    #     Overlapping plans are ALLOWED; precedence
    #     (FESTIVAL > SEASONAL > WEEKEND > PROMOTIONAL, then priority)
    #     makes them deterministic. Forbidding overlap would make
    #     "Diwali inside the winter season" unrepresentable.
    # ════════════════════════════════════════════════════════════════
    conn.execute(
        sa.text(
            """
        CREATE TABLE IF NOT EXISTS hotel_room_rate_plans (
            id               BIGSERIAL PRIMARY KEY,
            hotel_id         BIGINT NOT NULL REFERENCES hotels(id) ON DELETE CASCADE,
            room_category_id BIGINT NOT NULL REFERENCES hotel_room_categories(id) ON DELETE CASCADE,

            plan_name        VARCHAR(150) NOT NULL,
            plan_type        VARCHAR(30) NOT NULL,
            priority         INTEGER NOT NULL DEFAULT 0,

            date_from        DATE NOT NULL,
            date_to          DATE NOT NULL,
            day_of_week_mask VARCHAR(20),

            rate_mode        VARCHAR(20) NOT NULL DEFAULT 'ABSOLUTE',
            rate_value       NUMERIC(12,2) NOT NULL,

            min_nights       INTEGER NOT NULL DEFAULT 1,
            is_active        BOOLEAN NOT NULL DEFAULT TRUE,
            created_by       UUID REFERENCES users(id),
            created_at       TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """
        )
    )
    conn.execute(
        sa.text(
            """
        CREATE INDEX IF NOT EXISTS idx_hrrp_cat_dates
            ON hotel_room_rate_plans (room_category_id, date_from, date_to) WHERE is_active
    """
        )
    )

    # ════════════════════════════════════════════════════════════════
    # 14. hotel_rooms — physical rooms. Doc Ref: DB Part 5 §7, BRD §66
    #     Optional: BRD §66 calls room numbers "recommended", so a hotel
    #     can sell 20 Deluxe rooms without ever naming them.
    # ════════════════════════════════════════════════════════════════
    conn.execute(
        sa.text(
            """
        CREATE TABLE IF NOT EXISTS hotel_rooms (
            id               BIGSERIAL PRIMARY KEY,
            hotel_id         BIGINT NOT NULL REFERENCES hotels(id) ON DELETE CASCADE,
            room_category_id BIGINT NOT NULL REFERENCES hotel_room_categories(id) ON DELETE CASCADE,
            room_number      VARCHAR(50) NOT NULL,
            floor_number     VARCHAR(20),
            room_status      VARCHAR(50) NOT NULL DEFAULT 'AVAILABLE',
            remarks          TEXT,
            is_active        BOOLEAN NOT NULL DEFAULT TRUE,
            created_at       TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_hotel_room_number UNIQUE (hotel_id, room_number)
        )
    """
        )
    )
    conn.execute(
        sa.text(
            """
        CREATE INDEX IF NOT EXISTS idx_hotel_rooms_cat ON hotel_rooms (room_category_id)
    """
        )
    )

    # ════════════════════════════════════════════════════════════════
    # 15. hotel_inventory — Doc Ref: DB Part 5 §9, SRS §161-162, §165
    #     Per room CATEGORY per DATE, not per physical room.
    #     The CHECK (available_rooms >= 0) is what actually satisfies
    #     SRS Rule 64 under concurrency — application checks race.
    # ════════════════════════════════════════════════════════════════
    conn.execute(
        sa.text(
            """
        CREATE TABLE IF NOT EXISTS hotel_inventory (
            id               BIGSERIAL PRIMARY KEY,
            hotel_id         BIGINT NOT NULL REFERENCES hotels(id) ON DELETE CASCADE,
            room_category_id BIGINT NOT NULL REFERENCES hotel_room_categories(id) ON DELETE CASCADE,
            inventory_date   DATE NOT NULL,
            total_rooms      INTEGER NOT NULL DEFAULT 0,
            booked_rooms     INTEGER NOT NULL DEFAULT 0,
            blocked_rooms    INTEGER NOT NULL DEFAULT 0,
            held_rooms       INTEGER NOT NULL DEFAULT 0,
            available_rooms  INTEGER NOT NULL DEFAULT 0,
            rate_override    NUMERIC(12,2),
            is_stop_sell     BOOLEAN NOT NULL DEFAULT FALSE,
            updated_at       TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_hotel_inv_cat_date UNIQUE (room_category_id, inventory_date),
            CONSTRAINT ck_hotel_inv_nonneg CHECK (available_rooms >= 0)
        )
    """
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_hotel_inv_date   ON hotel_inventory (inventory_date)"
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_hotel_inv_lookup ON hotel_inventory (room_category_id, inventory_date)"
        )
    )

    # ════════════════════════════════════════════════════════════════
    # 16. hotel_verification_assignments — mirrors migration 0017's
    #     vehicle_verification_assignments. Reassign = deactivate old
    #     rows, insert new.
    # ════════════════════════════════════════════════════════════════
    conn.execute(
        sa.text(
            """
        CREATE TABLE IF NOT EXISTS hotel_verification_assignments (
            id            BIGSERIAL PRIMARY KEY,
            hotel_id      BIGINT NOT NULL REFERENCES hotels(id) ON DELETE CASCADE,
            officer_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            assigned_by   UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            assigned_at   TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            unassigned_at TIMESTAMP WITH TIME ZONE,
            is_active     BOOLEAN NOT NULL DEFAULT TRUE,
            notes         TEXT
        )
    """
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_hva_hotel   ON hotel_verification_assignments (hotel_id)"
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_hva_officer ON hotel_verification_assignments (officer_id)"
        )
    )

    # ════════════════════════════════════════════════════════════════
    # 17. hotel_verification_logs — Doc Ref: BRD §91 (immutable logs)
    #     Records from_status/to_status, which partner_verification_logs
    #     does not; without them a status history is guesswork.
    #     Append-only: the service exposes no update or delete.
    # ════════════════════════════════════════════════════════════════
    conn.execute(
        sa.text(
            """
        CREATE TABLE IF NOT EXISTS hotel_verification_logs (
            id           BIGSERIAL PRIMARY KEY,
            hotel_id     BIGINT NOT NULL REFERENCES hotels(id) ON DELETE CASCADE,
            action       VARCHAR(100) NOT NULL,
            from_status  VARCHAR(50),
            to_status    VARCHAR(50),
            remarks      TEXT,
            performed_by UUID REFERENCES users(id),
            created_at   TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """
        )
    )
    conn.execute(
        sa.text(
            """
        CREATE INDEX IF NOT EXISTS idx_hvl_hotel ON hotel_verification_logs (hotel_id, created_at DESC)
    """
        )
    )

    # ════════════════════════════════════════════════════════════════
    # 18-19. Ratings & performance — Doc Ref: DB Part 5 §19-20
    #        Created empty; populated by a later phase. They exist now so
    #        the detail page's Performance tab reads zeros, not 404s.
    # ════════════════════════════════════════════════════════════════
    conn.execute(
        sa.text(
            """
        CREATE TABLE IF NOT EXISTS hotel_ratings (
            id             BIGSERIAL PRIMARY KEY,
            hotel_id       BIGINT NOT NULL UNIQUE REFERENCES hotels(id) ON DELETE CASCADE,
            average_rating NUMERIC(3,2) NOT NULL DEFAULT 0.00,
            total_reviews  INTEGER NOT NULL DEFAULT 0,
            updated_at     TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """
        )
    )
    conn.execute(
        sa.text(
            """
        CREATE TABLE IF NOT EXISTS hotel_performance_summary (
            id                BIGSERIAL PRIMARY KEY,
            hotel_id          BIGINT NOT NULL UNIQUE REFERENCES hotels(id) ON DELETE CASCADE,
            total_bookings    INTEGER NOT NULL DEFAULT 0,
            occupancy_rate    NUMERIC(5,2) NOT NULL DEFAULT 0.00,
            total_revenue     NUMERIC(14,2) NOT NULL DEFAULT 0.00,
            cancellation_rate NUMERIC(5,2) NOT NULL DEFAULT 0.00,
            updated_at        TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """
        )
    )

    # ════════════════════════════════════════════════════════════════
    # 20. hotel_reservations — Doc Ref: DB Part 5 §12, SRS §169-170
    #     SCHEMA ONLY this phase — no service, no endpoint.
    #     The three *_snapshot JSONB columns are the point: BRD Rule 25
    #     forbids later price/commission/policy edits from changing a
    #     confirmed booking, and freezing resolved values on the row is
    #     the only reliable way to honour that.
    # ════════════════════════════════════════════════════════════════
    conn.execute(
        sa.text(
            """
        CREATE TABLE IF NOT EXISTS hotel_reservations (
            id                           BIGSERIAL PRIMARY KEY,
            uuid                         UUID NOT NULL UNIQUE,
            master_booking_id            BIGINT NOT NULL REFERENCES master_bookings(id),
            booking_service_id           BIGINT REFERENCES booking_services(id),
            hotel_id                     BIGINT NOT NULL REFERENCES hotels(id),
            room_category_id             BIGINT NOT NULL REFERENCES hotel_room_categories(id),
            customer_id                  BIGINT,
            reservation_number           VARCHAR(100) UNIQUE,

            check_in_date                DATE NOT NULL,
            check_out_date               DATE NOT NULL,
            nights                       INTEGER NOT NULL DEFAULT 1,
            rooms_count                  INTEGER NOT NULL DEFAULT 1,
            room_nights                  INTEGER NOT NULL DEFAULT 1,
            adults_count                 INTEGER NOT NULL DEFAULT 1,
            children_count               INTEGER NOT NULL DEFAULT 0,

            base_amount                  NUMERIC(12,2) NOT NULL DEFAULT 0,
            extra_charges                NUMERIC(12,2) NOT NULL DEFAULT 0,
            discount_amount              NUMERIC(12,2) NOT NULL DEFAULT 0,
            taxable_amount               NUMERIC(12,2) NOT NULL DEFAULT 0,
            gst_percent                  NUMERIC(5,2) NOT NULL DEFAULT 0,
            gst_amount                   NUMERIC(12,2) NOT NULL DEFAULT 0,
            is_tax_invoice               BOOLEAN NOT NULL DEFAULT FALSE,
            total_amount                 NUMERIC(12,2) NOT NULL DEFAULT 0,
            platform_commission          NUMERIC(12,2) NOT NULL DEFAULT 0,
            partner_payout               NUMERIC(12,2) NOT NULL DEFAULT 0,

            rate_snapshot                JSONB,
            commission_config_snapshot   JSONB,
            cancellation_policy_snapshot JSONB,

            reservation_status           VARCHAR(50) NOT NULL DEFAULT 'PENDING_PAYMENT',
            voucher_url                  TEXT,
            special_requests             TEXT,
            confirmed_at                 TIMESTAMP WITH TIME ZONE,
            cancelled_at                 TIMESTAMP WITH TIME ZONE,
            created_at                   TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            updated_at                   TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """
        )
    )
    for stmt in (
        "CREATE INDEX IF NOT EXISTS idx_reservation_status ON hotel_reservations (reservation_status)",
        "CREATE INDEX IF NOT EXISTS idx_checkin_date       ON hotel_reservations (check_in_date)",
        "CREATE INDEX IF NOT EXISTS idx_checkout_date      ON hotel_reservations (check_out_date)",
        "CREATE INDEX IF NOT EXISTS idx_reservation_hotel  ON hotel_reservations (hotel_id)",
    ):
        conn.execute(sa.text(stmt))

    # ════════════════════════════════════════════════════════════════
    # 21-23. Guests, check-ins, check-outs — Doc Ref: DB Part 5 §14-16
    #        Checkout carries additional_charges / final_bill_amount /
    #        actual_nights per BRD §79-80 and §84 (early check-out).
    # ════════════════════════════════════════════════════════════════
    conn.execute(
        sa.text(
            """
        CREATE TABLE IF NOT EXISTS hotel_reservation_guests (
            id             BIGSERIAL PRIMARY KEY,
            reservation_id BIGINT NOT NULL REFERENCES hotel_reservations(id) ON DELETE CASCADE,
            guest_name     VARCHAR(255) NOT NULL,
            mobile         VARCHAR(15),
            gender         VARCHAR(20),
            age            INTEGER,
            id_type        VARCHAR(50),
            id_number      VARCHAR(100),
            is_primary     BOOLEAN NOT NULL DEFAULT FALSE,
            created_at     TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """
        )
    )
    conn.execute(
        sa.text(
            """
        CREATE INDEX IF NOT EXISTS idx_hrg_reservation ON hotel_reservation_guests (reservation_id)
    """
        )
    )
    conn.execute(
        sa.text(
            """
        CREATE TABLE IF NOT EXISTS hotel_checkins (
            id                BIGSERIAL PRIMARY KEY,
            reservation_id    BIGINT NOT NULL REFERENCES hotel_reservations(id) ON DELETE CASCADE,
            check_in_datetime TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            allocated_rooms   TEXT,
            checked_in_by     UUID REFERENCES users(id),
            remarks           TEXT,
            created_at        TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """
        )
    )
    conn.execute(
        sa.text(
            """
        CREATE TABLE IF NOT EXISTS hotel_checkouts (
            id                 BIGSERIAL PRIMARY KEY,
            reservation_id     BIGINT NOT NULL REFERENCES hotel_reservations(id) ON DELETE CASCADE,
            check_out_datetime TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            additional_charges NUMERIC(12,2) NOT NULL DEFAULT 0,
            final_bill_amount  NUMERIC(12,2) NOT NULL DEFAULT 0,
            actual_nights      INTEGER,
            checked_out_by     UUID REFERENCES users(id),
            remarks            TEXT,
            created_at         TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """
        )
    )

    # ════════════════════════════════════════════════════════════════
    # 25a. system_configurations seeds
    #      GST_ENABLED / GST_RATE already exist (0024) and are REUSED,
    #      not duplicated — the platform tax switch stays global.
    # ════════════════════════════════════════════════════════════════
    conn.execute(
        sa.text(
            """
        INSERT INTO system_configurations (config_key, config_value, description, updated_at)
        VALUES
            ('HOTEL_DEFAULT_COMMISSION_PERCENT', '12',
             'Last-resort hotel commission percentage when no override or commission rule matches', NOW()),
            ('HOTEL_INVENTORY_HORIZON_DAYS', '365',
             'Days ahead for which hotel inventory rows are materialised', NOW()),
            ('HOTEL_RESERVATION_HOLD_MINUTES', '5',
             'Minutes a room is held during payment before the hold expires (SRS Part 5 §167)', NOW()),
            ('HOTEL_CODE_PREFIX', 'HTL',
             'Prefix for generated hotel codes, e.g. HTL000001 (SRS Part 5 §155)', NOW()),
            ('HOTEL_RESERVATION_PREFIX', 'WT-HTL',
             'Prefix for hotel reservation numbers, e.g. WT-HTL-000001 (SRS Part 5 §169)', NOW()),
            ('HOTEL_AUTO_APPROVE_ENABLED', 'false',
             'Whether admins may approve a hotel at own risk, bypassing verification officer review', NOW()),
            ('HOTEL_MIN_IMAGES_REQUIRED', '3',
             'Minimum hotel images required before a hotel can be submitted for verification', NOW()),
            ('HOTEL_ADVANCE_PERCENT_DEFAULT', '30',
             'Default partial advance percentage for hotel bookings (BRD Part 4 §72)', NOW()),
            ('HOTEL_GST_ON_COMMISSION_PERCENT', '18',
             'GST percentage applied to the platform commission invoice raised to the hotel partner', NOW())
        ON CONFLICT (config_key) DO NOTHING
    """
        )
    )

    # ════════════════════════════════════════════════════════════════
    # 25b. Onboarding notification templates.
    #      HOTEL_CONFIRMED / HOTEL_CHECKIN_REMINDER already exist (0012)
    #      and are booking-side; these are onboarding-side.
    # ════════════════════════════════════════════════════════════════
    conn.execute(
        sa.text(
            """
        INSERT INTO notification_templates
            (template_code, channel, template_name, template_content, is_active, created_at)
        VALUES
            ('HOTEL_SUBMITTED', 'SMS', 'Hotel Submitted For Verification',
             'Your hotel {{hotel_name}} ({{hotel_code}}) has been submitted for verification. We will update you shortly. - WayTero',
             TRUE, NOW()),
            ('HOTEL_OFFICER_ASSIGNED', 'SMS', 'Verification Officer Assigned',
             'A verification officer has been assigned to your hotel {{hotel_name}}. Please keep your documents ready. - WayTero',
             TRUE, NOW()),
            ('HOTEL_DOCUMENT_REJECTED', 'SMS', 'Hotel Document Rejected',
             'The {{document_type}} for {{hotel_name}} could not be verified. Reason: {{remarks}}. Please re-upload. - WayTero',
             TRUE, NOW()),
            ('HOTEL_APPROVED', 'SMS', 'Hotel Approved',
             'Congratulations! Your hotel {{hotel_name}} ({{hotel_code}}) has been approved on WayTero.',
             TRUE, NOW()),
            ('HOTEL_REJECTED', 'SMS', 'Hotel Rejected',
             'Your hotel {{hotel_name}} could not be approved. Reason: {{rejection_reason}}. Contact support for help. - WayTero',
             TRUE, NOW()),
            ('HOTEL_ACTIVATED', 'SMS', 'Hotel Live',
             'Your hotel {{hotel_name}} is now live on WayTero and can receive bookings.',
             TRUE, NOW())
        ON CONFLICT (template_code, channel) DO NOTHING
    """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()

    conn.execute(
        sa.text(
            """
        DELETE FROM notification_templates WHERE channel = 'SMS' AND template_code IN (
            'HOTEL_SUBMITTED', 'HOTEL_OFFICER_ASSIGNED', 'HOTEL_DOCUMENT_REJECTED',
            'HOTEL_APPROVED', 'HOTEL_REJECTED', 'HOTEL_ACTIVATED'
        )
    """
        )
    )
    conn.execute(
        sa.text(
            """
        DELETE FROM system_configurations WHERE config_key IN (
            'HOTEL_DEFAULT_COMMISSION_PERCENT', 'HOTEL_INVENTORY_HORIZON_DAYS',
            'HOTEL_RESERVATION_HOLD_MINUTES', 'HOTEL_CODE_PREFIX',
            'HOTEL_RESERVATION_PREFIX', 'HOTEL_AUTO_APPROVE_ENABLED',
            'HOTEL_MIN_IMAGES_REQUIRED', 'HOTEL_ADVANCE_PERCENT_DEFAULT',
            'HOTEL_GST_ON_COMMISSION_PERCENT'
        )
    """
        )
    )

    # Reverse FK order
    for table in (
        "hotel_checkouts",
        "hotel_checkins",
        "hotel_reservation_guests",
        "hotel_reservations",
        "hotel_performance_summary",
        "hotel_ratings",
        "hotel_verification_logs",
        "hotel_verification_assignments",
        "hotel_inventory",
        "hotel_rooms",
        "hotel_room_rate_plans",
        "hotel_room_category_images",
        "hotel_room_category_amenities",
        "hotel_room_categories",
        "hotel_gst_slabs",
        "hotel_commission_configs",
        "hotel_policies",
        "hotel_documents",
        "hotel_images",
        "hotel_amenity_mappings",
        "hotels",
        "hotel_amenities",
        "hotel_categories",
    ):
        conn.execute(sa.text(f"DROP TABLE IF EXISTS {table} CASCADE"))

    conn.execute(sa.text("DROP SEQUENCE IF EXISTS hotel_code_seq"))
