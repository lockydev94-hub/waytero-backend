# Doc Ref: SEO Implementation §2 — shared slug policy
"""Shared slug validation for every SEO-facing entity (blog, hotel, tour,
hotel/vehicle categories, service types)."""

import re
import unicodedata
from typing import Any, Optional

from pydantic import field_validator
from sqlalchemy import select

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class SlugValidatorMixin:
    """Add to a Pydantic model that has an optional/required ``slug`` field."""

    @field_validator("slug")
    @classmethod
    def _validate_slug(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not SLUG_RE.match(v):
            raise ValueError(
                "slug must be lowercase letters, digits and single hyphens"
            )
        return v


def slugify(text: str) -> str:
    """ASCII-fold and collapse a display name into a URL-safe slug."""
    folded = (
        unicodedata.normalize("NFKD", text or "")
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", folded)).strip("-")


async def _slug_taken(
    db: Any, model: Any, slug: str, exclude_id: Optional[int]
) -> bool:
    q = select(model.id).where(model.slug == slug)
    if exclude_id is not None:
        q = q.where(model.id != exclude_id)
    return (await db.scalar(q.limit(1))) is not None


async def unique_slug(
    db: Any, model: Any, source: str, exclude_id: Optional[int] = None
) -> str:
    """Slug derived from ``source``, de-duplicated with -2, -3 … suffixes."""
    from app.core.exceptions import ValidationException

    base = slugify(source)
    if not base:
        raise ValidationException(
            "Could not build a URL slug from this name.", details={"field": "slug"}
        )
    candidate = base
    counter = 2
    while await _slug_taken(db, model, candidate, exclude_id):
        candidate = f"{base}-{counter}"
        counter += 1
    return candidate[:120]


async def ensure_slug_unique(
    db: Any, model: Any, slug: str, exclude_id: Optional[int] = None
) -> None:
    from app.core.exceptions import DuplicateResourceException

    if await _slug_taken(db, model, slug, exclude_id):
        raise DuplicateResourceException(model.__name__, "slug")
