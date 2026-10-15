# ============================================================
# WAYTERO — PLACE PAGES + AVAILABILITY TESTS
# File: tests/unit/test_places.py
# Doc Ref: Place-Level Landing Pages & Hard Service Availability
#          (migration 0070_places, public_availability.py)
#
# Locks the three contracts that must never drift: the keyword-first place
# slug ({city}-{place}-cab with city-prefix dedupe), the cross-sell exception
# that lets a CAB place page carry hotel/tour grids (and nothing else may),
# and the /public/availability payload shape the whole website hides on.
# ============================================================

import pytest

from app.core.exceptions import ValidationException
from app.modules.admin.constants.landing_pages import (
    PLACE_CABS,
    PAGE_TYPE_URL_PATTERNS,
    block_allowed,
    blocks_allowed_for,
    default_blocks,
)
from app.modules.admin.services.landing_pages import LandingPageService

# ── Place slug derivation ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "city,place,expected",
    [
        (
            "Bhubaneswar",
            "Biju Patnaik International Airport",
            "bhubaneswar-biju-patnaik-international-airport-cab",
        ),
        # the seeded station: its name already carries the city, so the
        # dedupe must fire or the URL would read bhubaneswar-bhubaneswar-…
        (
            "Bhubaneswar",
            "Bhubaneswar Railway Station",
            "bhubaneswar-railway-station-cab",
        ),
        ("Puri", "Jagannath Temple Bus Stand", "puri-jagannath-temple-bus-stand-cab"),
    ],
)
def test_derive_place_slug_is_keyword_first_and_dedupes_the_city(
    city: str, place: str, expected: str
) -> None:
    assert LandingPageService.derive_place_slug(city, place) == expected


def test_derived_place_slugs_always_satisfy_the_place_url_pattern() -> None:
    for city, place in [
        ("Bhubaneswar", "Airport"),
        ("Kolkata", "Howrah Junction"),
        (None, "Bus Stand"),  # city may be missing; suffix still legal
    ]:
        slug = LandingPageService.derive_place_slug(city, place)
        assert PAGE_TYPE_URL_PATTERNS[PLACE_CABS].match(slug), slug
        assert slug.endswith("-cab")


def test_place_slug_without_a_slugable_name_is_rejected() -> None:
    with pytest.raises(ValidationException):
        LandingPageService.derive_place_slug("Bhubaneswar", "  --  ")


def test_overlong_place_names_fail_loud_instead_of_truncating() -> None:
    """A silently truncated slug would collide with a shorter sibling page,
    so the admin must rename — the 120-char column limit is not a licence to
    chop."""
    with pytest.raises(ValidationException):
        LandingPageService.derive_place_slug("Bhubaneswar", "Station " * 30)


# ── Cross-sell block legality ────────────────────────────────────────────────


def test_place_cab_pages_may_cross_sell_hotel_and_tour_grids() -> None:
    assert block_allowed(PLACE_CABS, "hotel_grid")
    assert block_allowed(PLACE_CABS, "tour_grid")
    assert block_allowed(PLACE_CABS, "fare_matrix")
    assert block_allowed(PLACE_CABS, "vehicle_grid")


def test_no_other_cab_page_gets_the_cross_sell_exception() -> None:
    """The whole point of PLACE_CABS is one location's supply; a city cab
    page carrying a hotel grid is still the mistake the rule guards against."""
    assert not block_allowed("CITY_CABS", "hotel_grid")
    assert not block_allowed("CITY_CABS", "tour_grid")


def test_the_reverse_still_holds_a_hotel_page_cannot_host_fare_matrix() -> None:
    assert not block_allowed("CITY_HOTELS", "fare_matrix")
    assert not block_allowed("STATE_HOTELS", "vehicle_grid")


def test_editor_offers_place_pages_exactly_the_default_vocabulary() -> None:
    offered = set(blocks_allowed_for(PLACE_CABS))
    for block in default_blocks(PLACE_CABS):
        assert block["type"] in offered


# ── /public/availability payload shape ───────────────────────────────────────


def test_availability_payload_shape_is_stable_for_the_website(client) -> None:
    """customer-web's getAvailability()/serviceVisible() branch purely on
    {services:{KEY:{active,supply_count,city_ids}}}; a renamed key silently
    fail-opens the entire hide-policy."""
    response = client.get("/api/v1/public/availability")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"services"}
    assert set(body["services"]) == {"CAB", "HOTEL", "TOUR"}
    for key, entry in body["services"].items():
        assert set(entry) == {"active", "supply_count", "city_ids"}, key
        assert isinstance(entry["active"], bool), key
        assert isinstance(entry["supply_count"], int), key
        assert entry["supply_count"] >= 0, key
        assert isinstance(entry["city_ids"], list), key
        assert all(isinstance(c, int) for c in entry["city_ids"]), key
    assert "max-age" in response.headers["cache-control"]


def test_place_public_feed_carries_coords_and_city_for_intent_matching(client) -> None:
    """/public/places feeds the geolocation intent matcher; a missing
    latitude/longitude/city_id would silently kill the airport banner."""
    response = client.get("/api/v1/public/places")

    assert response.status_code == 200
    rows = response.json()
    assert isinstance(rows, list)
    for row in rows:
        assert row["kind"] in {"AIRPORT", "RAILWAY_STATION"}
        assert row["name"]
        assert row["city_id"] is not None
        assert {"latitude", "longitude"} <= set(row)
