# ============================================================
# WAYTERO — LANDING PAGE SERVICE
# File: app/modules/admin/services/landing_pages.py
# Doc Ref: City + Service-Type Landing Pages §Phase 1
#          Migration 0069_landing_pages
#
# Owns every rule the landing-page feature depends on:
#   • slug legality (shape, uniqueness, route-reserved, page_type agreement)
#   • copy derivation — a NULL slot renders a real sentence, never "null"
#   • price derivation — from the actual pricing tables, in Decimal
#   • the public bundle, which is one fetch per page
#
# Never commits: app.core.database.get_db owns the transaction. Raises
# WayTeroException subclasses only; the exception handler builds the envelope.
# ============================================================

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    ResourceNotFoundException,
    ValidationException,
)
from app.modules.admin.constants.landing_pages import (
    CITY_PAGE_TYPES,
    CURATED_SLOTS,
    PAGE_TYPE_PREFIX,
    PAGE_TYPE_SERVICE,
    PAGE_TYPES,
    PAGE_TYPE_URL_PATTERNS,
    PLACE_CABS,
    PLACE_PAGE_TYPES,
    SLOT_SPECS,
    block_allowed,
    curated_for,
    default_blocks,
    is_reserved_slug,
)
from app.modules.admin.models import CmsAuditVersion, LandingPage
from app.modules.master.models import City, Place, State
from app.shared.utilities.seo import SLUG_RE, ensure_slug_unique, slugify

# Kept as module-level aliases so the call sites below read as domain
# vocabulary rather than import plumbing.
_CITY_PAGE_TYPES = CITY_PAGE_TYPES
_PLACE_PAGE_TYPES = PLACE_PAGE_TYPES
_PAGE_TYPE_SERVICE = PAGE_TYPE_SERVICE
_PAGE_TYPE_PREFIX = PAGE_TYPE_PREFIX
_PAGE_TYPES = PAGE_TYPES
_SLOT_SPECS = SLOT_SPECS
_CURATED_SLOTS = CURATED_SLOTS
_block_allowed = block_allowed
_default_blocks = default_blocks
_curated_for = curated_for
_is_reserved_slug = is_reserved_slug

# The fare matrix is a marketing teaser, not a quote: one fixed sample
# distance keeps every page's numbers comparable and cacheable. Anything
# that must be charged is quoted by the booking engine, never here.
SAMPLE_DISTANCE_KM = 10
MAX_FARE_ROWS = 30
CAB_TRIP_ORDER = ("LOCAL", "AIRPORT", "OUTSTATION", "ONE_WAY", "ROUND_TRIP")

ACTIVE = "ACTIVE"

# Which price a page may claim in its own subheadline. A hotel page that
# quotes a tour price is making a promise the page cannot honour.
_PRICE_CLAUSES: Dict[str, Tuple[str, ...]] = {
    "CITY_HUB": ("hotel", "tour", "cab"),
    "CITY_CABS": ("cab",),
    "CITY_HOTELS": ("hotel",),
    "CITY_TOURS": ("tour",),
    "STATE_HOTELS": ("hotel",),
    "STATE_TOURS": ("tour",),
    "PLACE_CABS": ("cab",),
}

# Which stat chips a page type may show, in display order.
_STAT_KEYS: Dict[str, Tuple[str, ...]] = {
    "CITY_HUB": (
        "hotel_count",
        "tour_count",
        "hotel_from",
        "tour_from",
        "cab_from",
        "avg_hotel_rating",
    ),
    "CITY_CABS": ("cab_from", "vehicle_count"),
    "PLACE_CABS": ("cab_from", "vehicle_count"),
    "CITY_HOTELS": ("hotel_count", "hotel_from", "avg_hotel_rating"),
    "STATE_HOTELS": ("hotel_count", "hotel_from", "avg_hotel_rating"),
    "CITY_TOURS": ("tour_count", "tour_from"),
    "STATE_TOURS": ("tour_count", "tour_from"),
}

# Vertical copy keyed to page_type — used when a slot is NULL.
# On PLACE_CABS pages {city} carries the place name (the scope) and {region}
# the owning city — see derive_copy's kwargs.
_HEADLINES = {
    "CITY_HUB": "{city} — cabs, hotels & tour packages",
    "CITY_CABS": "Cab Booking in {city}",
    "CITY_HOTELS": "Hotel Booking in {city}",
    "CITY_TOURS": "Tour Packages in {city}",
    "STATE_HOTELS": "Hotel Booking in {state}",
    "STATE_TOURS": "Tour Packages in {state}",
    "PLACE_CABS": "{city} Cab Booking — Cabs in {region}",
}

_EYEBROWS = {
    "CITY_HUB": "WayTero {region}",
    "CITY_CABS": "Cabs in {region}",
    "CITY_HOTELS": "Hotels in {region}",
    "CITY_TOURS": "Tours in {region}",
    "STATE_HOTELS": "Hotels in {region}",
    "STATE_TOURS": "Tours in {region}",
    "PLACE_CABS": "Cabs at {region}",
}

_CTA = {
    "CITY_HUB": ("Explore {city}", "/hotels?city_id={city_id}"),
    "CITY_CABS": ("Book a cab in {city}", "/cabs?city_id={city_id}&trip_type=LOCAL"),
    "CITY_HOTELS": ("See hotels in {city}", "/hotels?city_id={city_id}"),
    "CITY_TOURS": ("Browse {city} tours", "/tours?destination={city}"),
    "STATE_HOTELS": ("See hotels in {state}", "/hotels"),
    "STATE_TOURS": ("Browse {state} tours", "/tours"),
    "PLACE_CABS": (
        "Book a cab to {city}",
        "/cabs?city_id={city_id}&trip_type=AIRPORT",
    ),
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _pretty(name: Optional[str]) -> str:
    """Display form of a city/state name — title-cased, dashes collapsed."""
    value = (name or "").strip().replace("-", " ")
    if not value:
        return ""
    if value.isupper() and len(value) > 3:
        return value.title()
    return value[:1].upper() + value[1:]


def _money(value: Any) -> Optional[float]:
    """Decimal in, 2-dp float out for the JSON edge. Never float arithmetic."""
    if value is None:
        return None
    try:
        return float(Decimal(str(value)).quantize(Decimal("0.01")))
    except Exception:
        return None


def _clean_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped or None


class LandingPageService:
    # ══════════════════════════════════════════════════════════════════
    # Admin reads
    # ══════════════════════════════════════════════════════════════════

    @staticmethod
    async def list_pages(
        db: AsyncSession,
        *,
        page_type: Optional[str] = None,
        city_id: Optional[int] = None,
        state_id: Optional[int] = None,
        search: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Admin list — rows joined to their scope names, newest edit first.

        ``search`` matches the slug or the scope name, which is how marketing
        finds "the Bhubaneswar cab page" without knowing its id.
        """
        base = (
            select(
                LandingPage,
                City.name.label("city_name"),
                State.name.label("state_name"),
                Place.name.label("place_name"),
            )
            .outerjoin(City, City.id == LandingPage.city_id)
            .outerjoin(State, State.id == LandingPage.state_id)
            .outerjoin(Place, Place.id == LandingPage.place_id)
        )
        count_base = (
            select(func.count(LandingPage.id))
            .outerjoin(City, City.id == LandingPage.city_id)
            .outerjoin(State, State.id == LandingPage.state_id)
        )

        conditions = []
        if page_type:
            conditions.append(LandingPage.page_type == page_type)
        if city_id is not None:
            conditions.append(LandingPage.city_id == city_id)
        if state_id is not None:
            conditions.append(LandingPage.state_id == state_id)
        if status == "published":
            conditions.append(LandingPage.is_published.is_(True))
        elif status == "draft":
            conditions.append(LandingPage.is_published.is_(False))
        if search and search.strip():
            term = f"%{search.strip()}%"
            conditions.append(
                or_(
                    LandingPage.slug.ilike(term),
                    LandingPage.display_name.ilike(term),
                    City.name.ilike(term),
                    State.name.ilike(term),
                )
            )

        if conditions:
            base = base.where(and_(*conditions))
            count_base = count_base.where(and_(*conditions))

        total = (await db.execute(count_base)).scalar() or 0
        rows = (
            await db.execute(
                base.order_by(
                    LandingPage.display_order, LandingPage.page_type, LandingPage.slug
                )
                .limit(limit)
                .offset(offset)
            )
        ).all()

        items: List[Dict[str, Any]] = []
        for page, city_name, state_name, place_name in rows:
            items.append(
                await LandingPageService.to_admin_dict(
                    db,
                    page,
                    city_name=city_name,
                    state_name=state_name,
                    place_name=place_name,
                )
            )
        return items, int(total)

    @staticmethod
    async def get_page(db: AsyncSession, page_id: int) -> Optional[LandingPage]:
        return await db.get(LandingPage, page_id)

    @staticmethod
    async def get_page_dict(db: AsyncSession, page_id: int) -> Dict[str, Any]:
        page = await db.get(LandingPage, page_id)
        if not page:
            raise ResourceNotFoundException("LandingPage", page_id)
        city_name, state_name = await LandingPageService._scope_names(
            db, page.city_id, page.state_id
        )
        return await LandingPageService.to_admin_dict(
            db, page, city_name=city_name, state_name=state_name
        )

    # ══════════════════════════════════════════════════════════════════
    # Writes
    # ══════════════════════════════════════════════════════════════════

    @staticmethod
    async def create(
        db: AsyncSession, payload, actor_id: Optional[str] = None
    ) -> Dict[str, Any]:
        page_type = payload.page_type
        city_id = payload.city_id
        state_id = payload.state_id

        # Place pages exist exactly where their place does — slugs, scope and
        # cross-sell blocks are derived from the Places registry, so a
        # hand-created row could only drift from it.
        if page_type in _PLACE_PAGE_TYPES:
            raise ValidationException(
                "place pages are provisioned from Places master data, "
                "not created here",
                details={"field": "page_type"},
            )

        scope_name = await LandingPageService._require_scope(
            db, page_type, city_id, state_id
        )
        slug = _clean_str(payload.slug) or LandingPageService.derive_slug(
            page_type, scope_name
        )
        await LandingPageService.validate_write(
            db,
            slug=slug,
            page_type=page_type,
            city_id=city_id,
            state_id=state_id,
            exclude_id=None,
            slug_confirmed=True,
            blocks=payload.blocks,
        )

        page = LandingPage(
            slug=slug,
            page_type=page_type,
            city_id=city_id,
            state_id=state_id,
            service_type=_PAGE_TYPE_SERVICE[page_type],
            display_name=payload.display_name or scope_name,
            is_published=bool(payload.is_published),
            created_by_user_id=_as_uuid(actor_id),
            updated_by_user_id=_as_uuid(actor_id),
            created_at=_utcnow(),
            updated_at=_utcnow(),
        )
        LandingPageService._apply(page, payload)
        db.add(page)
        await db.flush()
        await db.refresh(page)

        LandingPageService._audit(
            db,
            entity_id=page.id,
            action_type="CREATED",
            new_value={"slug": page.slug, "page_type": page.page_type},
            actor_id=actor_id,
        )
        await db.flush()

        city_name, state_name = await LandingPageService._scope_names(
            db, page.city_id, page.state_id
        )
        return await LandingPageService.to_admin_dict(
            db, page, city_name=city_name, state_name=state_name
        )

    @staticmethod
    async def update(
        db: AsyncSession, page_id: int, payload, actor_id: Optional[str] = None
    ) -> Dict[str, Any]:
        page = await db.get(LandingPage, page_id)
        if not page:
            raise ResourceNotFoundException("LandingPage", page_id)

        data = payload.model_dump(exclude_unset=True)
        page_type = data.get("page_type", page.page_type)
        city_id = data.get("city_id", page.city_id)
        state_id = data.get("state_id", page.state_id)

        # page_type is immutable once published: it defines the URL, so
        # changing it would silently republish a different story under a
        # live, indexed slug.
        if page_type != page.page_type and page.is_published:
            raise ValidationException(
                "page_type cannot be changed on a published page — unpublish "
                "it first, or create a new page",
                details={"field": "page_type"},
            )
        if page.page_type in _PLACE_PAGE_TYPES or page_type in _PLACE_PAGE_TYPES:
            if page_type != page.page_type or (
                (city_id, state_id) != (page.city_id, page.state_id)
            ):
                raise ValidationException(
                    "place pages keep their place — edit the Place row instead",
                    details={"field": "page_type"},
                )
        if (city_id, state_id) != (page.city_id, page.state_id) and page.is_published:
            raise ValidationException(
                "the city/state scope cannot be changed on a published page",
                details={"field": "city_id"},
            )

        new_slug = _clean_str(data.get("slug")) or page.slug
        if (
            page_type != page.page_type
            or city_id != page.city_id
            or state_id != page.state_id
        ):
            scope_name = await LandingPageService._require_scope(
                db, page_type, city_id, state_id
            )
            # A re-typed page keeps its slug only if the owner confirmed it;
            # otherwise the prefix would lie about what the page is.
            if new_slug == page.slug and not payload.slug_confirmed:
                new_slug = LandingPageService.derive_slug(page_type, scope_name)
        else:
            scope_name = None

        await LandingPageService.validate_write(
            db,
            slug=new_slug,
            page_type=page_type,
            city_id=city_id,
            state_id=state_id,
            exclude_id=page.id,
            slug_confirmed=payload.slug_confirmed,
            blocks=data.get("blocks"),
        )

        previous = {
            "slug": page.slug,
            "is_published": page.is_published,
            "headline": page.headline,
        }

        page.page_type = page_type
        page.city_id = city_id
        page.state_id = state_id
        page.service_type = _PAGE_TYPE_SERVICE[page_type]
        page.slug = new_slug
        if scope_name and not _clean_str(data.get("display_name")):
            page.display_name = scope_name
        LandingPageService._apply(page, payload)
        page.updated_by_user_id = _as_uuid(actor_id)
        page.updated_at = _utcnow()
        await db.flush()
        await db.refresh(page)

        LandingPageService._audit(
            db,
            entity_id=page.id,
            action_type="UPDATED",
            previous_value=previous,
            new_value={"slug": page.slug, "is_published": page.is_published},
            actor_id=actor_id,
        )
        await db.flush()

        city_name, state_name = await LandingPageService._scope_names(
            db, page.city_id, page.state_id
        )
        return await LandingPageService.to_admin_dict(
            db, page, city_name=city_name, state_name=state_name
        )

    @staticmethod
    async def delete(
        db: AsyncSession, page_id: int, actor_id: Optional[str] = None
    ) -> None:
        page = await db.get(LandingPage, page_id)
        if not page:
            raise ResourceNotFoundException("LandingPage", page_id)
        slug = page.slug
        await db.delete(page)
        LandingPageService._audit(
            db,
            entity_id=page_id,
            action_type="DELETED",
            previous_value={"slug": slug},
            actor_id=actor_id,
        )
        await db.flush()

    @staticmethod
    async def set_published(
        db: AsyncSession,
        page_id: int,
        is_published: bool,
        *,
        confirmed: bool = False,
        actor_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        page = await db.get(LandingPage, page_id)
        if not page:
            raise ResourceNotFoundException("LandingPage", page_id)

        if is_published:
            problems = await LandingPageService.publish_blockers(db, page)
            if problems and not confirmed:
                raise ValidationException(
                    "This page is not ready to publish.", details={"problems": problems}
                )

        page.is_published = is_published
        page.updated_by_user_id = _as_uuid(actor_id)
        page.updated_at = _utcnow()
        await db.flush()
        await db.refresh(page)

        LandingPageService._audit(
            db,
            entity_id=page.id,
            action_type="PUBLISHED" if is_published else "UNPUBLISHED",
            previous_value=None,
            new_value={"is_published": is_published},
            actor_id=actor_id,
        )
        await db.flush()

        city_name, state_name = await LandingPageService._scope_names(
            db, page.city_id, page.state_id
        )
        return await LandingPageService.to_admin_dict(
            db, page, city_name=city_name, state_name=state_name
        )

    @staticmethod
    async def publish_blockers(db: AsyncSession, page: LandingPage) -> List[str]:
        """What a human must fix before this page is worth indexing."""
        blockers: List[str] = []
        if not page.slug:
            blockers.append("slug is empty")

        blocks = (
            page.blocks
            if isinstance(page.blocks, list)
            else _default_blocks(page.page_type)
        )
        for index, block in enumerate(blocks):
            btype = block.get("type")
            if btype == "faq":
                faqs = page.faqs if isinstance(page.faqs, list) else []
                if not faqs:
                    blockers.append(f"block {index} (faq) has no questions")
            if not _block_allowed(page.page_type, btype or ""):
                blockers.append(
                    f"block {index} ({btype}) is not valid on a {page.page_type} page"
                )
        if isinstance(page.faqs, list) and len(page.faqs) > 0 and not blocks:
            if not any(b.get("type") == "faq" for b in blocks):
                blockers.append("faqs are authored but no faq block renders them")
        return blockers

    # ══════════════════════════════════════════════════════════════════
    # Validation
    # ══════════════════════════════════════════════════════════════════

    @staticmethod
    async def validate_write(
        db: AsyncSession,
        *,
        slug: str,
        page_type: str,
        city_id: Optional[int],
        state_id: Optional[int],
        exclude_id: Optional[int],
        slug_confirmed: bool,
        blocks: Optional[List[Dict[str, Any]]],
    ) -> None:
        if page_type not in _PAGE_TYPES:
            raise ValidationException(
                f"unknown page_type {page_type!r}", details={"field": "page_type"}
            )
        if page_type in _CITY_PAGE_TYPES:
            if not city_id or state_id:
                raise ValidationException(
                    f"{page_type} requires city_id and no state_id",
                    details={"field": "city_id"},
                )
        elif not state_id or city_id:
            raise ValidationException(
                f"{page_type} requires state_id and no city_id",
                details={"field": "state_id"},
            )

        if not SLUG_RE.match(slug or ""):
            raise ValidationException(
                "slug must be lowercase letters, digits and single hyphens",
                details={"field": "slug"},
            )
        if _is_reserved_slug(slug):
            raise ValidationException(
                f"'{slug}' is reserved by an existing website route",
                details={"field": "slug", "reserved": True},
            )
        await ensure_slug_unique(db, LandingPage, slug, exclude_id)

        # The slug is the whole URL, so it has to keep saying what the page is.
        pattern = _URL_PATTERNS[page_type]
        if not pattern.match(slug) and not slug_confirmed:
            raise ValidationException(
                "slug does not match this page type's URL pattern",
                details={"field": "slug", "expected": pattern.pattern},
            )

        scope_row = await LandingPageService._require_scope(
            db, page_type, city_id, state_id
        )
        if page_type in _CITY_PAGE_TYPES:
            derived = LandingPageService.derive_slug(page_type, scope_row)
            if slug != derived and not slug_confirmed:
                raise ValidationException(
                    f"slug is not the derived form '{derived}'. Send "
                    "slug_confirmed=true to override.",
                    details={"field": "slug", "derived": derived},
                )

        if blocks is not None:
            for index, block in enumerate(blocks):
                btype = block.get("type")
                if not _block_allowed(page_type, btype or ""):
                    raise ValidationException(
                        f"block {index}: {btype} is not valid on a {page_type} page",
                        details={"field": "blocks"},
                    )

    # ══════════════════════════════════════════════════════════════════
    # Slug / copy derivation
    # ══════════════════════════════════════════════════════════════════

    @staticmethod
    def derive_slug(page_type: str, scope_name: Optional[str]) -> str:
        base = slugify(scope_name or "")
        if not base:
            raise ValidationException(
                "could not build a URL slug from this city/state name",
                details={"field": "slug"},
            )
        slug = f"{_PAGE_TYPE_PREFIX.get(page_type, '')}{base}"
        if len(slug) > 120:
            raise ValidationException(
                "generated slug is longer than 120 characters — set a shorter "
                "display name or override the slug",
                details={"field": "slug", "length": len(slug)},
            )
        return slug

    @staticmethod
    def derive_place_slug(city_name: Optional[str], place_name: Optional[str]) -> str:
        """
        Keyword-first place-page slug: ``{city}-{place}-cab``.

        The city stays the head token (it is the search qualifier and keeps
        is_reserved_slug's head check meaningful), and the place part drops
        the duplicated city prefix when the place name already starts with it
        ("Bhubaneswar Railway Station" → bhubaneswar-railway-station-cab).
        """
        city_slug = slugify(city_name or "")
        place_slug = slugify(place_name or "")
        if not place_slug:
            raise ValidationException(
                "could not build a URL slug from this place name",
                details={"field": "slug"},
            )
        if city_slug and place_slug.startswith(city_slug + "-"):
            place_slug = place_slug[len(city_slug) + 1 :]
        base = f"{city_slug}-{place_slug}" if city_slug else place_slug
        slug = f"{base}-cab"
        if len(slug) > 120:
            raise ValidationException(
                "generated slug is longer than 120 characters — set a shorter "
                "place name or override the slug",
                details={"field": "slug", "length": len(slug)},
            )
        return slug

    @staticmethod
    async def derive_copy(
        db: AsyncSession,
        page: LandingPage,
        *,
        scope_name: str,
        region_name: str,
        prices: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Fill every NULL slot with something true.

        A price only appears in the template when the pricing tables actually
        produced one — the copy never claims "from ₹999" on a city with no
        priced supply.
        """
        pretty = _pretty(scope_name)
        prices = prices or {}
        # A page only advertises the price it can actually honour. A hotel
        # page quoting a tour price would be a claim the page cannot back up.
        clause_map = {
            "hotel": ("hotel_from", "hotels from"),
            "tour": ("tour_from", "tour packages from"),
            "cab": ("cab_from", "cabs from"),
        }
        wanted = _PRICE_CLAUSES.get(page.page_type, ())
        price_clauses: List[str] = []
        for key in wanted:
            field, label = clause_map[key]
            if prices.get(field) is not None:
                price_clauses.append(f"{label} {_rupiah(prices[field])}")
        price_line = " · ".join(price_clauses)
        # The line is appended as its own sentence, so it needs a capital —
        # "...instant booking. hotels from ₹4,500" reads like a template bug.
        if price_line:
            price_line = price_line[0].upper() + price_line[1:]

        headline = _clean_str(page.headline) or _HEADLINES[page.page_type].format(
            city=pretty, state=pretty, region=_pretty(region_name) or pretty
        )
        eyebrow = _clean_str(page.eyebrow) or _EYEBROWS[page.page_type].format(
            region=_pretty(region_name) or pretty
        )
        subheadline = _clean_str(page.subheadline) or _default_subheadline(
            page.page_type, pretty, price_line
        )
        body_text = _clean_str(page.body_text) or _default_body(page.page_type, pretty)

        seo_title = _clean_str(page.seo_title) or headline[:120]
        # subheadline already carries the price line, so appending it again
        # would read "...cabs from ₹210. cabs from ₹210".
        seo_description = (
            _clean_str(page.seo_description)
            or (
                subheadline
                if price_line in subheadline
                else f"{subheadline} {price_line}".strip()
            )[:320]
        )
        if page.seo_keywords and page.seo_keywords.strip():
            keywords = _keyword_union(page.seo_keywords)
        else:
            keywords = _default_keywords(page.page_type, pretty, region_name)

        cta_text, cta_link = _CTA[page.page_type]
        return {
            "eyebrow": eyebrow,
            "headline": headline,
            "subheadline": subheadline,
            "body_text": body_text,
            "cta_text": _clean_str(page.cta_text)
            or cta_text.format(city=pretty, state=pretty, city_id=page.city_id or ""),
            "cta_link": _clean_str(page.cta_link)
            or cta_link.format(city=pretty, state=pretty, city_id=page.city_id or ""),
            "cta2_text": _clean_str(page.cta2_text),
            "cta2_link": _clean_str(page.cta2_link),
            "seo_title": seo_title,
            "seo_description": seo_description,
            "seo_keywords": keywords,
            "canonical_path": f"/{page.slug}",
        }

    # ══════════════════════════════════════════════════════════════════
    # Price derivation — Decimal all the way, float only at the JSON edge
    # ══════════════════════════════════════════════════════════════════

    @staticmethod
    async def derive_prices(db: AsyncSession, page: LandingPage) -> Dict[str, Any]:
        city_id, state_id = page.city_id, page.state_id
        out: Dict[str, Any] = {
            "hotel_from": None,
            "tour_from": None,
            "cab_from": None,
            "hotel_count": 0,
            "tour_count": 0,
            "vehicle_count": 0,
            "avg_hotel_rating": None,
            "currency": "INR",
        }

        # State scope walks cities.state_id: hotels.state_id is nullable, while
        # every hotel has a city and every city has a state — so this is the
        # count that is actually true.
        hotel_scope = "h.city_id = :scope_id" if city_id else "c.state_id = :scope_id"
        tour_scope = "tp.city_id = :scope_id" if city_id else "tc.state_id = :scope_id"
        scope_id = city_id if city_id else state_id

        hotel_row = (
            (
                await db.execute(
                    text(
                        f"""
                SELECT COUNT(DISTINCT h.id) AS hotel_count,
                       MIN(rc.base_price)   AS from_price,
                       AVG(r.average_rating) AS avg_rating
                  FROM hotels h
                  JOIN cities c            ON c.id = h.city_id
             LEFT JOIN hotel_room_categories rc
                    ON rc.hotel_id = h.id AND rc.is_active = TRUE
                       AND rc.base_price > 0
             LEFT JOIN hotel_ratings r      ON r.hotel_id = h.id
                 WHERE h.status = '{ACTIVE}'
                   AND h.deleted_at IS NULL
                   AND {hotel_scope}
                """
                    ),
                    {"scope_id": scope_id},
                )
            )
            .mappings()
            .first()
        )
        if hotel_row:
            out["hotel_count"] = int(hotel_row["hotel_count"] or 0)
            out["hotel_from"] = _money(hotel_row["from_price"])
            out["avg_hotel_rating"] = (
                round(float(hotel_row["avg_rating"]), 2)
                if hotel_row["avg_rating"] is not None
                else None
            )

        tour_row = (
            (
                await db.execute(
                    text(
                        f"""
                SELECT COUNT(DISTINCT tp.id) AS tour_count,
                       MIN(p.package_price)  AS from_price
                  FROM tour_packages tp
                  JOIN cities tc ON tc.id = tp.city_id
             LEFT JOIN tour_package_pricing p ON p.package_id = tp.id
                 WHERE tp.status = '{ACTIVE}'
                   AND {tour_scope}
                """
                    ),
                    {"scope_id": scope_id},
                )
            )
            .mappings()
            .first()
        )
        if tour_row:
            out["tour_count"] = int(tour_row["tour_count"] or 0)
            out["tour_from"] = _money(tour_row["from_price"])

        if page.page_type in ("CITY_CABS", PLACE_CABS):
            # A place page leads with the rides a traveller at an airport or
            # station actually books; if the city prices none of those, the
            # full matrix is shown rather than an empty teaser.
            place_filter = (
                ("AIRPORT", "ONE_WAY", "ROUND_TRIP")
                if page.page_type == PLACE_CABS
                else None
            )
            fares = await LandingPageService.cab_fare_matrix(
                db, int(city_id or 0), trip_types_filter=place_filter
            )
            out["cab_fares"] = fares["rows"]
            out["cab_from"] = fares["fare_from"]
            out["vehicle_count"] = fares["vehicle_count"]
        return out

    @staticmethod
    async def cab_fare_matrix(
        db: AsyncSession,
        city_id: int,
        distance_km: Optional[float] = None,
        max_rows: int = MAX_FARE_ROWS,
        trip_types_filter: Optional[Tuple[str, ...]] = None,
    ) -> Dict[str, Any]:
        """
        vehicle_categories × bookable trip types, priced through the real fare
        engine — the same rule resolution the booking flow uses (city-specific
        rule first, platform default second), so a page can never quote a price
        the platform would not charge.

        Both rule tables are read in two queries up front. Resolving per pair
        would be one query per cell (categories × trip types), which for a
        12-vehicle platform is ~60 round trips on a marketing page.

        `distance_km` overrides the 10 km sample; the landing pages always use
        the default so their quoted figures stay comparable across cities.
        """
        from app.modules.booking.services.fare import calculate_fare_breakdown

        sample = float(distance_km) if distance_km else float(SAMPLE_DISTANCE_KM)

        categories = (
            (
                await db.execute(
                    text(
                        """
                SELECT id, category_name, seating_capacity, luggage_capacity,
                       image_url, icon_url
                  FROM vehicle_categories
                 WHERE is_active = TRUE
                 ORDER BY display_order ASC, id ASC
                """
                    )
                )
            )
            .mappings()
            .all()
        )

        city_rules = (
            (
                await db.execute(
                    text(
                        """
                SELECT vehicle_category_id, trip_type, base_fare, minimum_km,
                       per_km_rate, driver_allowance, driver_allowance_type,
                       night_charge, night_charge_type, toll,
                       free_waiting_minutes, waiting_rate_per_hour,
                       waiting_granularity
                  FROM vehicle_pricing_rules
                 WHERE city_id = :city_id
                """
                    ),
                    {"city_id": city_id},
                )
            )
            .mappings()
            .all()
        )
        default_rules = (
            (
                await db.execute(
                    text(
                        """
                SELECT vehicle_category_id, trip_type, base_fare, minimum_km,
                       per_km_rate, driver_allowance, driver_allowance_type,
                       night_charge, night_charge_type, toll,
                       free_waiting_minutes, waiting_rate_per_hour,
                       waiting_granularity
                  FROM default_vehicle_pricing_rules
                """
                    )
                )
            )
            .mappings()
            .all()
        )

        by_key: Dict[Tuple[int, str], Dict[str, Any]] = {}
        for rule in default_rules:
            by_key[(rule["vehicle_category_id"], rule["trip_type"])] = {
                **dict(rule),
                "source": "default",
            }
        for rule in city_rules:
            by_key[(rule["vehicle_category_id"], rule["trip_type"])] = {
                **dict(rule),
                "source": "city_specific",
            }

        trip_types = sorted(
            {key[1] for key in by_key if key[1]},
            key=lambda code: (
                CAB_TRIP_ORDER.index(code) if code in CAB_TRIP_ORDER else 99
            ),
        )
        if trip_types_filter:
            filtered = [code for code in trip_types if code in trip_types_filter]
            trip_types = filtered or trip_types  # never show an empty teaser

        rows: List[Dict[str, Any]] = []
        included_types: List[str] = []
        # Whole trip-type groups only: a truncated group leaves a vehicle
        # missing from that tab and lets "cab from" quote a price the table
        # itself cannot show.
        for code in trip_types:
            group: List[Dict[str, Any]] = []
            for cat in categories:
                rule = by_key.get((cat["id"], code))
                if not rule:
                    continue
                breakdown = calculate_fare_breakdown(
                    rule=rule,
                    actual_distance_km=sample,
                    trip_type=code,
                )
                group.append(
                    {
                        "vehicle_category_id": cat["id"],
                        "vehicle_category": cat["category_name"],
                        "seats": cat["seating_capacity"],
                        "luggage": cat["luggage_capacity"],
                        "image_url": cat["image_url"],
                        "icon_url": cat["icon_url"],
                        "trip_type": code,
                        "base_fare": _money(rule.get("base_fare")),
                        "per_km_rate": _money(rule.get("per_km_rate")),
                        "minimum_km": int(rule.get("minimum_km") or 0),
                        "sample_distance_km": sample,
                        "estimated_total": _money(breakdown["total"]),
                        "source": rule.get("source") or "default",
                    }
                )
            if not group:
                continue
            if rows and len(rows) + len(group) > max_rows:
                break
            rows.extend(group)
            included_types.append(code)

        totals = [
            Decimal(str(r["estimated_total"]))
            for r in rows
            if r["estimated_total"] is not None
        ]
        fare_from = min(totals) if totals else None

        return {
            "rows": rows,
            "fare_from": _money(fare_from),
            "vehicle_count": len({r["vehicle_category_id"] for r in rows}),
            "trip_types": included_types,
            "sample_distance_km": sample,
        }

    @staticmethod
    async def gst_config(db: AsyncSession) -> Dict[str, Any]:
        rows = (
            (
                await db.execute(
                    text(
                        "SELECT config_key, config_value FROM system_configurations "
                        "WHERE config_key IN ('GST_ENABLED', 'GST_RATE')"
                    )
                )
            )
            .mappings()
            .all()
        )
        values = {r["config_key"]: r["config_value"] for r in rows}
        enabled = str(values.get("GST_ENABLED", "false")).strip().lower() == "true"
        rate: Optional[float] = None
        try:
            rate = float(Decimal(str(values.get("GST_RATE") or "0")))
        except Exception:
            rate = None
        return {"enabled": enabled, "rate_pct": rate}

    # ══════════════════════════════════════════════════════════════════
    # Vertical data for blocks
    # ══════════════════════════════════════════════════════════════════

    @staticmethod
    async def hotels_for(
        db: AsyncSession,
        page: LandingPage,
        limit: int = 9,
        place_tokens: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        scope = "h.city_id = :scope_id" if page.city_id else "ci.state_id = :scope_id"
        # On a place page a hotel only counts as supply when its own record
        # mentions the place — landmark/name text, not vibes. Zero matches and
        # the grid block is stripped from the payload rather than falling back
        # to unrelated city hotels.
        place_clause = ""
        params: Dict[str, Any] = {
            "scope_id": page.city_id or page.state_id,
            "limit": limit,
        }
        if place_tokens:
            ors = []
            for i, tok in enumerate(place_tokens):
                params[f"tok{i}"] = f"%{tok}%"
                ors.append(f"h.landmark ILIKE :tok{i} OR h.hotel_name ILIKE :tok{i}")
            place_clause = " AND (" + " OR ".join(ors) + ")"
        rows = (
            (
                await db.execute(
                    text(
                        f"""
                SELECT h.id, h.slug, h.hotel_name, h.star_rating, h.is_featured,
                       ci.name AS city_name,
                       COALESCE((SELECT MIN(rc.base_price) FROM hotel_room_categories rc
                                  WHERE rc.hotel_id = h.id AND rc.is_active = TRUE
                                    AND rc.base_price > 0), 0) AS starting_price,
                       (SELECT image_url FROM hotel_images i
                         WHERE i.hotel_id = h.id AND i.is_primary = TRUE
                         ORDER BY i.display_order LIMIT 1) AS primary_image_url,
                       COALESCE(hr.average_rating, 0) AS average_rating,
                       COALESCE(hr.total_reviews, 0) AS total_reviews
                  FROM hotels h
                  JOIN cities ci ON ci.id = h.city_id
                  LEFT JOIN hotel_ratings hr ON hr.hotel_id = h.id
                 WHERE h.status = '{ACTIVE}' AND h.deleted_at IS NULL
                   AND {scope}
                   {place_clause}
                 ORDER BY h.is_featured DESC, hr.average_rating DESC NULLS LAST,
                          h.display_order, h.hotel_name
                 LIMIT :limit
                """
                    ),
                    params,
                )
            )
            .mappings()
            .all()
        )
        return [
            {
                "id": int(r["id"]),
                "slug": r["slug"],
                "hotel_name": r["hotel_name"],
                "star_rating": r["star_rating"],
                "city_name": r["city_name"],
                "starting_price": _money(r["starting_price"]),
                "primary_image_url": r["primary_image_url"],
                "average_rating": float(r["average_rating"] or 0),
                "total_reviews": int(r["total_reviews"] or 0),
                "is_featured": bool(r["is_featured"]),
            }
            for r in rows
        ]

    @staticmethod
    async def tours_for(
        db: AsyncSession,
        page: LandingPage,
        limit: int = 9,
        place_tokens: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        scope = "tp.city_id = :scope_id" if page.city_id else "ci.state_id = :scope_id"
        place_clause = ""
        params: Dict[str, Any] = {
            "scope_id": page.city_id or page.state_id,
            "limit": limit,
        }
        if place_tokens:
            ors = []
            for i, tok in enumerate(place_tokens):
                params[f"tok{i}"] = f"%{tok}%"
                ors.append(
                    f"tp.destination ILIKE :tok{i} OR tp.package_name ILIKE :tok{i}"
                )
            place_clause = " AND (" + " OR ".join(ors) + ")"
        rows = (
            (
                await db.execute(
                    text(
                        f"""
                SELECT tp.id, tp.slug, tp.package_name, tp.destination,
                       tp.duration_days, tp.duration_nights, tp.short_description,
                       ci.name AS city_name,
                       (SELECT m.media_url FROM tour_package_media m
                         WHERE m.package_id = tp.id
                         ORDER BY m.is_primary DESC NULLS LAST, m.id LIMIT 1)
                           AS primary_image_url,
                       (SELECT MIN(p.package_price) FROM tour_package_pricing p
                         WHERE p.package_id = tp.id) AS starting_price
                  FROM tour_packages tp
                  JOIN cities ci ON ci.id = tp.city_id
                 WHERE tp.status = '{ACTIVE}' AND {scope}
                   {place_clause}
                 ORDER BY tp.created_at DESC
                 LIMIT :limit
                """
                    ),
                    params,
                )
            )
            .mappings()
            .all()
        )
        return [
            {
                "id": int(r["id"]),
                "slug": r["slug"],
                "package_name": r["package_name"],
                "destination": r["destination"],
                "city_name": r["city_name"],
                "duration_days": r["duration_days"],
                "duration_nights": r["duration_nights"],
                "short_description": r["short_description"],
                "primary_image_url": r["primary_image_url"],
                "starting_price": _money(r["starting_price"]),
            }
            for r in rows
        ]

    @staticmethod
    async def vehicles_for(db: AsyncSession) -> List[Dict[str, Any]]:
        rows = (
            (
                await db.execute(
                    text(
                        """
                SELECT id, category_name, seating_capacity, luggage_capacity,
                       image_url, icon_url
                  FROM vehicle_categories
                 WHERE is_active = TRUE
                 ORDER BY display_order ASC, id ASC
                """
                    )
                )
            )
            .mappings()
            .all()
        )
        return [
            {
                "id": int(r["id"]),
                "category_name": r["category_name"],
                "seating_capacity": r["seating_capacity"],
                "luggage_capacity": r["luggage_capacity"],
                "image_url": r["image_url"],
                "icon_url": r["icon_url"],
            }
            for r in rows
        ]

    # ══════════════════════════════════════════════════════════════════
    # Public reads
    # ══════════════════════════════════════════════════════════════════

    @staticmethod
    async def resolve_public(db: AsyncSession, slug: str) -> Optional[LandingPage]:
        """Published and inside its schedule window — the only rows the
        website may render. An out-of-window row 404s rather than serving a
        stale campaign."""
        now = _utcnow()
        stmt = select(LandingPage).where(
            LandingPage.slug == slug,
            LandingPage.is_published.is_(True),
            or_(LandingPage.starts_at.is_(None), LandingPage.starts_at <= now),
            or_(LandingPage.ends_at.is_(None), LandingPage.ends_at > now),
        )
        return (await db.execute(stmt)).scalar_one_or_none()

    @staticmethod
    async def public_filter() -> Any:
        now = _utcnow()
        return and_(
            LandingPage.is_published.is_(True),
            or_(LandingPage.starts_at.is_(None), LandingPage.starts_at <= now),
            or_(LandingPage.ends_at.is_(None), LandingPage.ends_at > now),
        )

    @staticmethod
    async def list_public_refs(db: AsyncSession) -> List[Dict[str, Any]]:
        """The single feed behind generateStaticParams, sitemap.xml and
        llms.txt — slim on purpose so a crawl costs one cheap query."""
        rows = (
            await db.execute(
                select(
                    LandingPage,
                    City.name.label("place_city_name"),
                )
                .outerjoin(Place, Place.id == LandingPage.place_id)
                .outerjoin(City, City.id == Place.city_id)
                .where(await LandingPageService.public_filter())
                .order_by(
                    LandingPage.display_order,
                    LandingPage.page_type,
                    LandingPage.slug,
                )
            )
        ).all()
        return [
            {
                "slug": page.slug,
                "page_type": page.page_type,
                "service_type": page.service_type,
                "scope": {
                    "kind": (
                        "place"
                        if page.page_type in _PLACE_PAGE_TYPES
                        else "city" if page.city_id else "state"
                    ),
                    "display_name": page.display_name,
                    # Footer/intent labels need the city for place pages
                    # ("Bhubaneswar Airport Cabs" reads better than the
                    # full official place name).
                    **(
                        {"city_name": place_city_name}
                        if page.page_type in _PLACE_PAGE_TYPES
                        else {}
                    ),
                },
                "canonical_path": f"/{page.slug}",
                "updated_at": page.updated_at,
            }
            for page, place_city_name in rows
        ]

    @staticmethod
    async def build_public_payload(
        db: AsyncSession, page: LandingPage
    ) -> Dict[str, Any]:
        city_name, state_name = await LandingPageService._scope_names(
            db, page.city_id, page.state_id
        )
        is_place = page.page_type in _PLACE_PAGE_TYPES
        place_row = await db.get(Place, page.place_id) if page.place_id else None

        # A place page is about its place: the place is the scope, the city
        # becomes the region.
        scope_name = (
            (place_row.name if place_row else page.display_name)
            if is_place
            else city_name or state_name or page.display_name
        )
        region_name = (
            city_name if is_place else (state_name if page.city_id else scope_name)
        )

        prices = await LandingPageService.derive_prices(db, page)
        derived = await LandingPageService.derive_copy(
            db,
            page,
            scope_name=scope_name,
            region_name=region_name or "",
            prices=prices,
        )

        blocks = (
            page.blocks
            if isinstance(page.blocks, list) and page.blocks
            else _default_blocks(page.page_type)
        )
        block_types = {b.get("type") for b in blocks if isinstance(b, dict)}

        place_tokens = (
            _place_tokens(place_row.name, city_name) if is_place and place_row else None
        )
        hotels: List[Dict[str, Any]] = []
        tours: List[Dict[str, Any]] = []
        vehicles: List[Dict[str, Any]] = []
        if "hotel_grid" in block_types:
            limit = _block_limit(blocks, "hotel_grid", 9)
            hotels = await LandingPageService.hotels_for(
                db, page, limit=limit, place_tokens=place_tokens
            )
        if "tour_grid" in block_types:
            limit = _block_limit(blocks, "tour_grid", 9)
            tours = await LandingPageService.tours_for(
                db, page, limit=limit, place_tokens=place_tokens
            )
        if "vehicle_grid" in block_types:
            vehicles = await LandingPageService.vehicles_for(db)

        if is_place:
            # Cross-sell honesty: a place page only carries a hotel/tour grid
            # it can actually fill from that place's own supply. No matches →
            # the block goes away, rather than rendering a titled empty
            # section — the owner's "no fake sections" rule.
            empties = {
                btype
                for btype, rows_ in (("hotel_grid", hotels), ("tour_grid", tours))
                if not rows_
            }
            if empties:
                blocks = [
                    b
                    for b in blocks
                    if not (isinstance(b, dict) and b.get("type") in empties)
                ]

        hero, image_source = LandingPageService.resolve_hero(
            page, page.page_type, scope_name
        )
        stats = LandingPageService.resolve_stats(page, prices)
        highlights = (
            page.highlights
            if isinstance(page.highlights, list) and page.highlights
            else []
        )
        gallery = (
            page.gallery if isinstance(page.gallery, list) and page.gallery else []
        )
        faqs = page.faqs if isinstance(page.faqs, list) and page.faqs else []

        scope: Dict[str, Any] = {
            "kind": "place" if is_place else "city" if page.city_id else "state",
            "id": page.city_id or page.state_id,
            "name": scope_name,
            "display_name": _pretty(scope_name),
            "state_id": page.state_id if not page.city_id else None,
            "state_name": _pretty(state_name or "") or None,
        }
        if is_place and place_row:
            scope.update(
                {
                    "place": {
                        "id": int(place_row.id),
                        "name": place_row.name,
                        "code": place_row.code,
                        "kind": place_row.kind,
                    },
                    "city_id": page.city_id,
                    "city_name": _pretty(city_name or "") or None,
                    "latitude": (
                        float(place_row.latitude)
                        if place_row.latitude is not None
                        else None
                    ),
                    "longitude": (
                        float(place_row.longitude)
                        if place_row.longitude is not None
                        else None
                    ),
                }
            )
        if page.city_id:
            geo = await LandingPageService._city_geo(db, page.city_id)
            scope.setdefault("latitude", geo["latitude"])
            scope.setdefault("longitude", geo["longitude"])

        return {
            "slug": page.slug,
            "page_type": page.page_type,
            "service_type": page.service_type,
            "scope": scope,
            "hero": {
                "eyebrow": derived["eyebrow"],
                "headline": derived["headline"],
                "subheadline": derived["subheadline"],
                "body_text": derived["body_text"],
                "image_url": hero.get("banner_image_url"),
                "mobile_image_url": hero.get("banner_mobile_url"),
                "video_url": hero.get("banner_video_url"),
                "alt_text": _clean_str(page.banner_alt_text) or derived["headline"],
                "overlay_opacity": float(page.overlay_opacity or 0.55),
                "image_source": image_source,
            },
            "blocks": blocks,
            "highlights": highlights,
            "gallery": gallery,
            "faqs": faqs,
            "stats": stats,
            "cta": {
                "text": derived["cta_text"],
                "link": derived["cta_link"],
                "text2": derived["cta2_text"],
                "link2": derived["cta2_link"],
            },
            "seo": {
                "title": derived["seo_title"],
                "description": derived["seo_description"],
                "keywords": derived["seo_keywords"],
                "canonical_path": derived["canonical_path"],
                "og_image_url": hero.get("og_image_url"),
            },
            "prices": {
                "hotel_from": prices.get("hotel_from"),
                "tour_from": prices.get("tour_from"),
                "cab_from": prices.get("cab_from"),
                "hotel_count": prices.get("hotel_count"),
                "tour_count": prices.get("tour_count"),
                "vehicle_count": prices.get("vehicle_count"),
                "avg_hotel_rating": prices.get("avg_hotel_rating"),
                "currency": "INR",
            },
            "cab_fares": prices.get("cab_fares", []),
            "gst": await LandingPageService.gst_config(db),
            "hotels": hotels,
            "tours": tours,
            "vehicles": vehicles,
            "siblings": await LandingPageService.siblings(db, page),
            "nearby": await LandingPageService.nearby(db, page),
            "meta": {
                "updated_at": page.updated_at,
                "revalidate": 300,
                "image_source": image_source,
                "sample_distance_km": SAMPLE_DISTANCE_KM,
            },
        }

    # ══════════════════════════════════════════════════════════════════
    # Hero / stats / links
    # ══════════════════════════════════════════════════════════════════

    @staticmethod
    def resolve_hero(
        page: LandingPage, page_type: str, scope_name: Optional[str]
    ) -> Tuple[Dict[str, Optional[str]], str]:
        """
        Admin upload wins; curated art is the fallback; gradient is the last
        resort. The website needs the verdict (image_source) because a missing
        image must degrade to the gradient treatment, not a broken <Image>.
        """
        curated = _curated_for(page_type, slugify(scope_name or ""))
        resolved: Dict[str, Optional[str]] = {}
        sources: List[str] = []
        for slot in _CURATED_SLOTS:
            admin_value = _clean_str(getattr(page, slot, None))
            if admin_value:
                resolved[slot] = admin_value
                sources.append("admin")
            elif curated.get(slot):
                resolved[slot] = curated[slot]
                sources.append("curated")
            else:
                resolved[slot] = None
                sources.append("gradient")

        banner = resolved.get("banner_image_url") or resolved.get("banner_mobile_url")
        if not banner and curated.get("banner_video_url"):
            resolved["banner_video_url"] = curated["banner_video_url"]
        else:
            resolved["banner_video_url"] = _clean_str(page.banner_video_url)

        if "admin" in sources:
            return resolved, "admin"
        if "curated" in sources:
            return resolved, "curated"
        return resolved, "gradient"

    @staticmethod
    def resolve_stats(
        page: LandingPage,
        prices: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """
        Authored stats win; otherwise build the ones the data supports.

        Scoped to the page's own vertical: a cab page showing "1 Hotels
        bookable" is technically true and still noise — the visitor came for
        cabs. The hub page is the one place where all three belong.
        """
        if isinstance(page.stats, list) and page.stats:
            return page.stats

        allowed = _STAT_KEYS.get(page.page_type, ())
        candidates = {
            "hotel_count": (
                prices.get("hotel_count"),
                lambda v: {
                    "value": v,
                    "suffix": "",
                    "label": f"{_plural(v, 'Hotel', 'Hotels')} bookable",
                },
            ),
            "tour_count": (
                prices.get("tour_count"),
                lambda v: {
                    "value": v,
                    "suffix": "",
                    "label": _plural(v, "Tour package", "Tour packages"),
                },
            ),
            "hotel_from": (
                prices.get("hotel_from"),
                lambda v: {
                    "value": _rupiah(v),
                    "suffix": "",
                    "label": "Hotel price from",
                },
            ),
            "tour_from": (
                prices.get("tour_from"),
                lambda v: {"value": _rupiah(v), "suffix": "", "label": "Package from"},
            ),
            "cab_from": (
                prices.get("cab_from"),
                lambda v: {
                    "value": _rupiah(v),
                    "suffix": "",
                    "label": "Cab trip from",
                },
            ),
            "vehicle_count": (
                prices.get("vehicle_count"),
                lambda v: {
                    "value": v,
                    "suffix": "",
                    "label": _plural(v, "Vehicle option", "Vehicle options"),
                },
            ),
            "avg_hotel_rating": (
                prices.get("avg_hotel_rating"),
                lambda v: {"value": v, "suffix": "/5", "label": "Avg guest rating"},
            ),
        }

        stats: List[Dict[str, Any]] = []
        for key in allowed:
            entry = candidates.get(key)
            if not entry:
                continue
            value, build = entry
            if value:
                stats.append(build(value))
        return stats[:6]

    @staticmethod
    async def siblings(db: AsyncSession, page: LandingPage) -> List[Dict[str, Any]]:
        """The other pages for the same city/state — the internal link cluster
        that keeps a visitor inside the platform instead of back to Google."""
        if not (page.city_id or page.state_id):
            return []
        scope = (
            LandingPage.city_id == page.city_id
            if page.city_id
            else LandingPage.state_id == page.state_id
        )
        rows = (
            (
                await db.execute(
                    select(LandingPage).where(
                        scope,
                        LandingPage.id != page.id,
                        await LandingPageService.public_filter(),
                    )
                )
            )
            .scalars()
            .all()
        )
        return [
            {
                "slug": other.slug,
                "page_type": other.page_type,
                # A place sibling is worth its own name ("Bhubaneswar
                # Railway Station"), not the generic vertical label.
                "label": (
                    _pretty(other.display_name)
                    if other.page_type in _PLACE_PAGE_TYPES
                    else _sibling_label(other.page_type)
                ),
                "href": f"/{other.slug}",
            }
            for other in sorted(rows, key=lambda r: (r.display_order, r.slug))
        ]

    @staticmethod
    async def nearby(db: AsyncSession, page: LandingPage) -> List[Dict[str, Any]]:
        """Neighbouring cities in the same state that have their own hub page."""
        state_id = page.state_id
        if page.city_id:
            state_id = (
                await db.execute(select(City.state_id).where(City.id == page.city_id))
            ).scalar()
        if not state_id:
            return []

        rows = (
            await db.execute(
                select(LandingPage.slug, LandingPage.display_name, City.name)
                .join(City, City.id == LandingPage.city_id)
                .where(
                    City.state_id == state_id,
                    LandingPage.page_type == "CITY_HUB",
                    await LandingPageService.public_filter(),
                )
                .order_by(LandingPage.display_order, LandingPage.slug)
                .limit(12)
            )
        ).all()
        out = [
            {
                "slug": slug,
                "label": _pretty(name or display),
                "href": f"/{slug}",
                "relation": "nearby",
            }
            for slug, display, name in rows
            if slug != page.slug
        ]
        return out[:8]

    @staticmethod
    async def _city_geo(db: AsyncSession, city_id: int) -> Dict[str, Any]:
        row = (
            (
                await db.execute(
                    select(City.latitude, City.longitude).where(City.id == city_id)
                )
            )
            .mappings()
            .first()
        )
        if not row:
            return {"latitude": None, "longitude": None}
        return {
            "latitude": float(row["latitude"]) if row["latitude"] is not None else None,
            "longitude": (
                float(row["longitude"]) if row["longitude"] is not None else None
            ),
        }

    # ══════════════════════════════════════════════════════════════════
    # Provisioning
    # ══════════════════════════════════════════════════════════════════

    @staticmethod
    async def sync_for_city(
        db: AsyncSession, city_id: int, actor_id: Optional[str] = None
    ) -> List[str]:
        """Create any missing page for this city, without touching existing
        rows. Called from the city create flow and from the admin 'Provision
        missing pages' action, so a new serviceable city never becomes a
        landing-page gap that only shows up in a crawl report."""
        city = await db.get(City, city_id)
        if not city:
            raise ResourceNotFoundException("City", city_id)
        if not city.is_active:
            raise ValidationException(
                "only active cities can have landing pages",
                details={"field": "city_id"},
            )
        state = await db.get(State, city.state_id)
        return await LandingPageService._provision(
            db, city=city, state=state, actor_id=actor_id
        )

    @staticmethod
    async def sync_for_place(
        db: AsyncSession, place_id: int, actor_id: Optional[str] = None
    ) -> List[str]:
        """Provision the missing PLACE_CABS page for one place. Called from
        the place create flow and by sync_all, so a new airport or station
        never becomes a landing-page gap."""
        place = await db.get(Place, place_id)
        if not place:
            raise ResourceNotFoundException("Place", place_id)
        if not place.is_active:
            raise ValidationException(
                "only active places can have landing pages",
                details={"field": "place_id"},
            )
        city = await db.get(City, place.city_id)
        if not city:
            raise ResourceNotFoundException("City", place.city_id)
        return await LandingPageService._provision_place(
            db, place=place, city=city, actor_id=actor_id
        )

    @staticmethod
    async def sync_all(
        db: AsyncSession, actor_id: Optional[str] = None
    ) -> Dict[str, Any]:
        cities = (
            (await db.execute(select(City).where(City.is_active.is_(True))))
            .scalars()
            .all()
        )
        states = {
            s.id: s
            for s in (await db.execute(select(State).where(State.is_active.is_(True))))
            .scalars()
            .all()
        }
        created: List[str] = []
        for city in cities:
            created += await LandingPageService._provision(
                db, city=city, state=states.get(city.state_id), actor_id=actor_id
            )
        place_rows = (
            await db.execute(
                select(Place, City)
                .join(City, City.id == Place.city_id)
                .where(Place.is_active.is_(True), City.is_active.is_(True))
            )
        ).all()
        for place, city in place_rows:
            created += await LandingPageService._provision_place(
                db, place=place, city=city, actor_id=actor_id
            )
        return {
            "created": created,
            "cities": len(cities),
            "places": len(place_rows),
        }

    @staticmethod
    async def _provision(
        db: AsyncSession,
        *,
        city: City,
        state: Optional[State],
        actor_id: Optional[str],
    ) -> List[str]:
        created: List[str] = []
        specs = [
            ("CITY_HUB", None),
            ("CITY_CABS", "CAB"),
            ("CITY_HOTELS", "HOTEL"),
            ("CITY_TOURS", "TOUR"),
        ]
        for index, (page_type, service_type) in enumerate(specs):
            try:
                slug = LandingPageService.derive_slug(page_type, city.name)
            except ValidationException:
                continue
            exists = (
                await db.execute(
                    select(LandingPage.id).where(
                        LandingPage.page_type == page_type,
                        LandingPage.city_id == city.id,
                    )
                )
            ).scalar()
            if exists:
                continue
            db.add(
                LandingPage(
                    slug=slug,
                    page_type=page_type,
                    city_id=city.id,
                    state_id=None,
                    service_type=service_type,
                    display_name=city.name,
                    is_published=False,
                    display_order=index,
                    blocks=_default_blocks(page_type),
                    gallery=[],
                    highlights=[],
                    stats=[],
                    faqs=[],
                    created_by_user_id=_as_uuid(actor_id),
                    updated_by_user_id=_as_uuid(actor_id),
                    created_at=_utcnow(),
                    updated_at=_utcnow(),
                )
            )
            created.append(slug)

        await db.flush()
        if created:
            LandingPageService._audit(
                db,
                entity_id=city.id,
                action_type="PROVISIONED",
                new_value={"city_id": city.id, "created": created},
                actor_id=actor_id,
            )
            await db.flush()
        return created

    @staticmethod
    async def _provision_place(
        db: AsyncSession,
        *,
        place: Place,
        city: City,
        actor_id: Optional[str],
    ) -> List[str]:
        slug = LandingPageService.derive_place_slug(city.name, place.name)
        exists = (
            await db.execute(
                select(LandingPage.id).where(
                    LandingPage.page_type == PLACE_CABS,
                    LandingPage.place_id == place.id,
                )
            )
        ).scalar()
        if exists:
            return []
        db.add(
            LandingPage(
                slug=slug,
                page_type=PLACE_CABS,
                city_id=city.id,
                state_id=None,
                place_id=place.id,
                service_type="CAB",
                display_name=place.name,
                is_published=False,
                display_order=10,
                blocks=_default_blocks(PLACE_CABS),
                gallery=[],
                highlights=[],
                stats=[],
                faqs=[],
                created_by_user_id=_as_uuid(actor_id),
                updated_by_user_id=_as_uuid(actor_id),
                created_at=_utcnow(),
                updated_at=_utcnow(),
            )
        )
        await db.flush()
        LandingPageService._audit(
            db,
            entity_id=place.id,
            action_type="PROVISIONED",
            new_value={"place_id": place.id, "created": [slug]},
            actor_id=actor_id,
        )
        await db.flush()
        return [slug]

    # ══════════════════════════════════════════════════════════════════
    # Admin editor support
    # ══════════════════════════════════════════════════════════════════

    @staticmethod
    async def published_slugs(db: AsyncSession) -> List[str]:
        rows = (
            (
                await db.execute(
                    select(LandingPage.slug).where(LandingPage.is_published.is_(True))
                )
            )
            .scalars()
            .all()
        )
        return list(rows)

    @staticmethod
    async def editor_options(db: AsyncSession) -> Dict[str, Any]:
        """City/state picklists for the editor, with the pages already present
        so the admin cannot try to create a duplicate scope by accident."""
        cities = (
            (
                await db.execute(
                    select(
                        City.id,
                        City.name,
                        City.state_id,
                        State.name.label("state_name"),
                    )
                    .join(State, State.id == City.state_id)
                    .where(City.is_active.is_(True))
                    .order_by(City.name)
                )
            )
            .mappings()
            .all()
        )
        states = (
            (
                await db.execute(
                    select(State.id, State.name)
                    .where(State.is_active.is_(True))
                    .order_by(State.name)
                )
            )
            .mappings()
            .all()
        )
        existing = (
            (
                await db.execute(
                    select(
                        LandingPage.page_type, LandingPage.city_id, LandingPage.state_id
                    )
                )
            )
            .mappings()
            .all()
        )

        return {
            "cities": [
                {
                    "id": int(c["id"]),
                    "name": c["name"],
                    "state_id": int(c["state_id"]),
                    "state_name": c["state_name"],
                }
                for c in cities
            ],
            "states": [{"id": int(s["id"]), "name": s["name"]} for s in states],
            "taken_scopes": [
                {
                    "page_type": r["page_type"],
                    "city_id": r["city_id"],
                    "state_id": r["state_id"],
                }
                for r in existing
            ],
        }

    @staticmethod
    async def suggest(
        db: AsyncSession,
        page_type: str,
        city_id: Optional[int],
        state_id: Optional[int],
    ) -> Dict[str, Any]:
        """What the editor shows the moment a scope is picked: the slug the
        platform would use, the default block layout, and the copy the page
        would derive if the owner authored nothing."""
        if page_type not in _PAGE_TYPES:
            raise ValidationException(
                f"unknown page_type {page_type!r}", details={"field": "page_type"}
            )
        if page_type in _PLACE_PAGE_TYPES:
            raise ValidationException(
                "place pages are provisioned from Places master data",
                details={"field": "page_type"},
            )
        scope_name = await LandingPageService._require_scope(
            db, page_type, city_id, state_id
        )
        slug = LandingPageService.derive_slug(page_type, scope_name)
        preview = LandingPage(
            slug=slug,
            page_type=page_type,
            city_id=city_id,
            state_id=state_id,
            service_type=_PAGE_TYPE_SERVICE[page_type],
            display_name=scope_name,
        )
        prices = await LandingPageService.derive_prices(db, preview)
        derived = await LandingPageService.derive_copy(
            db,
            preview,
            scope_name=scope_name,
            region_name=scope_name,
            prices=prices,
        )
        return {
            "slug": slug,
            "reserved": _is_reserved_slug(slug),
            "display_name": scope_name,
            "service_type": _PAGE_TYPE_SERVICE[page_type],
            "blocks": _default_blocks(page_type),
            "derived": derived,
            "prices": prices,
            "publish_blockers": [],
        }

    # ══════════════════════════════════════════════════════════════════
    # Internals
    # ══════════════════════════════════════════════════════════════════

    _MUTABLE_FIELDS = (
        "eyebrow",
        "headline",
        "subheadline",
        "body_text",
        "banner_image_url",
        "banner_mobile_url",
        "banner_video_url",
        "banner_alt_text",
        "og_image_url",
        "overlay_opacity",
        "gallery",
        "highlights",
        "stats",
        "blocks",
        "faqs",
        "cta_text",
        "cta_link",
        "cta2_text",
        "cta2_link",
        "seo_title",
        "seo_description",
        "seo_keywords",
        "starts_at",
        "ends_at",
        "display_order",
    )

    @staticmethod
    def _apply(page: LandingPage, payload) -> None:
        data = payload.model_dump(exclude_unset=True)
        for field in LandingPageService._MUTABLE_FIELDS:
            if field in data:
                value = data[field]
                if field in _MUTABLE_STR_FIELDS:
                    value = _clean_str(value)
                setattr(page, field, value)

    @staticmethod
    async def _require_scope(
        db: AsyncSession,
        page_type: str,
        city_id: Optional[int],
        state_id: Optional[int],
    ) -> str:
        if page_type in _CITY_PAGE_TYPES:
            city = await db.get(City, int(city_id or 0))
            if not city:
                raise ResourceNotFoundException("City", city_id)
            return city.name
        state = await db.get(State, int(state_id or 0))
        if not state:
            raise ResourceNotFoundException("State", state_id)
        return state.name

    @staticmethod
    async def _scope_names(
        db: AsyncSession, city_id: Optional[int], state_id: Optional[int]
    ) -> Tuple[Optional[str], Optional[str]]:
        city_name = (
            (await db.execute(select(City.name).where(City.id == city_id))).scalar()
            if city_id
            else None
        )
        state_name = (
            (await db.execute(select(State.name).where(State.id == state_id))).scalar()
            if state_id
            else None
        )
        return city_name, state_name

    @staticmethod
    async def to_admin_dict(
        db: AsyncSession,
        page: LandingPage,
        *,
        city_name: Optional[str],
        state_name: Optional[str],
        place_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        if page.place_id and place_name is None:
            place_row = await db.get(Place, page.place_id)
            place_name = place_row.name if place_row else None
        is_place = page.page_type in _PLACE_PAGE_TYPES
        scope_name = (
            (place_name or page.display_name)
            if is_place
            else city_name or state_name or page.display_name
        )
        region_for_copy = (
            city_name if is_place and city_name else (state_name or scope_name)
        )
        _, image_source = LandingPageService.resolve_hero(
            page, page.page_type, scope_name
        )
        # Show what the page will actually render. An empty stored list is not
        # "no layout" — build_public_payload falls back to the page type's
        # default blocks, so an editor that showed [] here would offer a blank
        # structure for a page the website renders full.
        blocks = (
            page.blocks
            if isinstance(page.blocks, list) and page.blocks
            else _default_blocks(page.page_type)
        )
        derived = await LandingPageService.derive_copy(
            db,
            page,
            scope_name=scope_name,
            region_name=region_for_copy,
            prices=None,
        )
        return {
            "id": page.id,
            "slug": page.slug,
            "page_type": page.page_type,
            "city_id": page.city_id,
            "state_id": page.state_id,
            "place_id": page.place_id,
            "place_name": place_name,
            "city_name": city_name,
            "state_name": state_name,
            "service_type": page.service_type,
            "display_name": page.display_name,
            "eyebrow": page.eyebrow,
            "headline": page.headline,
            "subheadline": page.subheadline,
            "body_text": page.body_text,
            "banner_image_url": page.banner_image_url,
            "banner_mobile_url": page.banner_mobile_url,
            "banner_video_url": page.banner_video_url,
            "banner_alt_text": page.banner_alt_text,
            "og_image_url": page.og_image_url,
            "overlay_opacity": (
                float(page.overlay_opacity)
                if page.overlay_opacity is not None
                else None
            ),
            "gallery": page.gallery or [],
            "highlights": page.highlights or [],
            "stats": page.stats or [],
            "blocks": blocks,
            "faqs": page.faqs or [],
            "cta_text": page.cta_text,
            "cta_link": page.cta_link,
            "cta2_text": page.cta2_text,
            "cta2_link": page.cta2_link,
            "seo_title": page.seo_title,
            "seo_description": page.seo_description,
            "seo_keywords": page.seo_keywords,
            "is_published": page.is_published,
            "starts_at": page.starts_at,
            "ends_at": page.ends_at,
            "display_order": page.display_order,
            "derived": derived,
            "image_source": image_source,
            "slot_specs": _SLOT_SPECS,
            "created_by_user_id": page.created_by_user_id,
            "updated_by_user_id": page.updated_by_user_id,
            "created_at": page.created_at,
            "updated_at": page.updated_at,
        }

    @staticmethod
    def _audit(
        db: AsyncSession,
        *,
        entity_id: int,
        action_type: str,
        previous_value: Optional[dict] = None,
        new_value: Optional[dict] = None,
        actor_id: Optional[str] = None,
    ) -> None:
        db.add(
            CmsAuditVersion(
                entity_type="landing_page",
                entity_id=entity_id,
                section_key=None,
                action_type=action_type,
                previous_value=_jsonable(previous_value),
                new_value=_jsonable(new_value),
                changed_by_user_id=_as_uuid(actor_id),
            )
        )


# ── Module helpers ───────────────────────────────────────────────────────────

_MUTABLE_STR_FIELDS = frozenset(
    {
        "eyebrow",
        "headline",
        "subheadline",
        "banner_image_url",
        "banner_mobile_url",
        "banner_video_url",
        "banner_alt_text",
        "og_image_url",
        "cta_text",
        "cta_link",
        "cta2_text",
        "cta2_link",
        "seo_title",
        "seo_description",
        "seo_keywords",
    }
)

_URL_PATTERNS = PAGE_TYPE_URL_PATTERNS


def _as_uuid(value: Any) -> Optional[UUID]:
    if not value:
        return None
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _jsonable(value: Any) -> Any:
    if value is None:
        return None
    try:
        return json.loads(json.dumps(value, default=str))
    except Exception:
        return {"value": str(value)}


def _plural(count: Any, one: str, many: str) -> str:
    """One hotel is not "1+ Hotels" — derived counts stay exact and agree."""
    try:
        return one if int(count) == 1 else many
    except (TypeError, ValueError):
        return many


def _rupiah(value: Any) -> str:
    """₹1,25,000 — Indian lakh/crore grouping for derived copy and stat chips."""
    try:
        number = int(Decimal(str(value)).quantize(Decimal("1")))
    except Exception:
        return str(value)
    digits = str(abs(number))
    if len(digits) <= 3:
        grouped = digits
    else:
        # Last three digits stand alone, then pairs of two going left.
        head = digits[:-3]
        groups = [digits[-3:]]
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        grouped = ",".join(groups)
    sign = "-" if number < 0 else ""
    return f"{sign}₹{grouped}"


def _block_limit(blocks: List[Dict[str, Any]], btype: str, default: int) -> int:
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == btype:
            try:
                limit = int(block.get("limit") or default)
            except (TypeError, ValueError):
                return default
            return max(1, min(limit, 12))
    return default


def _place_tokens(place_name: Optional[str], city_name: Optional[str]) -> List[str]:
    """
    Words a listing's landmark/destination must contain to count as supply
    *for this place* on a place page.

    The city token is dropped (every row already matches the city scope), and
    "International" is — it renames half the hotels in India, not the airport.
    Kept words like "airport", "railway", "station", "patnaik" are exactly the
    landmark vocabulary property listings use when they mean the place.
    """
    drop = set(slugify(city_name or "").split("-")) | {"international"}
    keep: List[str] = []
    for word in slugify(place_name or "").split("-"):
        if len(word) < 4 or word in drop:
            continue
        if word not in keep:
            keep.append(word)
    return keep[:4]


def _sibling_label(page_type: str) -> str:
    return {
        "CITY_HUB": "City overview",
        "CITY_CABS": "Cabs",
        "CITY_HOTELS": "Hotels",
        "CITY_TOURS": "Tours",
        "STATE_HOTELS": "Hotels",
        "STATE_TOURS": "Tours",
        "PLACE_CABS": "Airport & station cabs",
    }.get(page_type, page_type)


def _default_subheadline(page_type: str, pretty: str, price_line: str) -> str:
    base = {
        "CITY_HUB": (
            f"Verified cabs, hotels and tour packages in {pretty} — real prices, "
            "instant confirmation, managed end to end by WayTero."
        ),
        "CITY_CABS": f"Local, airport and outstation cabs in {pretty} with upfront fares.",
        "CITY_HOTELS": f"Verified hotels in {pretty} with real nightly rates and instant booking.",
        "CITY_TOURS": f"Itineraries, durations and per-person pricing for {pretty}.",
        "STATE_HOTELS": f"Verified hotels across {pretty} with real nightly rates.",
        "STATE_TOURS": f"Tour packages across {pretty} with real itineraries and pricing.",
        "PLACE_CABS": (
            f"Cabs to and from {pretty} — pickup, drop and per-km fares from the "
            "live pricing rules, booked in seconds."
        ),
    }[page_type]
    if price_line:
        return f"{base} {price_line}."
    return base


def _default_body(page_type: str, pretty: str) -> str:
    if page_type == "CITY_CABS":
        return (
            f"Every fare below comes from the live pricing rules for {pretty} — the "
            "same rules the booking engine charges. Where a city-specific rate is "
            "not configured, the platform-standard rate applies and the page says so."
        )
    if page_type == PLACE_CABS:
        return (
            f"Pickups and drop-offs at {pretty} are quoted from the live pricing "
            "rules for its city — the same rules the booking engine charges. "
            "Where a city-specific rate is not configured, the platform-standard "
            "rate applies and the page says so."
        )
    if page_type in ("CITY_HOTELS", "STATE_HOTELS"):
        return (
            f"Listed properties in {pretty} are partner-managed and verified before "
            "they go live. The starting price is the cheapest active room category; "
            "the property page shows inclusions, policies and GST treatment."
        )
    if page_type in ("CITY_TOURS", "STATE_TOURS"):
        return (
            f"Each {pretty} package lists a day-by-day itinerary, what is included "
            "and what is not, and per-person pricing before you pay. Nothing is "
            "quoted that the operator has not published."
        )
    return (
        f"Everything bookable in {pretty} on one page — cabs, stays and tours, each "
        "priced from the platform's live rules rather than a marketing number."
    )


def _keyword_union(raw: str) -> List[str]:
    seen: List[str] = []
    for part in (raw or "").split(","):
        token = part.strip().lower()
        if token and token not in seen:
            seen.append(token)
    return seen


def _default_keywords(
    page_type: str, pretty: str, region_name: Optional[str]
) -> List[str]:
    vertical = {
        "CITY_CABS": ["cab booking", "taxi", "cab fare", "airport cab"],
        "PLACE_CABS": [
            "cab booking",
            "airport cab",
            "cab rent",
            "car hire",
            "taxi",
        ],
        "CITY_HOTELS": ["hotel booking", "rooms", "stay"],
        "STATE_HOTELS": ["hotel booking", "rooms", "stay"],
        "CITY_TOURS": ["tour packages", "holiday", "itinerary"],
        "STATE_TOURS": ["tour packages", "holiday", "itinerary"],
        "CITY_HUB": ["cabs", "hotels", "tour packages"],
    }[page_type]
    out: List[str] = []
    for term in vertical:
        for candidate in (f"{term} in {pretty.lower()}", term):
            if candidate not in out:
                out.append(candidate)
    if region_name:
        out.append(region_name.lower())
    return out[:12]
