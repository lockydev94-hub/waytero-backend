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

# Every model table must be registered before SQLAlchemy configures its
# mappers: LandingPage carries FKs to users and cities, and a partial import
# fails with NoReferencedTableError the first time a row is loaded.
import app.modules.auth.models  # noqa: F401
import app.modules.booking.models  # noqa: F401
import app.modules.chat.models  # noqa: F401
import app.modules.customer.models  # noqa: F401
import app.modules.driver.models  # noqa: F401
import app.modules.hotel.models  # noqa: F401
import app.modules.notification.models  # noqa: F401
import app.modules.partner.models  # noqa: F401
import app.modules.payment.models  # noqa: F401
import app.modules.settlement.models  # noqa: F401
import app.modules.tour.models  # noqa: F401
import app.modules.vehicle.models  # noqa: F401
import app.modules.wallet.models  # noqa: F401

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

# Second wave: the Old Town's smaller temples, the tantric yogini shrine on the
# city's edge, ISKCON on NH-5 and the Atri springs day trip. All of them are
# Bhubaneswar origins, so they share the city's rate card.
LANDMARKS += [
    {
        "kind": "LANDMARK",
        "name": "Mukteshwar Temple",
        "address": "Old Town, Kedar Gouri Vihar, Bhubaneswar 751019",
        "latitude": 20.2427,
        "longitude": 85.8416,
        "seo_title": "Cab to Mukteshwar Temple, Bhubaneswar — Old Town",
        "seo_description": (
            "Book a cab to Mukteshwar Temple in Bhubaneswar's Old Town. About "
            "5 km from the centre, dropped at the gate, and an easy addition "
            "to a Lingaraj or Rajarani booking."
        ),
        "seo_keywords": (
            "cab to Mukteshwar temple, Mukteshwar temple taxi Bhubaneswar, "
            "Old Town Bhubaneswar cab, Mukteshwar temple timings"
        ),
        "page": {
            "eyebrow": "Cabs to Mukteshwar · Bhubaneswar",
            "headline": "Cab to Mukteshwar Temple",
            "subheadline": (
                "The 10th-century temple with the famous torana gateway, in the "
                "middle of the Old Town cluster — five minutes from Lingaraj "
                "and a stop most visitors make on the same ride."
            ),
            "body_text": (
                "Mukteshwar is the temple architects point to when they talk "
                "about Kalinga style at its most refined. Built in the 10th "
                "century under the Somavamshi kings, it is small compared with "
                "Lingaraj but carries the carving that gave it the name 'gem of "
                "Odisha' — the arched torana gateway in front, with its lion "
                "heads and makara motifs, is the single most photographed "
                "detail in the city. The walls carry panels from the "
                "Panchatantra and rows of skeletal ascetics in meditation "
                "postures, a reminder that this was once a centre of tantric "
                "initiation.\n\n"
                "It stands in the Old Town, about 5 km from the city centre and "
                "four from the railway station, in the same cluster as Lingaraj, "
                "Rajarani, Kedar Gouri and Parashurameshwar — the 7th-century "
                "shrine next door that is the oldest standing temple in "
                "Bhubaneswar. That density is the reason to book by the hour "
                "rather than point to point: four temples and a tank inside a "
                "two-kilometre radius is a half-day, not four separate fares.\n\n"
                "The temple is free to enter and open from morning to evening, "
                "with a small tank beside it that fills with lotus in the "
                "cooler months. Mornings are quiet and the light on the "
                "sandstone is best before the sun climbs; evenings bring the "
                "evening lamps and a steady trickle of local worshippers. "
                "Mukteshwar has its own festival in the winter months, when "
                "the compound fills and the road outside is closed to traffic."
            ),
            "highlights": [
                {
                    "title": "The torana gateway",
                    "text": (
                        "The arched entrance is the finest piece of its kind in "
                        "Odisha — the reason this small temple is on every "
                        "architecture itinerary."
                    ),
                },
                {
                    "title": "Old Town cluster",
                    "text": (
                        "Lingaraj, Rajarani, Kedar Gouri and Parashurameshwar "
                        "are all within two kilometres — one hourly booking "
                        "covers them."
                    ),
                },
                {
                    "title": "Free, and open daily",
                    "text": (
                        "No ticket and no fixed slot, so the visit fits around "
                        "the rest of the Old Town rather than the other way "
                        "round."
                    ),
                },
            ],
            "faqs": [
                {
                    "question": "How far is Mukteshwar Temple from Bhubaneswar city centre?",
                    "answer": (
                        "About 5 km, in the Old Town. It is roughly four "
                        "kilometres from Bhubaneswar railway station and a few "
                        "minutes from Lingaraj Temple."
                    ),
                },
                {
                    "question": "What are the Mukteshwar Temple timings?",
                    "answer": (
                        "Generally from early morning to around 7 pm, every "
                        "day, with no entry fee. Timings can shift on festival "
                        "days."
                    ),
                },
                {
                    "question": "Is Mukteshwar worth visiting on its own?",
                    "answer": (
                        "It is a short visit on its own — half an hour. Most "
                        "people see it as part of the Old Town cluster, which "
                        "takes a half-day by cab."
                    ),
                },
                {
                    "question": "Can I combine Mukteshwar with Lingaraj Temple?",
                    "answer": (
                        "Yes, they are minutes apart. An hourly or half-day "
                        "hire covers Lingaraj, Mukteshwar, Rajarani and Kedar "
                        "Gouri without re-booking between stops."
                    ),
                },
            ],
        },
    },
    {
        "kind": "LANDMARK",
        "name": "Rajarani Temple",
        "address": "Rajarani Colony, Old Town, Bhubaneswar 751002",
        "latitude": 20.2435,
        "longitude": 85.8437,
        "seo_title": "Cab to Rajarani Temple — Bhubaneswar Old Town Drop",
        "seo_description": (
            "Book a cab to Rajarani Temple in Bhubaneswar's Old Town. About "
            "5 km from the centre, dropped at the ASI gate, with waiting for "
            "the Old Town circuit."
        ),
        "seo_keywords": (
            "cab to Rajarani temple, Rajarani temple taxi Bhubaneswar, "
            "Rajarani temple entry fee, Rajarani music festival"
        ),
        "page": {
            "eyebrow": "Cabs to Rajarani · Bhubaneswar",
            "headline": "Cab to Rajarani Temple",
            "subheadline": (
                "The 11th-century temple built from red-gold sandstone, with no "
                "presiding deity and the finest nayika figures in Odisha — a "
                "short stop between Mukteshwar and Lingaraj."
            ),
            "body_text": (
                "Rajarani is the odd one out among Bhubaneswar's temples: it was "
                "never consecrated, has no presiding deity, and has therefore "
                "never been a working shrine. What it has instead is carving. "
                "Built in the 11th century from the red-and-gold sandstone that "
                "gave it its name, the tower carries rows of nayikas — women "
                "with a tree branch, fastening an anklet, playing an instrument "
                "— and mithuna panels that are the most accomplished figure "
                "work in the city.\n\n"
                "Because it is an Archaeological Survey of India monument rather "
                "than a living temple, it has an entry ticket and set hours, and "
                "the grounds are quiet in a way the working temples are not. "
                "That makes it the easiest of the Old Town sites to actually "
                "look at: no crowd management, no darshan queue, just the "
                "monument and its lawns.\n\n"
                "It sits on Tankapani Road in the Old Town, about 5 km from the "
                "centre and roughly 700 metres from Mukteshwar, which puts it "
                "squarely inside the same half-day circuit as Lingaraj and "
                "Kedar Gouri. A drop-and-wait booking is the practical shape: "
                "fifteen to twenty minutes at the temple, then on to the next "
                "stop without hunting for a cab on a residential road.\n\n"
                "The one time the temple is not quiet is the Rajarani Music "
                "Festival, held in the grounds each winter — three evenings of "
                "classical music with the tower lit behind the stage. If you are "
                "in the city that week, it is worth planning an evening around."
            ),
            "highlights": [
                {
                    "title": "Figure carving at its best",
                    "text": (
                        "The nayika and mithuna panels on the tower are the "
                        "reason this temple is studied — bring a camera and "
                        "time."
                    ),
                },
                {
                    "title": "An ASI monument, not a shrine",
                    "text": (
                        "Ticketed and quiet, with lawns and space to walk around "
                        "the tower rather than a darshan queue."
                    ),
                },
                {
                    "title": "700 m from Mukteshwar",
                    "text": (
                        "The two temples are a two-minute drive apart, which is "
                        "why they belong in one booking."
                    ),
                },
            ],
            "faqs": [
                {
                    "question": "Is there an entry fee at Rajarani Temple?",
                    "answer": (
                        "Yes. It is a ticketed ASI monument, with a modest fee "
                        "for Indian visitors and a higher one for foreign "
                        "nationals. Children below the ticket age are usually "
                        "free."
                    ),
                },
                {
                    "question": "What are the Rajarani Temple timings?",
                    "answer": (
                        "Roughly 6:30 am to 7 pm daily. As a protected monument "
                        "it keeps fixed hours rather than temple hours."
                    ),
                },
                {
                    "question": "Why does Rajarani have no deity?",
                    "answer": (
                        "The temple was never consecrated, so it has never held "
                        "a presiding deity or daily worship. It is valued as "
                        "architecture rather than as a living shrine."
                    ),
                },
                {
                    "question": "When is the Rajarani Music Festival?",
                    "answer": (
                        "It is held in the temple grounds over a few evenings "
                        "each winter. Dates change year to year, so check the "
                        "Odisha Tourism calendar before planning a trip around "
                        "it."
                    ),
                },
            ],
        },
    },
    {
        "kind": "LANDMARK",
        "name": "Chausathi Yogini Temple, Hirapur",
        "address": "Hirapur, near Balakati, Khordha district, Bhubaneswar",
        "latitude": 20.225,
        "longitude": 85.879,
        "seo_title": "Cab to Chausathi Yogini Temple, Hirapur from Bhubaneswar",
        "seo_description": (
            "Book a cab to the Chausathi Yogini temple at Hirapur, about 15 km "
            "from Bhubaneswar. A 9th-century open-air shrine of 64 yoginis, "
            "free to enter, with waiting included."
        ),
        "seo_keywords": (
            "cab to Chausathi Yogini temple, Hirapur yogini temple taxi, "
            "64 yogini temple Bhubaneswar, Chausathi Yogini timings"
        ),
        "page": {
            "eyebrow": "Cabs to Hirapur · Bhubaneswar",
            "headline": "Cab to Chausathi Yogini Temple, Hirapur",
            "subheadline": (
                "A 9th-century circular shrine of sixty-four yoginis, open to "
                "the sky, about 15 km from the city — one of only four such "
                "temples left in India."
            ),
            "body_text": (
                "The Chausathi Yogini temple at Hirapur is unlike anything else "
                "in Odisha. Built in the 9th century and attributed to Queen "
                "Hiradevi, it is a small, roofless, circular enclosure with "
                "sixty-four yogini figures set into the walls around a central "
                "shrine to Mahamaya — the tantric counterpart to the towering "
                "Kalinga temples in the city. Only four yogini temples survive "
                "in India, and two of them are in this state.\n\n"
                "It sits at Hirapur, a village on the city's eastern edge, about "
                "15 km from central Bhubaneswar — half an hour on rural roads "
                "past Balakati. There is no monument complex around it, no "
                "ticket counter, and no line of auto-rickshaws waiting at the "
                "gate, which is the practical argument for a cab that waits. "
                "The visit itself is short: thirty to forty-five minutes to "
                "read the wall of figures and the central shrine.\n\n"
                "Because it is close and quick, it pairs well with something "
                "else in the same half-day — Dhauli on the way back into the "
                "city, or the longer run out to Raghurajpur, the pattachitra "
                "village near Puri, if you want a full day of craft and "
                "heritage.\n\n"
                "Entry is free and the shrine is normally open from morning to "
                "evening. A word of context helps before you arrive: this is an "
                "active place of worship with tantric roots, so treat the "
                "enclosure with the same respect you would a working temple, "
                "and expect the interior to be small enough that a crowd of "
                "twenty is a crowd."
            ),
            "highlights": [
                {
                    "title": "Sixty-four yoginis",
                    "text": (
                        "The figures line a circular, roofless wall around the "
                        "central shrine — a form that survives in only four "
                        "places in India."
                    ),
                },
                {
                    "title": "Half an hour away",
                    "text": (
                        "About 15 km east via Balakati, so it fits into a "
                        "morning rather than taking a day."
                    ),
                },
                {
                    "title": "No ticket, no queue",
                    "text": (
                        "Free entry and a village setting — the cab waits, "
                        "because there is nothing to hail on the way back."
                    ),
                },
            ],
            "faqs": [
                {
                    "question": "How far is the Chausathi Yogini temple from Bhubaneswar?",
                    "answer": (
                        "About 15 km east of the city, near the village of "
                        "Hirapur beyond Balakati — roughly half an hour by road."
                    ),
                },
                {
                    "question": "What makes this temple different?",
                    "answer": (
                        "It is circular and open to the sky, with sixty-four "
                        "yogini figures set into the inner wall around a central "
                        "shrine to Mahamaya. It belongs to the tantric yogini "
                        "cult rather than the main temple tradition."
                    ),
                },
                {
                    "question": "Is there an entry fee or a dress code?",
                    "answer": (
                        "Entry is free. It is an active shrine, so modest dress "
                        "and quiet are expected, and photography inside the "
                        "enclosure is best kept brief."
                    ),
                },
                {
                    "question": "How long should I plan for the visit?",
                    "answer": (
                        "Thirty to forty-five minutes at the shrine. Most "
                        "visitors combine it with Dhauli on the same half-day "
                        "booking."
                    ),
                },
            ],
        },
    },
    {
        "kind": "LANDMARK",
        "name": "ISKCON Sri Krishna Balaram Temple",
        "address": "NH-5, IRC Village, Nayapalli, Bhubaneswar 751015",
        "latitude": 20.2965,
        "longitude": 85.825,
        "seo_title": "Cab to ISKCON Temple Bhubaneswar — Nayapalli Drop",
        "seo_description": (
            "Book a cab to the ISKCON Sri Krishna Balaram temple in Nayapalli, "
            "Bhubaneswar. About 7 km from the station, drop at the gate, and "
            "early-morning aarti pickups."
        ),
        "seo_keywords": (
            "cab to ISKCON temple Bhubaneswar, Krishna Balaram temple taxi, "
            "ISKCON Nayapalli cab, ISKCON temple timings Bhubaneswar"
        ),
        "page": {
            "eyebrow": "Cabs to ISKCON · Bhubaneswar",
            "headline": "Cab to ISKCON Sri Krishna Balaram Temple",
            "subheadline": (
                "The Krishna-Balaram temple in Nayapalli, on the NH-5 side of "
                "the city — easiest as a drop, with a pre-dawn pickup for the "
                "first aarti."
            ),
            "body_text": (
                "Bhubaneswar's ISKCON temple, dedicated to Krishna and Balaram, "
                "sits in Nayapalli just off NH-5 — about 7 km from the railway "
                "station and a short run from the airport side of the city. It "
                "is a working temple rather than a monument: the day starts with "
                "mangal aarti before dawn, runs through the morning classes, "
                "closes in the afternoon, and reopens for the evening aarti.\n\n"
                "That schedule is what makes a cab useful rather than merely "
                "convenient. The morning aarti is early enough that public "
                "transport is thin, and the evening aarti ends after most buses "
                "have thinned out. A booked drop at either end, or a "
                "drop-and-wait if you want to stay for the class afterwards, "
                "removes the timing problem entirely.\n\n"
                "For visitors rather than devotees, the temple is worth an hour: "
                "the courtyard, the deity hall, the bookshop and the prasadam "
                "counter are all inside the same compound, and entry is free. "
                "Janmashtami is the big occasion of the year, when the whole "
                "lane fills and traffic is diverted — plan to arrive well "
                "before the stated time on that day.\n\n"
                "It also works as a practical landmark: the temple is on the "
                "northern edge of the city, so a drop here fits naturally on "
                "the way to Nandankanan or on an airport run, rather than "
                "needing its own out-and-back from the Old Town."
            ),
            "highlights": [
                {
                    "title": "Pre-dawn pickups",
                    "text": (
                        "Mangal aarti is before sunrise, when there is almost no "
                        "public transport running — a booked cab is the reliable "
                        "way to make it."
                    ),
                },
                {
                    "title": "On the NH-5 side",
                    "text": (
                        "Nayapalli sits towards the airport and Nandankanan, so "
                        "the temple fits on the way to either."
                    ),
                },
                {
                    "title": "Free entry, working temple",
                    "text": (
                        "No ticket, but the hall closes for a few hours in the "
                        "afternoon — worth knowing before you set out."
                    ),
                },
            ],
            "faqs": [
                {
                    "question": "What are the ISKCON Bhubaneswar temple timings?",
                    "answer": (
                        "The day begins with mangal aarti in the very early "
                        "morning and the deity hall closes for a few hours after "
                        "midday, reopening in the late afternoon until about "
                        "8:30 pm. Confirm the day's schedule before a specific "
                        "aarti."
                    ),
                },
                {
                    "question": "How far is ISKCON temple from Bhubaneswar railway station?",
                    "answer": (
                        "About 7 km, in Nayapalli just off NH-5. It is closer to "
                        "the airport than to the Old Town."
                    ),
                },
                {
                    "question": "Is there an entry fee?",
                    "answer": (
                        "No. Entry is free and the temple is funded by "
                        "donations. Prasadam at the counter is charged separately."
                    ),
                },
                {
                    "question": "When is the best time to visit?",
                    "answer": (
                        "Early morning for the aarti or early evening for the "
                        "sandhya aarti. Janmashtami is the biggest festival and "
                        "the busiest day of the year."
                    ),
                },
            ],
        },
    },
    {
        "kind": "LANDMARK",
        "name": "Atri Hot Springs",
        "address": "Atri, near Kaipadar, Khordha district, Odisha",
        "latitude": 20.116,
        "longitude": 85.52,
        "seo_title": "Cab to Atri Hot Springs from Bhubaneswar — Day Trip",
        "seo_description": (
            "Book a cab to the Atri sulphur springs, about 42 km from "
            "Bhubaneswar via Khordha. A warm perennial spring beside the "
            "Hatakeswar temple, with the driver waiting."
        ),
        "seo_keywords": (
            "cab to Atri hot springs, Atri hot spring taxi Bhubaneswar, "
            "Hatakeswar temple Atri, Bhubaneswar day trip hot springs"
        ),
        "page": {
            "eyebrow": "Cabs to Atri · Bhubaneswar",
            "headline": "Cab to Atri Hot Springs",
            "subheadline": (
                "A sulphur spring that runs warm all year, next to the "
                "Hatakeswar Shiva temple about 42 km from the city — a rural "
                "half-day where the return ride is the part you cannot improvise."
            ),
            "body_text": (
                "Atri is a natural perennial spring near Kaipadar in Khordha "
                "district, and the water comes out warm — around 55°C — every "
                "day of the year, which is unusual enough in this part of "
                "Odisha that people travel out from the city for it. The "
                "sulphur-rich water is channelled into bathing tanks beside the "
                "spring, and the Hatakeswar Shiva temple stands in the same "
                "compound.\n\n"
                "It is about 42 km from Bhubaneswar via Khordha town — an hour "
                "and a quarter on a good day, longer with traffic leaving the "
                "city. The road is ordinary state highway and village road, and "
                "the last stretch has no landmarks beyond the turning, which is "
                "why a driver who knows the site is worth having.\n\n"
                "The visit is simple: a bath in the tanks, a darshan at the "
                "temple, and time in the shade. Two hours covers it comfortably. "
                "What makes a cab the sensible way to do it is the return — "
                "this is a village site with no stream of passing taxis, so a "
                "drop-and-wait booking means you leave when you are ready "
                "rather than when something happens to come along.\n\n"
                "Makar Sankranti in mid-January is the big day here, when a "
                "large fair fills the site — spectacular, but the road in "
                "jams and a cab cannot get to the gate. On any other day it is "
                "quiet, and the winter months are the most comfortable for "
                "standing next to a hot spring."
            ),
            "highlights": [
                {
                    "title": "Warm water, all year",
                    "text": (
                        "A perennial sulphur spring at roughly 55°C, feeding "
                        "bathing tanks beside the temple — the reason people "
                        "make the drive."
                    ),
                },
                {
                    "title": "A 42 km half-day",
                    "text": (
                        "Out via Khordha in about an hour and a quarter; two "
                        "hours on site makes a comfortable morning or afternoon."
                    ),
                },
                {
                    "title": "The ride back matters",
                    "text": (
                        "A village site with no passing taxis — booked waiting "
                        "is what keeps the trip simple."
                    ),
                },
            ],
            "faqs": [
                {
                    "question": "How far is Atri from Bhubaneswar?",
                    "answer": (
                        "About 42 km via Khordha town, roughly an hour and a "
                        "quarter by road depending on traffic leaving the city."
                    ),
                },
                {
                    "question": "What is at Atri besides the spring?",
                    "answer": (
                        "The Hatakeswar Shiva temple stands in the same "
                        "compound, and the hot water is channelled into public "
                        "bathing tanks beside it."
                    ),
                },
                {
                    "question": "How long should I plan to stay?",
                    "answer": (
                        "About two hours covers a bath, a darshan and a rest. "
                        "Most visitors make it a morning or an afternoon rather "
                        "than a full day."
                    ),
                },
                {
                    "question": "When is the best time to visit Atri?",
                    "answer": (
                        "October to February is the most comfortable. Makar "
                        "Sankranti in January brings a large fair, which is "
                        "worth seeing but makes access slow."
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
            # The SEO block is authored once per landmark and serves both the
            # Place override and the landing page.
            _fill(page, "seo_title", content.get("seo_title") or item["seo_title"])
            _fill(
                page,
                "seo_description",
                content.get("seo_description") or item["seo_description"],
            )
            _fill(
                page,
                "seo_keywords",
                content.get("seo_keywords") or item["seo_keywords"],
            )
            if not page.highlights:
                page.highlights = content["highlights"]
            if not page.faqs:
                page.faqs = content["faqs"]
            # Order follows the list, so re-running after appending a landmark
            # renumbers the whole set deterministically instead of leaving the
            # new pages on the provisioning default.
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
