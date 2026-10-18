"""Unit tests for the CMS content picker and the content contract.

Doc Ref: BRD Part 6 §155, Website CMS.

These cover the two rules the admin content editor now depends on:
  1. `picker_items` only ever serves records that exist, and never leaks the
     personal columns that were collected to verify a booking.
  2. `validate_content` is what makes an unknown content key a 400 instead of
     silently-stored data nothing renders.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.core.exceptions import ValidationException
from app.modules.admin.services import CmsService


def mock_db(rows):
    """One execute() call, one .all() result — the shape picker_items uses."""
    result = Mock(spec=["all", "scalars"])
    result.all.return_value = rows
    scalars = Mock()
    scalars.all.return_value = rows
    result.scalars.return_value = scalars
    return SimpleNamespace(execute=AsyncMock(return_value=result))


def compiled(db) -> str:
    """The SQL the picker actually asked for, with its literals inlined.

    `status = 'APPROVED'` compiles to a bind parameter, so reading `str(stmt)`
    alone cannot tell a filter that exists from one that was forgotten.
    """
    statement = db.execute.await_args.args[0]
    return str(statement.compile(compile_kwargs={"literal_binds": True}))


# ── reviewer_display_name ─────────────────────────────────────────


@pytest.mark.parametrize(
    "source,guest_name,first_name,last_name,expected",
    [
        # A logged-out traveller typed their own name; show it as written.
        ("GUEST_WEB", "Meera Kulkarni", None, None, "Meera Kulkarni"),
        ("GUEST_WEB", "  Anita  ", None, None, "Anita"),
        ("GUEST_WEB", "   ", "Rahul", "Sharma", "WayTero customer"),
        ("GUEST_WEB", None, None, None, "WayTero customer"),
        # A registered customer is only ever published initial-last.
        ("CUSTOMER", None, "Rahul", "Sharma", "Rahul S."),
        ("customer", None, "Rahul", "Sharma", "Rahul S."),
        ("CUSTOMER", None, "Rahul", "  ", "Rahul"),
        ("CUSTOMER", None, "  Rahul ", "Sharma", "Rahul S."),
        ("CUSTOMER", None, None, None, "WayTero customer"),
    ],
)
def test_reviewer_display_name_masks_registered_customers(
    source, guest_name, first_name, last_name, expected
) -> None:
    assert (
        CmsService.reviewer_display_name(source, guest_name, first_name, last_name)
        == expected
    )


def test_reviewer_display_name_never_repeats_the_full_name() -> None:
    """The mask is the point: a surname beyond its initial must not survive."""
    name = CmsService.reviewer_display_name("CUSTOMER", "Ignored", "Rahul", "Sharma")
    assert "Sharma" not in name
    assert len(name) < len("Rahul Sharma") + 2


# ── picker_items: REVIEWS ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_reviews_picker_maps_renderer_keys() -> None:
    db = mock_db(
        [
            (
                "Booked  a   cab\n  for the airport run.",
                5,
                "CUSTOMER",
                None,
                "Rahul",
                "Sharma",
                "Pune",
            ),
            (
                "Loved the hotel.",
                None,
                "GUEST_WEB",
                "Meera",
                None,
                None,
                None,
            ),
        ]
    )

    payload = await CmsService.picker_items(db, "reviews")

    assert payload["type"] == "REVIEWS"
    assert payload["items"] == [
        {
            "name": "Rahul S.",
            "city": "Pune",
            "text": "Booked a cab for the airport run.",
            "rating": 5,
            "badge": "5/5",
        },
        {
            "name": "Meera",
            "city": None,
            "text": "Loved the hotel.",
            "rating": None,
            "badge": None,
        },
    ]


@pytest.mark.asyncio
async def test_reviews_query_is_approved_only_and_selects_no_pii() -> None:
    db = mock_db([])

    await CmsService.picker_items(db, "REVIEWS")

    statement = compiled(db)
    assert "booking_reviews.status = 'APPROVED'" in statement
    assert "IS NOT NULL" in statement
    # guest_mobile / guest_email / booking_ref_submitted were collected to
    # verify a booking, not to publish. They must never reach the SELECT list.
    for column in ("guest_mobile", "guest_email", "booking_ref_submitted"):
        assert column not in statement


@pytest.mark.asyncio
async def test_reviews_query_is_limited() -> None:
    db = mock_db([])

    await CmsService.picker_items(db, "REVIEWS")

    assert "LIMIT 30" in compiled(db)


# ── picker_items: PARTNERS ────────────────────────────────────────


@pytest.mark.asyncio
async def test_partners_picker_falls_back_to_owner_name_and_skips_blank() -> None:
    db = mock_db(
        [
            ("Skyline Travels", "Owner Name", "Bengaluru"),
            (None, "Ravi Das", None),
            ("   ", "   ", "Pune"),
        ]
    )

    payload = await CmsService.picker_items(db, "partners")

    assert payload["type"] == "PARTNERS"
    # `partners` is a string[] in the renderer, so `name` is all the band uses.
    assert payload["items"] == [
        {"name": "Skyline Travels", "city": "Bengaluru"},
        {"name": "Ravi Das", "city": ""},
    ]


@pytest.mark.asyncio
async def test_partners_query_excludes_deleted_and_unapproved() -> None:
    db = mock_db([])

    await CmsService.picker_items(db, "PARTNERS")

    statement = compiled(db)
    assert "partners.status IN ('APPROVED', 'ACTIVE')" in statement
    assert "partners.deleted_at IS NULL" in statement
    assert "LIMIT 40" in statement


# ── picker_items: SERVICES (existing behaviour, now contract-tested) ──


@pytest.mark.asyncio
async def test_services_picker_maps_to_the_items_rows() -> None:
    db = mock_db(
        [
            SimpleNamespace(label="Airport Ride", description="Door to door", type_code="CAB"),
            SimpleNamespace(label="Hotel Booking", description=None, type_code="HOTEL"),
        ]
    )

    payload = await CmsService.picker_items(db, "SERVICES")

    assert payload["type"] == "SERVICES"
    assert [i["title"] for i in payload["items"]] == ["Airport Ride", "Hotel Booking"]
    assert payload["items"][1]["desc"] == ""


# ── picker_items: unknown type is a 400, not an empty list ────────


@pytest.mark.asyncio
@pytest.mark.parametrize("picker_type", ["BOGUS", "", None, "HOTEL"])
async def test_unknown_picker_type_raises_validation_exception(picker_type) -> None:
    db = mock_db([])

    with pytest.raises(ValidationException) as excinfo:
        await CmsService.picker_items(db, picker_type)

    assert excinfo.value.status_code == 400
    assert excinfo.value.code == "VALIDATION_ERROR"
    db.execute.assert_not_awaited()


# ── validate_content ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "section_key,content",
    [
        ("HERO", {"marquee_items": [{"label": "Airport Rides"}]}),
        ("SERVICES", {"items": [{"title": "Cabs", "desc": "Door to door"}]}),
        ("STATS", {"stats": [{"value": 1200, "label": "Trips booked"}]}),
        ("PARTNERS", {"partners": ["Skyline Travels"], "cta": {"label": "Join", "href": "/partner"}}),
        ("FAQ", {"faqs": [{"question": "Q?", "answer": "A."}]}),
        ("TESTIMONIALS", {"testimonials": [{"name": "Rahul S.", "text": "Good"}]}),
    ],
)
def test_validate_content_accepts_the_editor_shape(section_key, content) -> None:
    """Whatever the typed editor writes must pass the backend gate."""
    CmsService.validate_content(section_key, content)


def test_validate_content_accepts_empty_and_none() -> None:
    CmsService.validate_content("FEATURED_HOTELS", None)
    CmsService.validate_content("FEATURED_HOTELS", {})


def test_validate_content_rejects_a_live_band_content_array() -> None:
    """FEATURED_HOTELS builds its cards from the hotel table at render time."""
    with pytest.raises(ValidationException) as excinfo:
        CmsService.validate_content("FEATURED_HOTELS", {"hotels": [{"name": "X"}]})

    assert excinfo.value.status_code == 400
    assert "content.hotels" == excinfo.value.details["field"]


def test_validate_content_lists_allowed_keys_when_the_band_has_some() -> None:
    with pytest.raises(ValidationException) as excinfo:
        CmsService.validate_content("HERO", {"wrong": 1})

    assert "Allowed keys: marquee_items." in str(excinfo.value.message)


def test_validate_content_explains_a_band_that_takes_no_keys() -> None:
    with pytest.raises(ValidationException) as excinfo:
        CmsService.validate_content("OFFERS", {"anything": 1})

    assert "takes no content keys" in str(excinfo.value.message)


@pytest.mark.parametrize("slot", ["variant_name", "headline", "cta_text"])
def test_validate_content_rejects_a_key_that_names_a_typed_column(slot) -> None:
    """A content key with a column's name overwrites the field the admin
    edited above it on the same form, because the public bundle spreads
    content over the columns."""
    assert slot in CmsService.TYPED_SLOTS
    assert slot not in CmsService.SECTION_CONTENT_KEYS["HERO"]

    with pytest.raises(ValidationException) as excinfo:
        CmsService.validate_content("HERO", {slot: "x"})

    assert "not a content key" in str(excinfo.value.message)
    assert excinfo.value.details["field"] == f"content.{slot}"


def test_validate_content_fails_open_for_an_unknown_section() -> None:
    """A band added later must not be locked out by a stale whitelist."""
    CmsService.validate_content("SOMETHING_NEW", {"whatever": [1, 2]})
