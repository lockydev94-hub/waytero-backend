# ============================================================
# WAYTERO — SEED BHUBANESWAR LANDMARKS
# File: scripts/seed_bhubaneswar_landmarks.py
# Run: python -m scripts.seed_bhubaneswar_landmarks  (from Backend/ dir)
#
# Places began as transport nodes (airport, railway station) and each one
# provisions a PLACE_CABS landing page, which capped a city at two local
# pages. LANDMARK (migration 0110) opens the registry to the places people
# actually search for — a zoo, a temple, the caves, the stupa — and this
# script seeds the four that matter in Bhubaneswar.
#
# Rows are created through PlaceService, so slugs are derived by the same
# rule the admin UI uses and the audit trail is written as usual. Content is
# filled only where the column is still empty: re-running the script on a
# live database updates nothing an editor has already written.
#
# Doc Ref: Place-Level Landing Pages §Phase 1
# ============================================================

import asyncio
import os
import sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.modules.admin.models import LandingPage
from app.modules.admin.schemas import PlaceCreate
from app.modules.admin.services.landing_pages import LandingPageService
from app.modules.admin.services.places import PlaceService
from app.modules.master.models import City, Place

CITY_NAME = "Bhubaneswar"

LANDMARKS: List[Dict[str, Any]] = [
    {
        "kind": "LANDMARK",
        "name": "Nandankanan Zoological Park",
        "address": "Nandankanan Road, Barang, Bhubaneswar 754005",
        "latitude": 20.396,
        "longitude": 85.824,
        "seo_title": "Cab to Nandankanan Zoological Park from Bhubaneswar",
        "seo_description": (
            "Book a cab to Nandankanan Zoological Park from anywhere in "
            "Bhubaneswar. About 20 km, fixed fare by vehicle class, and a "
            "driver who waits while you walk the zoo."
        ),
        "seo_keywords": (
            "cab to Nandankanan, Nandankanan zoo taxi Bhubaneswar, "
            "Bhubaneswar to Nandankanan cab fare, Nandankanan day trip"
        ),
        "page": {
            "eyebrow": "Cabs to Nandankanan · Bhubaneswar",
            "headline": "Cab to Nandankanan Zoological Park",
            "subheadline": (
                "About 20 km north of the city on the Cuttack road, closed on "
                "Mondays. Hire by the hour so the driver waits while you walk "
                "the park, or take a fixed round trip that brings you back the "
                "same day."
            ),
            "body_text": (
                "Nandankanan sits between Bhubaneswar and Cuttack, about 20 km "
                "from central Bhubaneswar and roughly the same from the "
                "airport. It is a 400-hectare wooded park wrapped around "
                "Kanjia Lake, not a city menagerie: the walking circuit alone "
                "takes three to four hours, and the safari sections add more. "
                "That distance and that duration are the two reasons most "
                "visitors book a cab rather than a bus.\n\n"
                "A one-way drop works if you are happy to find your own way "
                "back, but the park's gate is on a stretch of road where "
                "getting a return cab at closing time is the unreliable part "
                "of the day. An hourly or full-day hire removes that: the "
                "driver stays with the vehicle, you walk the zoo at your own "
                "pace, and the car is at the gate when you come out. The same "
                "arrangement covers the Botanical Garden next door and the "
                "Boating and ropeway counters inside the park.\n\n"
                "Go early. The animals are active in the first couple of hours "
                "after opening and the safari queues are shortest then; by "
                "midday the big cats are asleep and the walk is hot. The park "
                "is closed on Mondays, and timings shift with the season — "
                "roughly 7:30 am to 5:30 pm from April to September and 8:00 am "
                "to 5:00 pm from October to March. Check the official site "
                "before you set out, since the zoo closes on some public "
                "holidays as well.\n\n"
                "If you are arriving by air and heading straight to the zoo, "
                "an airport pickup works as a single booking — the airport and "
                "the park are on the same side of the city, so it is not a "
                "detour through traffic. Families with luggage often book the "
                "hotel drop as a second leg on the same day."
            ),
            "highlights": [
                {
                    "title": "20 km, half a day",
                    "text": (
                        "The park gate is about 20 km from the city centre — "
                        "close enough for a morning trip, far enough that the "
                        "return ride matters."
                    ),
                },
                {
                    "title": "The driver waits",
                    "text": (
                        "Hourly and day hire keep the vehicle with you, so the "
                        "walk through the zoo does not end with a hunt for a "
                        "cab at the gate."
                    ),
                },
                {
                    "title": "Straight from the airport",
                    "text": (
                        "The airport and Nandankanan are on the same side of "
                        "Bhubaneswar, so an arrival pickup can drop you at the "
                        "zoo gate without crossing the city."
                    ),
                },
            ],
            "faqs": [
                {
                    "question": "How far is Nandankanan from Bhubaneswar?",
                    "answer": (
                        "About 20 km from central Bhubaneswar, on the Cuttack "
                        "side of the city. The drive takes roughly 40 to 50 "
                        "minutes depending on traffic at Barang."
                    ),
                },
                {
                    "question": "Will the driver wait while we visit the zoo?",
                    "answer": (
                        "On an hourly or full-day booking, yes — the waiting "
                        "time is part of the package and the fare rules state "
                        "how many hours are included. On a plain one-way drop "
                        "the driver leaves after the drop."
                    ),
                },
                {
                    "question": "Which day is Nandankanan closed?",
                    "answer": (
                        "Mondays. The zoo also closes on a few public holidays, "
                        "so confirm the day on the official Nandankanan site "
                        "before booking."
                    ),
                },
                {
                    "question": "Can I combine Nandankanan with other sights?",
                    "answer": (
                        "Yes. A full-day hire comfortably covers Nandankanan "
                        "plus the Botanical Garden on the same campus, and "
                        "still leaves time for a stop in the Old Town on the "
                        "way back."
                    ),
                },
            ],
        },
    },
    {
        "kind": "LANDMARK",
        "name": "Lingaraj Temple",
        "address": "Old Town, near Bindusagar Lake, Bhubaneswar 751002",
        "latitude": 20.2382,
        "longitude": 85.8403,
        "seo_title": "Cab to Lingaraj Temple, Bhubaneswar — Old Town Drop",
        "seo_description": (
            "Book a cab to Lingaraj Temple in Bhubaneswar's Old Town. About "
            "4 km from the railway station, drop at the temple gate, or hourly "
            "hire for the whole temple circuit."
        ),
        "seo_keywords": (
            "cab to Lingaraj Temple, Lingaraj temple taxi Bhubaneswar, "
            "Bhubaneswar Old Town cab, Lingaraj temple timings"
        ),
        "page": {
            "eyebrow": "Cabs to Lingaraj · Bhubaneswar",
            "headline": "Cab to Lingaraj Temple",
            "subheadline": (
                "The 11th-century Kalinga temple in the Old Town, about 4 km "
                "from the railway station — a short ride that pairs naturally "
                "with Bindusagar, Mukteshwar and the rest of the temple "
                "cluster."
            ),
            "body_text": (
                "Lingaraj is the oldest and largest of Bhubaneswar's living "
                "temples, and the reason the city is called the Temple City. "
                "It stands in the Old Town, roughly 4 km from Bhubaneswar "
                "railway station and a similar distance from the Master "
                "Canteen side of the centre — a ten to fifteen minute drive, "
                "not a trip across town.\n\n"
                "The last stretch is the part worth knowing about. The Old "
                "Town lanes around the temple are narrow and congested, and "
                "parking close to the entrance is limited, so a drop at the "
                "gate is usually quicker than driving in and circling. A cab "
                "that waits also solves the return leg: the temple is a short "
                "visit — an hour or ninety minutes — but the surrounding "
                "cluster is not. Bindusagar lake, Mukteshwar, Kedar Gouri, "
                "Ananta Vasudeva and Ekamra Haat are all within a couple of "
                "kilometres, and an hourly booking covers the whole circuit "
                "without re-booking between stops.\n\n"
                "Timings are long but not continuous: the temple opens early "
                "in the morning and closes for a few hours around midday "
                "before reopening for the evening. Only Hindus may enter the "
                "sanctum area; visitors of other faiths can see the complex "
                "from the viewing platform outside the wall. Maha Shivaratri "
                "and Ashokastami bring enormous crowds — the roads are closed "
                "to ordinary traffic and the drop point moves well back "
                "from the gate.\n\n"
                "Early morning is the calmest time to arrive, both for the "
                "temple and for the drive. If you are starting from the "
                "airport, a Lingaraj stop fits naturally on the way into the "
                "city; if you are heading to Puri afterwards, the Old Town is "
                "on the right side of Bhubaneswar to continue south."
            ),
            "highlights": [
                {
                    "title": "Old Town drop",
                    "text": (
                        "Dropped at the temple gate rather than left to find "
                        "parking in the narrow lanes around it."
                    ),
                },
                {
                    "title": "Hourly hire for the circuit",
                    "text": (
                        "Lingaraj, Bindusagar, Mukteshwar and Kedar Gouri sit "
                        "within a couple of kilometres — one booking covers "
                        "all of them."
                    ),
                },
                {
                    "title": "Early darshan runs",
                    "text": (
                        "Pre-dawn and early-morning pickups are bookable, which "
                        "is when the temple and the roads are quietest."
                    ),
                },
            ],
            "faqs": [
                {
                    "question": "How far is Lingaraj Temple from Bhubaneswar railway station?",
                    "answer": (
                        "About 4 km, a drive of ten to fifteen minutes "
                        "depending on traffic in the Old Town lanes."
                    ),
                },
                {
                    "question": "What are the Lingaraj Temple timings?",
                    "answer": (
                        "The temple opens early in the morning and stays open "
                        "until evening, with a break of a few hours around "
                        "midday. Timings shift with the festival calendar, so "
                        "confirm before an early or late visit."
                    ),
                },
                {
                    "question": "Can non-Hindus visit Lingaraj Temple?",
                    "answer": (
                        "Only Hindus may enter the temple. Visitors of other "
                        "faiths can view the complex from the viewing platform "
                        "built outside the boundary wall."
                    ),
                },
                {
                    "question": "Can I cover other temples on the same booking?",
                    "answer": (
                        "Yes — an hourly or half-day hire is the practical way "
                        "to do it. Mukteshwar, Kedar Gouri, Ananta Vasudeva and "
                        "Bindusagar lake are all a short drive apart."
                    ),
                },
            ],
        },
    },
    {
        "kind": "LANDMARK",
        "name": "Khandagiri & Udayagiri Caves",
        "address": "Khandagiri, Bhubaneswar 751030",
        "latitude": 20.2627,
        "longitude": 85.7789,
        "seo_title": "Cab to Khandagiri & Udayagiri Caves, Bhubaneswar",
        "seo_description": (
            "Book a cab to the Udayagiri and Khandagiri Jain caves, about "
            "8 km west of Bhubaneswar. Fixed fare, driver waits, and an easy "
            "half-day with Dhauli."
        ),
        "seo_keywords": (
            "cab to Khandagiri caves, Udayagiri caves taxi Bhubaneswar, "
            "Khandagiri cab fare, Bhubaneswar heritage cab"
        ),
        "page": {
            "eyebrow": "Cabs to Khandagiri · Bhubaneswar",
            "headline": "Cab to Khandagiri & Udayagiri Caves",
            "subheadline": (
                "Two hills of 2,000-year-old Jain rock-cut caves about 8 km "
                "west of the centre — a two-hour climb that fits neatly into a "
                "half-day hire."
            ),
            "body_text": (
                "The twin hills of Udayagiri and Khandagiri hold the oldest "
                "monuments in Bhubaneswar: around thirty rock-cut caves carved "
                "from the 1st century BCE under King Kharavela of Kalinga, "
                "most of them shelters for Jain monks. Udayagiri carries the "
                "richer carving — Rani Gumpha's friezes and the Hathigumpha "
                "inscription — while Khandagiri's summit gives the better view "
                "over the city.\n\n"
                "They sit about 8 km west of central Bhubaneswar, close to the "
                "junction where the old Cuttack and Puri roads meet, so the "
                "drive is twenty minutes and traffic is rarely the problem. "
                "Parking is at the foot of the hills; from there it is a climb "
                "of around a hundred steps to the main caves, and the ground "
                "between them is uneven, so plan for two to three hours on "
                "site and shoes with grip.\n\n"
                "Because the site is compact, it works well as half of a "
                "half-day. Dhauli is roughly 15 km away on the other side of "
                "the city, and the two together make a natural heritage loop "
                "for an hourly hire; alternatively, Khandagiri pairs with the "
                "Old Town temples if you would rather stay closer to the "
                "centre. Mornings and late afternoons are cooler and the light "
                "on the carvings is better than at midday.\n\n"
                "The caves are open through the week and carry a small "
                "Archaeological Survey of India entry ticket for Indian and "
                "foreign visitors. It is a heritage site rather than a "
                "managed attraction, so there is little shade on the climb and "
                "no lift — a drop-and-wait booking is more comfortable than "
                "arranging a return ride from the gate."
            ),
            "highlights": [
                {
                    "title": "20 minutes from the centre",
                    "text": (
                        "About 8 km west of the city, at the Khandagiri "
                        "junction — parking at the foot of the hills."
                    ),
                },
                {
                    "title": "Half-day heritage loop",
                    "text": (
                        "Pair the caves with Dhauli on one hourly booking and "
                        "cover both ends of the city's oldest history."
                    ),
                },
                {
                    "title": "Driver waits at the base",
                    "text": (
                        "The climb takes two to three hours; the vehicle stays "
                        "parked at the gate instead of being dismissed."
                    ),
                },
            ],
            "faqs": [
                {
                    "question": "How far are the Khandagiri and Udayagiri caves from Bhubaneswar?",
                    "answer": (
                        "About 8 km west of the city centre, roughly a "
                        "20-minute drive. They are also about 8 to 9 km from "
                        "Bhubaneswar railway station."
                    ),
                },
                {
                    "question": "How long does a visit take?",
                    "answer": (
                        "Plan on two to three hours including the climb and "
                        "both hills. Udayagiri is the more elaborate of the "
                        "two and takes the larger share of the time."
                    ),
                },
                {
                    "question": "What are the cave timings?",
                    "answer": (
                        "The site is generally open from morning to late "
                        "afternoon and stays open through the week. It is an "
                        "ASI monument with an entry ticket, so timings can "
                        "shift around holidays."
                    ),
                },
                {
                    "question": "Can I combine the caves with Dhauli?",
                    "answer": (
                        "Yes — the two are about 15 km apart across the city "
                        "and make a comfortable half-day on an hourly or "
                        "half-day hire."
                    ),
                },
            ],
        },
    },
    {
        "kind": "LANDMARK",
        "name": "Dhauli Shanti Stupa",
        "address": "Dhauli, near the Daya river, Bhubaneswar",
        "latitude": 20.1926,
        "longitude": 85.8425,
        "seo_title": "Cab to Dhauli Shanti Stupa — Bhubaneswar Day Hire",
        "seo_description": (
            "Book a cab to Dhauli Shanti Stupa, about 8 km south of Bhubaneswar "
            "on the Daya river. Fixed fare, waiting included, and a natural "
            "stop on the way to Puri."
        ),
        "seo_keywords": (
            "cab to Dhauli, Dhauli Shanti Stupa taxi Bhubaneswar, "
            "Dhauli peace pagoda cab, Bhubaneswar to Puri via Dhauli"
        ),
        "page": {
            "eyebrow": "Cabs to Dhauli · Bhubaneswar",
            "headline": "Cab to Dhauli Shanti Stupa",
            "subheadline": (
                "The white peace pagoda on the Daya river, where Ashoka is said "
                "to have turned from conquest to Buddhism — about 8 km south, "
                "and a natural stop on the road to Puri."
            ),
            "body_text": (
                "Dhauli is the hill above the Daya river where, in 261 BCE, the "
                "Kalinga war was fought and — by the account carved into "
                "Ashoka's own edicts — the emperor renounced further conquest. "
                "The rock edicts are still there, cut into a boulder at the "
                "foot of the hill, and the white Shanti Stupa at the summit was "
                "built in the 1970s with Japanese involvement to mark the "
                "site's later life as a place of peace.\n\n"
                "It is about 8 km south of central Bhubaneswar, a fifteen to "
                "twenty minute drive, and the road continues towards Puri — "
                "which is what makes Dhauli useful. If you are leaving for "
                "Puri, Konark or Chilika, a short stop here costs almost "
                "nothing in time, and the hilltop view over the river plain is "
                "worth the detour. Coming back to the city for it as a "
                "dedicated trip is also easy, but most visitors fold it into a "
                "departure.\n\n"
                "The visit itself is short: the stupa, the edicts and the "
                "views take forty-five minutes to an hour, more if you walk "
                "down to the rock edicts rather than just the summit. There "
                "are steps and a slope up to the stupa, so a drop-and-wait "
                "booking is simpler than releasing the cab and hoping for a "
                "return ride from a rural stretch of road.\n\n"
                "Early morning and late afternoon are the best hours, both for "
                "the light and because the stupa is exposed with little shade. "
                "Entry is free. Combine it with Khandagiri for a heritage "
                "half-day, or with Lingaraj on the way out of the city if you "
                "are heading south."
            ),
            "highlights": [
                {
                    "title": "On the way to Puri",
                    "text": (
                        "Dhauli sits on the southern exit from Bhubaneswar, so "
                        "the stop costs little time on a Konark or Puri run."
                    ),
                },
                {
                    "title": "An hour is enough",
                    "text": (
                        "Stupa, edicts and the river view fit into under an "
                        "hour — a short, high-value stop rather than a half-day."
                    ),
                },
                {
                    "title": "Waiting included",
                    "text": (
                        "The cab stays while you climb to the stupa; there is no "
                        "reliable stream of return cabs from the hill."
                    ),
                },
            ],
            "faqs": [
                {
                    "question": "How far is Dhauli from Bhubaneswar?",
                    "answer": (
                        "About 8 km south of the city centre, a drive of "
                        "fifteen to twenty minutes on the road towards Puri."
                    ),
                },
                {
                    "question": "How long does a Dhauli visit take?",
                    "answer": (
                        "Forty-five minutes to an hour for the stupa and the "
                        "rock edicts. Add time if you want to walk the full "
                        "hill rather than drive to the top."
                    ),
                },
                {
                    "question": "Can I stop at Dhauli on the way to Puri?",
                    "answer": (
                        "Yes. It is on the southern route out of Bhubaneswar, "
                        "so an outstation booking to Puri or Konark can include "
                        "the stop without a separate trip."
                    ),
                },
                {
                    "question": "Is there an entry fee at Dhauli?",
                    "answer": (
                        "Entry to the stupa and the edict site is free. There "
                        "may be a small charge for the on-site light-and-sound "
                        "show if it is running."
                    ),
                },
            ],
        },
    },
]


def _fill(obj, field: str, value):
    """Set ``field`` only when the column is still empty — never clobber an
    editor's copy on a re-run."""
    if not getattr(obj, field, None):
        setattr(obj, field, value)


async def seed() -> None:
    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    async with session_factory() as db:
        city = (
            await db.execute(
                select(City).where(func.lower(City.name) == CITY_NAME.lower())
            )
        ).scalar_one_or_none()
        if not city:
            print(f"[seed_landmarks] ERROR: city {CITY_NAME!r} not found.")
            await engine.dispose()
            sys.exit(1)

        created_places = 0
        created_pages = 0
        updated_pages = 0

        for order, item in enumerate(LANDMARKS, start=2):
            place = (
                await db.execute(
                    select(Place).where(
                        Place.city_id == city.id,
                        func.lower(Place.name) == item["name"].lower(),
                    )
                )
            ).scalar_one_or_none()

            if not place:
                payload = PlaceCreate(
                    city_id=int(city.id),
                    kind=item["kind"],
                    name=item["name"],
                    code=None,
                    address=item["address"],
                    latitude=item["latitude"],
                    longitude=item["longitude"],
                    is_active=True,
                    seo_title=item["seo_title"],
                    seo_description=item["seo_description"],
                    seo_keywords=item["seo_keywords"],
                )
                await PlaceService.create(db, payload, actor_id=None)
                place = (
                    await db.execute(
                        select(Place).where(
                            Place.city_id == city.id,
                            func.lower(Place.name) == item["name"].lower(),
                        )
                    )
                ).scalar_one()
                created_places += 1
                print(f"[seed_landmarks] place created: {place.name} (id={place.id})")
            else:
                _fill(place, "address", item["address"])
                _fill(place, "seo_title", item["seo_title"])
                _fill(place, "seo_description", item["seo_description"])
                _fill(place, "seo_keywords", item["seo_keywords"])

            page = (
                await db.execute(
                    select(LandingPage).where(
                        LandingPage.place_id == place.id,
                        LandingPage.page_type == "PLACE_CABS",
                    )
                )
            ).scalar_one_or_none()
            if not page:
                await LandingPageService.sync_for_place(db, place.id, actor_id=None)
                page = (
                    await db.execute(
                        select(LandingPage).where(
                            LandingPage.place_id == place.id,
                            LandingPage.page_type == "PLACE_CABS",
                        )
                    )
                ).scalar_one()
                created_pages += 1
                print(f"[seed_landmarks] landing page created: /{page.slug}")

            content = item["page"]
            before = (
                page.headline,
                page.body_text,
                page.seo_title,
                bool(page.is_published),
            )
            _fill(page, "eyebrow", content["eyebrow"])
            _fill(page, "headline", content["headline"])
            _fill(page, "subheadline", content["subheadline"])
            _fill(page, "body_text", content["body_text"])
            _fill(page, "seo_title", content["seo_title"])
            _fill(page, "seo_description", content["seo_description"])
            _fill(page, "seo_keywords", content["seo_keywords"])
            if not page.highlights:
                page.highlights = content["highlights"]
            if not page.faqs:
                page.faqs = content["faqs"]
            if not page.display_order:
                page.display_order = order
            # The copy is written and the place is real, so the page is worth
            # publishing — but only a page that actually has copy, so a failed
            # fill never becomes an empty indexed URL.
            if page.body_text and page.seo_title:
                page.is_published = True
            after = (
                page.headline,
                page.body_text,
                page.seo_title,
                bool(page.is_published),
            )
            if after != before:
                updated_pages += 1
            print(
                f"[seed_landmarks] /{page.slug} — published={page.is_published} "
                f"body={len(page.body_text or '')} chars"
            )

        await db.commit()
        print(
            f"[seed_landmarks] done — places created: {created_places}, "
            f"pages created: {created_pages}, pages updated: {updated_pages}"
        )

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed())
