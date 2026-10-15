import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.modules.admin.public_homepage_api import (
    get_public_platform_profile,
    get_public_site_seo,
)


@pytest.fixture
def mock_db():
    result = Mock(spec=["all"])
    result.all.return_value = []
    db = SimpleNamespace(execute=AsyncMock(return_value=result))
    yield db
    db.execute.assert_awaited_once()
    result.all.assert_called_once_with()


@pytest.mark.asyncio
async def test_platform_profile_returns_trimmed_db_branding(mock_db) -> None:
    mock_db.execute.return_value.all.return_value = [
        ("PLATFORM_LOGO_URL", ""),
        ("PLATFORM_FAVICON_URL", None),
        ("PLATFORM_OG_IMAGE_URL", ""),
        ("SUPPORT_PHONE", "  +1 202 555 0100 \n"),
        ("SUPPORT_EMAIL", "\t support@example.test "),
        ("OFFICE_ADDRESS", "  123 Example Street \n"),
        ("PLATFORM_TAGLINE", "  Your next journey \t"),
        ("SOCIAL_YOUTUBE", "  https://www.youtube.com/@example \n"),
        ("SOCIAL_INSTAGRAM", "\t https://www.instagram.com/example "),
        ("SOCIAL_FACEBOOK", "  https://www.facebook.com/example  "),
        ("SOCIAL_TWITTER", "https://example.test/unapproved"),
    ]

    profile = await get_public_platform_profile(db=mock_db)

    assert profile.model_dump() == {
        "logo_url": "",
        "favicon_url": "",
        "og_image_url": "",
        "support_phone": "+1 202 555 0100",
        "support_email": "support@example.test",
        "office_address": "123 Example Street",
        "tagline": "Your next journey",
        "social_links": {
            "youtube": "https://www.youtube.com/@example",
            "instagram": "https://www.instagram.com/example",
            "facebook": "https://www.facebook.com/example",
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rows",
    [
        pytest.param([], id="missing-keys"),
        pytest.param(
            [
                ("SUPPORT_PHONE", None),
                ("SUPPORT_EMAIL", ""),
                ("OFFICE_ADDRESS", " \t\n"),
                ("PLATFORM_TAGLINE", None),
                ("SOCIAL_YOUTUBE", " \t\n"),
                ("SOCIAL_INSTAGRAM", None),
                ("SOCIAL_FACEBOOK", ""),
            ],
            id="blank-values",
        ),
    ],
)
async def test_platform_profile_does_not_fabricate_missing_branding(
    mock_db, rows
) -> None:
    mock_db.execute.return_value.all.return_value = rows

    profile = await get_public_platform_profile(db=mock_db)

    assert profile.model_dump() == {
        "logo_url": "",
        "favicon_url": "",
        "og_image_url": "",
        "support_phone": "",
        "support_email": "",
        "office_address": "",
        "tagline": "",
        "social_links": {},
    }


@pytest.mark.asyncio
async def test_site_seo_trims_metadata_and_preserves_slug_underscores(mock_db) -> None:
    mock_db.execute.return_value.all.return_value = [
        (
            "SEO_PAGE_BOOKING_INSTRUCTIONS",
            json.dumps(
                {"title": "  Booking help \n", "description": "\t How to book  "}
            ),
        ),
        ("SEO_PAGE_ABOUT", json.dumps({"title": "  About us  "})),
        ("SEO_PAGE_CONTACT", json.dumps({"description": "  Get in touch \n"})),
    ]

    assert await get_public_site_seo(db=mock_db) == {
        "booking_instructions": {
            "title": "Booking help",
            "description": "How to book",
        },
        "about": {"title": "About us", "description": ""},
        "contact": {"title": "", "description": "Get in touch"},
    }


@pytest.mark.asyncio
async def test_site_seo_returns_empty_when_no_keys_exist(mock_db) -> None:
    assert await get_public_site_seo(db=mock_db) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [
        pytest.param('{"title":', id="malformed-json"),
        pytest.param("[]", id="empty-array"),
        pytest.param('[{"title": "Not a page"}]', id="array-of-objects"),
        pytest.param('"Not an object"', id="json-string"),
        pytest.param("42", id="json-number"),
        pytest.param("true", id="json-boolean"),
        pytest.param("null", id="json-null"),
        pytest.param("{}", id="empty-object"),
        pytest.param("", id="empty-value"),
        pytest.param(None, id="null-value"),
        pytest.param(42, id="non-string-value"),
        pytest.param(
            '{"title": "  ", "description": "\\t\\n"}', id="whitespace-metadata"
        ),
    ],
)
async def test_site_seo_skips_invalid_entries_and_keeps_valid_pages(
    mock_db, value
) -> None:
    mock_db.execute.return_value.all.return_value = [
        ("SEO_PAGE_INVALID", value),
        ("SEO_PAGE_VALID", '{"title": "  Valid page  "}'),
    ]

    assert await get_public_site_seo(db=mock_db) == {
        "valid": {"title": "Valid page", "description": ""},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_value",
    [
        pytest.param(None, id="null"),
        pytest.param(123, id="integer"),
        pytest.param(1.5, id="float"),
        pytest.param(True, id="boolean"),
        pytest.param(["text"], id="array"),
        pytest.param({"text": "value"}, id="object"),
    ],
)
async def test_site_seo_checks_field_types_before_stripping(
    mock_db, invalid_value
) -> None:
    mock_db.execute.return_value.all.return_value = [
        (
            "SEO_PAGE_INVALID",
            json.dumps({"title": invalid_value, "description": invalid_value}),
        ),
        (
            "SEO_PAGE_TITLE_ONLY",
            json.dumps({"title": "  Title \n", "description": invalid_value}),
        ),
        (
            "SEO_PAGE_DESCRIPTION_ONLY",
            json.dumps({"title": invalid_value, "description": "\t Description  "}),
        ),
    ]

    assert await get_public_site_seo(db=mock_db) == {
        "title_only": {"title": "Title", "description": ""},
        "description_only": {"title": "", "description": "Description"},
    }
