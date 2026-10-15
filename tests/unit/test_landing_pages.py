# ============================================================
# WAYTERO — LANDING PAGE TESTS
# File: tests/unit/test_landing_pages.py
# Doc Ref: City + Service-Type Landing Pages §Phase 1
#          Migration 0069_landing_pages
#
# Covers the parts of the landing-page contract that must never drift and
# that need no database: the URL shape per page_type, the reserved-slug wall,
# write-time payload validation, and the three derivation rules the public
# payload is built from (slug, hero imagery source, stat chips).
# ============================================================

import asyncio

import pytest
from pydantic import ValidationError

from app.core.exceptions import ValidationException
from app.modules.admin.constants.landing_pages import (
    BLOCK_SERVICE_TYPES,
    CITY_PAGE_TYPES,
    CROSS_SELL_ALLOWED,
    DATA_BLOCK_PAGE_TYPES,
    MAX_BLOCKS,
    PAGE_TYPE_PREFIX,
    PAGE_TYPE_SERVICE,
    PAGE_TYPE_URL_PATTERNS,
    PAGE_TYPES,
    PLACE_PAGE_TYPES,
    SLOT_SPECS,
    STATE_PAGE_TYPES,
    curated_for,
    blocks_allowed_for,
    default_blocks,
    is_reserved_slug,
)
from app.modules.admin.constants.landing_pages import CURATED_IMAGES
from app.modules.admin.models import LandingPage
from app.modules.admin.schemas.landing_pages import (
    LandingPageCreate,
    LandingPageUpdate,
)
from app.modules.admin.services.landing_pages import LandingPageService, _rupiah

# ── Slug derivation ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "page_type,scope_name,expected",
    [
        ("CITY_HUB", "Bhubaneswar", "bhubaneswar"),
        ("CITY_CABS", "Bhubaneswar", "cab-booking-in-bhubaneswar"),
        ("CITY_HOTELS", "Bhubaneswar", "hotel-booking-in-bhubaneswar"),
        ("CITY_TOURS", "Bhubaneswar", "tour-packages-in-bhubaneswar"),
        ("STATE_HOTELS", "Odisha", "hotel-booking-in-odisha"),
        ("STATE_TOURS", "West Bengal", "tour-packages-in-west-bengal"),
    ],
)
def test_derive_slug_matches_the_keyword_url_contract(
    page_type: str, scope_name: str, expected: str
) -> None:
    assert LandingPageService.derive_slug(page_type, scope_name) == expected


@pytest.mark.parametrize("page_type", PAGE_TYPES)
def test_derived_slug_always_satisfies_its_own_page_type_pattern(
    page_type: str,
) -> None:
    if page_type in PLACE_PAGE_TYPES:
        slug = LandingPageService.derive_place_slug("Bhubaneswar", "Airport")
    else:
        slug = LandingPageService.derive_slug(page_type, "Bhubaneswar")

    assert PAGE_TYPE_URL_PATTERNS[page_type].match(slug)


def test_derive_slug_rejects_a_name_with_no_url_safe_characters() -> None:
    with pytest.raises(ValidationException):
        LandingPageService.derive_slug("CITY_HUB", "  ---  ")


def test_derived_landing_slugs_are_not_reserved() -> None:
    """The reserved wall must not shadow our own URL scheme — a city named
    exactly like a prefix would otherwise make its page unpublishable."""
    for page_type in PAGE_TYPES:
        slug = f"{PAGE_TYPE_PREFIX[page_type]}bhubaneswar"
        assert not is_reserved_slug(slug), slug


@pytest.mark.parametrize(
    "slug",
    [
        "cabs",
        "hotels",
        "blog",
        "destinations",
        "login",
        "admin",
        "api",
        "sitemap.xml",
        # /cabs/x resolves inside the /cabs route tree before the root catch-all
        "cabs-in-bhubaneswar",
        "hotels-in-bhubaneswar",
    ],
)
def test_reserved_slugs_are_rejected(slug: str) -> None:
    assert is_reserved_slug(slug)


# ── Contract self-consistency ────────────────────────────────────────────────


def test_page_type_vocabulary_is_the_documented_seven_with_no_state_cabs() -> None:
    assert set(PAGE_TYPES) == {
        "CITY_HUB",
        "CITY_CABS",
        "CITY_HOTELS",
        "CITY_TOURS",
        "STATE_HOTELS",
        "STATE_TOURS",
        "PLACE_CABS",
    }
    assert "STATE_CABS" not in PAGE_TYPES
    assert CITY_PAGE_TYPES | STATE_PAGE_TYPES | PLACE_PAGE_TYPES == set(PAGE_TYPES)
    assert not (CITY_PAGE_TYPES & STATE_PAGE_TYPES)
    assert not (PLACE_PAGE_TYPES & (CITY_PAGE_TYPES | STATE_PAGE_TYPES))


def test_every_page_type_declares_a_url_prefix_and_service() -> None:
    assert set(PAGE_TYPE_PREFIX) == set(PAGE_TYPES)
    assert set(PAGE_TYPE_SERVICE) == set(PAGE_TYPES)
    assert PAGE_TYPE_SERVICE["CITY_HUB"] is None


def test_default_blocks_are_legal_for_their_own_page_type() -> None:
    for page_type in PAGE_TYPES:
        blocks = default_blocks(page_type)
        assert blocks, page_type
        assert len(blocks) <= MAX_BLOCKS
        for block in blocks:
            btype = block["type"]
            allowed_types = DATA_BLOCK_PAGE_TYPES.get(btype)
            if allowed_types is not None:
                assert page_type in allowed_types, (page_type, btype)


def test_the_editor_offers_every_block_the_default_layout_uses() -> None:
    """``blocks_allowed_for`` is what the admin's add-block menu shows. If a
    default layout contained a block the menu would refuse to offer, the admin
    could delete it but never put it back."""
    for page_type in PAGE_TYPES:
        offered = set(blocks_allowed_for(page_type))
        for block in default_blocks(page_type):
            assert block["type"] in offered, (page_type, block["type"])


def test_the_two_block_legality_maps_agree() -> None:
    """A data block is legal on a page type only where its own service type is
    in play — the page-type list and the service list must never drift, or the
    admin could save a hotel grid onto a tour page. The one declared exception
    is CROSS_SELL_ALLOWED (place pages may carry hotel/tour grids)."""
    for btype, page_types in DATA_BLOCK_PAGE_TYPES.items():
        services = BLOCK_SERVICE_TYPES[btype]
        for page_type in page_types:
            if btype in CROSS_SELL_ALLOWED.get(page_type, frozenset()):
                continue
            service = PAGE_TYPE_SERVICE[page_type]
            # The hub spans all three verticals, so it carries no service type.
            assert service is None or service in services, (btype, page_type)


def test_a_hub_pages_own_default_grids_are_never_blockers() -> None:
    """The hub's whole pitch is cabs + stays + tours in one place. Its default
    layout carries a hotel_grid and a tour_grid, so if the vertical check applied
    to a page with no service type, every provisioned hub would ship with two
    red blockers and could only be published by forcing it."""
    hub = LandingPage(
        slug="bhubaneswar",
        page_type="CITY_HUB",
        service_type=None,
        city_id=1,
        blocks=default_blocks("CITY_HUB"),
        faqs=[{"question": "Q", "answer": "A"}],
    )
    blockers = asyncio.run(LandingPageService.publish_blockers(None, hub))
    assert not [b for b in blockers if "hotel_grid" in b or "tour_grid" in b], blockers


def test_a_vertical_block_on_the_wrong_vertical_is_still_a_blocker() -> None:
    """The hub exemption must not become a blanket exemption: a tour page with a
    hotel grid is still the mistake the check exists to catch."""
    tour = LandingPage(
        slug="tour-packages-in-odisha",
        page_type="STATE_TOURS",
        service_type="TOUR",
        state_id=1,
        blocks=[{"type": "hotel_grid", "title": "nope"}],
        faqs=[],
    )
    blockers = asyncio.run(LandingPageService.publish_blockers(None, tour))
    assert any("hotel_grid" in b for b in blockers), blockers


def test_slot_specs_cover_every_uploader_slot_with_usable_numbers() -> None:
    for slot, spec in SLOT_SPECS.items():
        assert spec["width"] > spec["min_width"] > 0, slot
        assert spec["height"] > spec["min_height"] > 0, slot
        assert spec["aspect"] == pytest.approx(
            spec["width"] / spec["height"], abs=0.02
        ), slot


# ── Write-time payload validation ────────────────────────────────────────────


def test_create_accepts_a_city_page_and_trims_copy() -> None:
    payload = LandingPageCreate(
        page_type="CITY_HOTELS",
        city_id=5,
        slug="hotel-booking-in-bhubaneswar",
        headline="  Hotel Booking in Bhubaneswar  ",
        seo_title="",
    )

    assert payload.headline == "Hotel Booking in Bhubaneswar"
    # blank string means "not authored", so the service can derive it
    assert payload.seo_title is None


@pytest.mark.parametrize(
    "page_type,city_id,state_id",
    [
        ("CITY_HOTELS", None, None),
        ("CITY_HOTELS", 5, 1),
        ("CITY_HOTELS", None, 1),
        ("STATE_HOTELS", 5, None),
        ("STATE_HOTELS", None, None),
    ],
)
def test_create_rejects_page_type_scope_mismatch(
    page_type: str, city_id, state_id
) -> None:
    with pytest.raises(ValidationError):
        LandingPageCreate(page_type=page_type, city_id=city_id, state_id=state_id)


@pytest.mark.parametrize(
    "slug", ["Hotel Booking", "hotel_booking", "-x", "x-", "hop!", ""]
)
def test_create_rejects_a_slug_outside_the_one_regex(slug: str) -> None:
    with pytest.raises(ValidationError):
        LandingPageCreate(page_type="CITY_HUB", city_id=1, slug=slug)


def test_create_rejects_state_cabs_as_a_page_type() -> None:
    with pytest.raises(ValidationError):
        LandingPageCreate(page_type="STATE_CABS", state_id=1)


def test_blocks_reject_an_unknown_type_on_write() -> None:
    with pytest.raises(ValidationError, match="unknown type"):
        LandingPageCreate(
            page_type="CITY_HUB",
            city_id=1,
            blocks=[{"type": "prose"}, {"type": "video_embed"}],
        )


def test_blocks_are_capped() -> None:
    with pytest.raises(ValidationError):
        LandingPageCreate(
            page_type="CITY_HUB",
            city_id=1,
            blocks=[{"type": "prose"} for _ in range(MAX_BLOCKS + 1)],
        )


def test_faqs_need_both_question_and_answer() -> None:
    """A half-written FAQ would render an accordion hole and still be sent to
    Google as FAQPage markup."""
    with pytest.raises(ValidationError, match="answer is required"):
        LandingPageCreate(
            page_type="CITY_HUB",
            city_id=1,
            faqs=[{"question": "Do you charge GST?", "answer": "  "}],
        )


def test_gallery_tiles_need_an_image_url() -> None:
    with pytest.raises(ValidationError, match="image_url is required"):
        LandingPageCreate(
            page_type="CITY_HUB", city_id=1, gallery=[{"caption": "Sea face"}]
        )


def test_overlay_opacity_is_a_unit_interval() -> None:
    with pytest.raises(ValidationError):
        LandingPageUpdate(overlay_opacity=1.4)


def test_update_allows_a_partial_payload() -> None:
    payload = LandingPageUpdate(headline="New headline")

    assert payload.headline == "New headline"
    assert payload.slug is None
    assert payload.blocks is None


# ── Hero imagery resolution ──────────────────────────────────────────────────


def test_hero_reports_gradient_when_no_art_exists_anywhere(monkeypatch) -> None:
    monkeypatch.setitem(CURATED_IMAGES, "default", {})
    page = LandingPage(slug="bhubaneswar", page_type="CITY_HUB")

    hero, source = LandingPageService.resolve_hero(page, "CITY_HUB", "Bhubaneswar")

    assert source == "gradient"
    assert hero["banner_image_url"] is None


def test_hero_prefers_the_admin_upload_over_curated_art(monkeypatch) -> None:
    monkeypatch.setitem(
        CURATED_IMAGES,
        "default",
        {"CITY_HOTELS": {"banner_image_url": "https://cdn.example/curated.jpg"}},
    )
    page = LandingPage(
        slug="hotel-booking-in-bhubaneswar",
        page_type="CITY_HOTELS",
        banner_image_url="https://cdn.example/admin.jpg",
    )

    hero, source = LandingPageService.resolve_hero(page, "CITY_HOTELS", "Bhubaneswar")

    assert source == "admin"
    assert hero["banner_image_url"] == "https://cdn.example/admin.jpg"


def test_hero_falls_back_to_curated_art_slot_by_slot(monkeypatch) -> None:
    monkeypatch.setitem(
        CURATED_IMAGES,
        "default",
        {
            "CITY_CABS": {
                "banner_image_url": "https://cdn.example/curated-desktop.jpg",
                "og_image_url": "https://cdn.example/curated-og.jpg",
            }
        },
    )
    page = LandingPage(
        slug="cab-booking-in-bhubaneswar",
        page_type="CITY_CABS",
        banner_mobile_url="https://cdn.example/admin-mobile.jpg",
    )

    hero, source = LandingPageService.resolve_hero(page, "CITY_CABS", "Bhubaneswar")

    # One authored slot makes the page an "admin" page for imagery reporting…
    assert source == "admin"
    assert hero["banner_mobile_url"] == "https://cdn.example/admin-mobile.jpg"
    # …but the empty slots still get the curated art, not a broken image.
    assert hero["banner_image_url"] == "https://cdn.example/curated-desktop.jpg"
    assert hero["og_image_url"] == "https://cdn.example/curated-og.jpg"


def test_curated_lookup_prefers_the_scope_override_over_the_default(
    monkeypatch,
) -> None:
    monkeypatch.setitem(
        CURATED_IMAGES,
        "default",
        {
            "CITY_HUB": {
                "banner_image_url": "https://cdn.example/generic-hub.jpg",
                "og_image_url": "https://cdn.example/generic-og.jpg",
            }
        },
    )
    monkeypatch.setitem(
        CURATED_IMAGES,
        "by_scope",
        {
            "bhubaneswar": {
                "CITY_HUB": {"banner_image_url": "https://cdn.example/bbsr.jpg"}
            }
        },
    )

    resolved = curated_for("CITY_HUB", "bhubaneswar")

    # The scope override wins where it has the slot; the page-type default
    # fills the rest, so one curated city never loses its social image.
    assert resolved["banner_image_url"] == "https://cdn.example/bbsr.jpg"
    assert resolved["og_image_url"] == "https://cdn.example/generic-og.jpg"

    assert curated_for("CITY_HUB", "cuttack")["banner_image_url"] == (
        "https://cdn.example/generic-hub.jpg"
    )


# ── Stat chips ───────────────────────────────────────────────────────────────


def _prices() -> dict:
    return {
        "hotel_count": 12,
        "tour_count": 8,
        "hotel_from": 4500,
        "tour_from": 2800,
        "cab_from": 210,
        "vehicle_count": 7,
        "avg_hotel_rating": 4.3,
    }


def test_stat_chips_stay_inside_the_pages_own_vertical() -> None:
    """A cab page printing "12 Hotels bookable" is true and still noise."""
    cab_page = LandingPage(slug="cab-booking-in-x", page_type="CITY_CABS")

    labels = [s["label"] for s in LandingPageService.resolve_stats(cab_page, _prices())]

    assert labels == ["Cab trip from", "Vehicle options"]


def test_hub_page_shows_all_three_verticals() -> None:
    hub = LandingPage(slug="bhubaneswar", page_type="CITY_HUB")

    labels = [s["label"] for s in LandingPageService.resolve_stats(hub, _prices())]

    assert labels == [
        "Hotels bookable",
        "Tour packages",
        "Hotel price from",
        "Package from",
        "Cab trip from",
        "Avg guest rating",
    ]


def test_zero_and_none_values_never_become_a_stat_chip() -> None:
    """Regression: an unrated city used to render "0/5 Avg guest rating"."""
    page = LandingPage(
        slug="hotel-booking-in-x",
        page_type="CITY_HOTELS",
    )
    prices = {**_prices(), "avg_hotel_rating": 0, "hotel_count": None}

    labels = [s["label"] for s in LandingPageService.resolve_stats(page, prices)]

    assert labels == ["Hotel price from"]


def test_a_single_item_is_never_claimed_as_a_plus() -> None:
    """ "1+ Hotels bookable" overstates one property — the derived numbers on
    this page are the platform's own, so they must read exactly."""
    hub = LandingPage(slug="x", page_type="CITY_HUB")
    prices = {**_prices(), "hotel_count": 1, "tour_count": 1, "vehicle_count": 1}

    chips = LandingPageService.resolve_stats(hub, prices)
    labels = [s["label"] for s in chips]

    assert "Hotel bookable" in labels
    assert "Tour package" in labels
    assert not any(s["suffix"] == "+" for s in chips)

    cab = LandingPage(slug="y", page_type="CITY_CABS")
    cab_chips = LandingPageService.resolve_stats(cab, {**_prices(), "vehicle_count": 1})
    assert [s["label"] for s in cab_chips] == ["Cab trip from", "Vehicle option"]


def test_authored_stats_win_over_derived_ones() -> None:
    page = LandingPage(
        slug="bhubaneswar",
        page_type="CITY_HUB",
        stats=[{"value": "1L+", "suffix": "", "label": "Happy travellers"}],
    )

    assert LandingPageService.resolve_stats(page, _prices()) == page.stats


# ── Derived copy ─────────────────────────────────────────────────────────────


def _derive(page_type: str, prices: dict, slug: str, name: str = "Bhubaneswar") -> dict:
    page = LandingPage(slug=slug, page_type=page_type)
    return asyncio.run(
        LandingPageService.derive_copy(
            None, page, scope_name=name, region_name="Odisha", prices=prices
        )
    )


def test_derived_price_line_is_a_sentence_and_only_honours_real_numbers() -> None:
    derived = _derive("CITY_HOTELS", {"hotel_from": 4500, "tour_from": 2800}, "x")

    # The hotel page can honour the hotel price, not the tour price.
    assert derived["subheadline"].endswith("Hotels from ₹4,500.")
    assert "2,800" not in derived["subheadline"]
    assert "₹4500" not in derived["subheadline"]


def test_seo_description_does_not_repeat_the_price_line_from_the_subheadline() -> None:
    derived = _derive("CITY_HOTELS", {"hotel_from": 4500}, "x")

    assert derived["seo_description"].count("₹4,500") == 1


def test_no_price_claim_is_made_when_the_tables_have_nothing_to_say() -> None:
    derived = _derive(
        "CITY_CABS", {"hotel_from": 4500, "tour_from": 2800, "cab_from": None}, "x"
    )

    assert "₹" not in derived["subheadline"]
    assert derived["subheadline"] == (
        "Local, airport and outstation cabs in Bhubaneswar with upfront fares."
    )


# ── Money formatting ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,expected",
    [
        (210, "₹210"),
        (4500, "₹4,500"),
        (125000, "₹1,25,000"),
        (10000000, "₹1,00,00,000"),
        ("4500.0", "₹4,500"),
    ],
)
def test_rupiah_uses_indian_digit_grouping_and_one_symbol(value, expected: str) -> None:
    assert _rupiah(value) == expected
