# ============================================================
# WAYTERO — LANDING PAGE CONSTANTS
# File: app/modules/admin/constants/landing_pages.py
# Doc Ref: City + Service-Type Landing Pages §Phase 1
#          Migration 0069_landing_pages
#
# Single source of truth for the landing-page contract: the legal
# page_types, the URL each type produces, the slug shapes the admin is
# warned about, the per-slot crop dimensions the upload modal enforces,
# the block types the website renderer knows how to draw, and the curated
# fallback imagery.
#
# Everything the admin portal and customer-web need to agree with lives
# here so a change is made once.
# ============================================================

import re
from typing import Any, Dict, FrozenSet, List, Optional

# ── Page types ───────────────────────────────────────────────────────────────
# STATE_CABS is deliberately absent: cab fares are priced per city
# (vehicle_pricing_rules.city_id), so a state-scope cab page would have no
# honest number to print.
CITY_HUB = "CITY_HUB"
CITY_CABS = "CITY_CABS"
CITY_HOTELS = "CITY_HOTELS"
CITY_TOURS = "CITY_TOURS"
STATE_HOTELS = "STATE_HOTELS"
STATE_TOURS = "STATE_TOURS"
PLACE_CABS = "PLACE_CABS"

PAGE_TYPES: tuple[str, ...] = (
    CITY_HUB,
    CITY_CABS,
    CITY_HOTELS,
    CITY_TOURS,
    STATE_HOTELS,
    STATE_TOURS,
    PLACE_CABS,
)

CITY_PAGE_TYPES: frozenset[str] = frozenset(
    {CITY_HUB, CITY_CABS, CITY_HOTELS, CITY_TOURS}
)
STATE_PAGE_TYPES: frozenset[str] = frozenset({STATE_HOTELS, STATE_TOURS})
PLACE_PAGE_TYPES: frozenset[str] = frozenset({PLACE_CABS})

# page_type → service_type (None for the hub, which spans all three).
PAGE_TYPE_SERVICE: Dict[str, Optional[str]] = {
    CITY_HUB: None,
    CITY_CABS: "CAB",
    CITY_HOTELS: "HOTEL",
    CITY_TOURS: "TOUR",
    STATE_HOTELS: "HOTEL",
    STATE_TOURS: "TOUR",
    PLACE_CABS: "CAB",
}

# page_type → URL prefix, so the slug is derived rather than typed. The hub
# page is the bare city name because "bhubaneswar" is the keyword travellers
# actually type.
PAGE_TYPE_PREFIX: Dict[str, str] = {
    CITY_HUB: "",
    CITY_CABS: "cab-booking-in-",
    CITY_HOTELS: "hotel-booking-in-",
    CITY_TOURS: "tour-packages-in-",
    STATE_HOTELS: "hotel-booking-in-",
    STATE_TOURS: "tour-packages-in-",
    PLACE_CABS: "",  # suffix-shaped: {city}-{place}-cab
}

# page_type → compiled slug shape. A row whose slug does not match its own
# type's shape is almost certainly a typo (or a renamed city), and the admin
# gets a live warning from this — the check is only bypassed when the editor
# sends slug_confirmed=true.
PAGE_TYPE_URL_PATTERNS: Dict[str, re.Pattern] = {
    pt: re.compile(rf"^{re.escape(prefix)}[a-z0-9]+(?:-[a-z0-9]+)*$")
    for pt, prefix in PAGE_TYPE_PREFIX.items()
}
# Place pages are keyword-suffixed instead of prefixed ("bhubaneswar-airport-cab"),
# so the generic prefix-shaped pattern above does not fit them.
PAGE_TYPE_URL_PATTERNS[PLACE_CABS] = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*-cab$")

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# ── Reserved slugs ───────────────────────────────────────────────────────────
# The landing engine mounts at the site root as /[slug], so any published
# slug that collides with a real route silently shadows it (or gets shadowed
# by it) — a 404 or a wrong page with nothing in the logs to explain it.
# Writes against these are rejected outright.
RESERVED_LANDING_SLUGS: FrozenSet[str] = frozenset(
    {
        # (site) static pages
        "about",
        "account-deletion",
        "booking-instructions",
        "bookings",
        "careers",
        "contact",
        "cookies",
        "forgot-password",
        "login",
        "partner",
        "press",
        "privacy",
        "profile",
        "refund",
        "register",
        "safety",
        "security",
        "support",
        "terms",
        "track",
        "wallet",
        "sitemap",
        # vertical hubs and their children
        "blog",
        "cabs",
        "hotels",
        "tours",
        "destinations",
        # app-level / infrastructure paths
        "api",
        "admin",
        "auth",
        "health",
        "docs",
        "redocs",
        "openapi.json",
        "robots.txt",
        "llms.txt",
        "sitemap.xml",
        "favicon.ico",
        "feed",
        "rss.xml",
        "_next",
        "static",
        "assets",
        "images",
    }
)

# A landing slug is also reserved if it *starts* with one of these, because
# Next resolves /cabs/foo against the /cabs route tree before the root
# [slug] catch-all.
RESERVED_LANDING_PREFIXES: frozenset[str] = frozenset(
    {"blog", "cabs", "hotels", "tours", "destinations", "api", "admin", "_next"}
)


def is_reserved_slug(slug: str) -> bool:
    """True when ``slug`` would collide with a real website route."""
    value = (slug or "").strip().lower()
    if not value:
        return False
    if value in RESERVED_LANDING_SLUGS:
        return True
    head = value.split("-", 1)[0]
    return head in RESERVED_LANDING_PREFIXES


# ── JSONB shape limits ───────────────────────────────────────────────────────
MAX_BLOCKS = 24
MAX_GALLERY = 12
MAX_HIGHLIGHTS = 8
MAX_STATS = 6
MAX_FAQS = 10
MAX_REDIRECT_URL_LEN = 500

# ── Content blocks ───────────────────────────────────────────────────────────
# The website renderer (src/components/renderers/landing/) switches on these.
# An unknown type is ignored at render time but rejected on write, so a typo
# never silently disappears from a published page.
BLOCK_TYPES: frozenset[str] = frozenset(
    {
        "prose",
        "highlights",
        "stats",
        "gallery",
        "faq",
        "fare_matrix",
        "vehicle_grid",
        "hotel_grid",
        "tour_grid",
        "steps",
        "cta_band",
        "internal_links",
    }
)

# Vertical blocks need live data from the API bundle, not authored content —
# they are only legal on the page types that actually carry that data.
DATA_BLOCK_PAGE_TYPES: Dict[str, frozenset[str]] = {
    "fare_matrix": frozenset({CITY_CABS, PLACE_CABS}),
    "vehicle_grid": frozenset({CITY_CABS, PLACE_CABS}),
    "hotel_grid": frozenset({CITY_HOTELS, STATE_HOTELS, CITY_HUB, PLACE_CABS}),
    "tour_grid": frozenset({CITY_TOURS, STATE_TOURS, CITY_HUB, PLACE_CABS}),
}

# Service types that make a block meaningless on a page (a CAB block on a
# tour page) — enforced on write, tolerated on read.
BLOCK_SERVICE_TYPES: Dict[str, frozenset[str]] = {
    "fare_matrix": frozenset({"CAB"}),
    "vehicle_grid": frozenset({"CAB"}),
    "hotel_grid": frozenset({"HOTEL"}),
    "tour_grid": frozenset({"TOUR"}),
}

# Place pages are CAB pages that may ALSO cross-sell hotel/tour grids for the
# same location — the only exception to "page service must match block
# service". The payload strips these blocks server-side when no real supply
# matches the place, so an authored grid never renders empty marketing.
CROSS_SELL_ALLOWED: Dict[str, frozenset[str]] = {
    PLACE_CABS: frozenset({"hotel_grid", "tour_grid"}),
}


def block_allowed(page_type: str, block_type: str) -> bool:
    """
    The one rule for "may this block sit on this page type".

    Both the write-path validator and the admin's block picker call this, so
    the editor can never offer something the server would reject. A hub page
    carries every vertical by design and has no service type of its own, so the
    vertical check deliberately skips it — ``DATA_BLOCK_PAGE_TYPES`` already
    lists the hub as a legal host for the hotel and tour grids.
    """
    allowed_page_types = DATA_BLOCK_PAGE_TYPES.get(block_type)
    if allowed_page_types is not None and page_type not in allowed_page_types:
        return False
    if block_type in CROSS_SELL_ALLOWED.get(page_type, frozenset()):
        return True
    wanted_services = BLOCK_SERVICE_TYPES.get(block_type)
    service = PAGE_TYPE_SERVICE.get(page_type)
    if wanted_services and service is not None and service not in wanted_services:
        return False
    return True


def blocks_allowed_for(page_type: str) -> List[str]:
    """Sorted vocabulary the editor offers for a page type."""
    return sorted(b for b in BLOCK_TYPES if block_allowed(page_type, b))


# ── Per-slot image specs ─────────────────────────────────────────────────────
# Consumed by the admin crop modal AND the strict dimension check on
# media-library picks; the website trusts whatever comes out. Aspect ratios
# match the responsive contract in the plan (desktop hero 2.4:1, mobile hero
# portrait, OG 1.9:1 per Google's 1200×630 recommendation).
SLOT_SPECS: Dict[str, Dict[str, Any]] = {
    "banner_image_url": {
        "label": "Desktop banner",
        "width": 1920,
        "height": 800,
        "min_width": 1400,
        "min_height": 600,
        "aspect": 2.4,
        "max_bytes": 400_000,
        "formats": ("jpg", "webp", "avif"),
    },
    "banner_mobile_url": {
        "label": "Mobile banner",
        "width": 900,
        "height": 1200,
        "min_width": 700,
        "min_height": 900,
        "aspect": 0.75,
        "max_bytes": 300_000,
        "formats": ("jpg", "webp", "avif"),
    },
    "og_image_url": {
        "label": "Social share image",
        "width": 1200,
        "height": 630,
        "min_width": 600,
        "min_height": 315,
        "aspect": 1.9048,
        "max_bytes": 200_000,
        "formats": ("jpg", "png", "webp"),
    },
    "gallery_tile": {
        "label": "Gallery tile",
        "width": 1200,
        "height": 800,
        "min_width": 800,
        "min_height": 500,
        "aspect": 1.5,
        "max_bytes": 250_000,
        "formats": ("jpg", "webp", "avif"),
    },
    "highlight_icon": {
        "label": "Highlight icon",
        "width": 200,
        "height": 200,
        "min_width": 120,
        "min_height": 120,
        "aspect": 1.0,
        "max_bytes": 80_000,
        "formats": ("png", "webp", "svg"),
    },
}


def slot_spec(slot: str) -> Optional[Dict[str, Any]]:
    return SLOT_SPECS.get(slot)


# ── Default block layout per page type ───────────────────────────────────────
# A freshly provisioned page renders a complete, sensible story with zero
# admin work: derived copy fills the text, the data blocks pull live prices.
# Admins reorder/add/remove from here; they are not starting from a blank page.
def default_blocks(page_type: str) -> List[Dict[str, Any]]:
    blocks: List[Dict[str, Any]] = [
        {"type": "highlights", "title": None},
        {"type": "stats", "title": None},
    ]

    if page_type == CITY_HUB:
        blocks += [
            {"type": "tour_grid", "title": "Popular tour packages", "limit": 6},
            {"type": "hotel_grid", "title": "Where to stay", "limit": 6},
            {"type": "steps", "title": "How booking with WayTero works"},
            {"type": "internal_links", "title": "More in this city"},
        ]
    elif page_type == CITY_CABS:
        blocks += [
            {"type": "fare_matrix", "title": "Cab fares in this city"},
            {"type": "vehicle_grid", "title": "Choose your vehicle"},
            {"type": "steps", "title": "How booking a cab works"},
        ]
    elif page_type == PLACE_CABS:
        blocks += [
            {"type": "fare_matrix", "title": "Fares to and from this place"},
            {"type": "vehicle_grid", "title": "Choose your vehicle"},
            {"type": "hotel_grid", "title": "Stay near this place", "limit": 6},
            {"type": "tour_grid", "title": "Packages that start here", "limit": 6},
            {"type": "steps", "title": "How booking a cab works"},
        ]
    elif page_type in (CITY_HOTELS, STATE_HOTELS):
        blocks += [
            {"type": "hotel_grid", "title": "Available stays", "limit": 9},
            {"type": "steps", "title": "How hotel booking works"},
        ]
    elif page_type in (CITY_TOURS, STATE_TOURS):
        blocks += [
            {"type": "tour_grid", "title": "Tour packages", "limit": 9},
            {"type": "steps", "title": "How tour booking works"},
        ]

    blocks += [
        {"type": "gallery", "title": "Gallery"},
        {"type": "faq", "title": "Frequently asked questions"},
        {"type": "cta_band", "title": None},
    ]
    return blocks


# ── Curated fallback imagery — DELIBERATELY EMPTY ────────────────────────────
# Owner decision (2026-10-15): no curated fallback photos at all. A landing page
# whose pitch is "real prices, real supply" must not decorate itself with stock
# photography of somewhere else, and every automated sourcing route we checked
# was licence-blocked (image search returns unlicensable aggregator re-hosts;
# Wikimedia Commons travel photos are overwhelmingly CC BY-SA, whose Share-Alike
# would attach to our own marketing asset once cropped/re-graded).
#
# So the only image source is an admin or partner upload on the page itself;
# until then image_source resolves to "gradient" and the website renders the
# brand gradient + iconography hero treatment. That treatment is the default
# look of every new landing page, so it has to be designed, not improvised.
#
# If real art is ever wanted, fill these maps with Cloudinary URLs from
# waytero/landing (upload through /admin/settings/upload-media with
# folder_override) and record each file's provenance here.
CURATED_IMAGE_FOLDER = "waytero/landing"

CURATED_IMAGES: Dict[str, Dict[str, Dict[str, str]]] = {
    "default": {
        CITY_HUB: {},
        CITY_CABS: {},
        CITY_HOTELS: {},
        CITY_TOURS: {},
        STATE_HOTELS: {},
        STATE_TOURS: {},
        PLACE_CABS: {},
    },
    # Per-scope overrides, keyed by the landing slug without its prefix
    # (i.e. the city/state slug).
    "by_scope": {},
}

CURATED_SLOTS: tuple[str, ...] = (
    "banner_image_url",
    "banner_mobile_url",
    "og_image_url",
)


def curated_for(page_type: str, scope_slug: Optional[str] = None) -> Dict[str, str]:
    """
    Curated slot → URL map for a page, merged per slot — most specific wins.

    Resolution order per slot: per-scope override → page-type default → absent.
    Merging (rather than replacing) matters: a city override that only authors
    a desktop banner must not drop that page's social image back to the
    gradient treatment. Callers treat an empty dict as "no curated art".
    """
    merged: Dict[str, str] = {
        k: v
        for k, v in ((CURATED_IMAGES["default"] or {}).get(page_type) or {}).items()
        if v
    }
    if scope_slug:
        scoped = ((CURATED_IMAGES["by_scope"] or {}).get(scope_slug) or {}).get(
            page_type
        ) or {}
        merged.update({k: v for k, v in scoped.items() if v})
    return merged
