"""
WayTero — Odisha tour package content for the admin bulk seed (run on VPS).

Ten packages for the single active partner (PRT-379499 Maa Travels), mirroring
the format of the existing package TP-20260913-0001 "Jagannath Dham –
Bhubaneswar to Puri" (city-scoped, FIXED type, per-group pricing slabs by
persons_count, rich inclusions/exclusions, day-wise itinerary with activity
lists). All copy is written in-house for WayTero — no text lifted from any
other website. Images are intentionally NOT set here; admin adds/updates
images from the admin dashboard afterwards.
"""

# City ids present on the VPS: 1 Berhampur, 2 Rourkela, 3 Cuttack, 4 Puri,
# 5 Bhubaneswar (all Odisha).
CITY_BERHAMPUR = 1
CITY_ROURKELA = 2
CITY_CUTTACK = 3
CITY_PURI = 4
CITY_BHUBANESWAR = 5

SHARED_TERMS = """Package confirmation is subject to availability of hotels and transport at the time of booking.
Hotel rooms are provided on a double/twin-sharing basis unless clearly stated otherwise.
Standard hotel check-in and check-out timings apply; early check-in or late check-out is on request and subject to hotel policy.
Sightseeing points mentioned in the itinerary are covered subject to time, weather, road conditions, local regulations and operational feasibility on the day of travel.
Darshan and entry at temples, wildlife sanctuaries, national parks and restricted areas are governed by the rules, timings and permit requirements of the respective authorities; entry may be refused or curtailed for reasons beyond our control.
Vehicle usage is restricted to the itinerary route. Additional detours or extra kilometres requested by travellers may attract extra charges, payable directly.
Travellers must carry a valid photo ID (Aadhaar, passport, voter ID or driving licence) at all times; some forest destinations require ID copies for permits.
Wildlife safaris, boat rides, adventure activities and entries not included in the package are optional and payable directly by the traveller.
The itinerary sequence may be adjusted by the local team for weather, road blocks, strikes, crowd control or circumstances beyond reasonable control.
Cancellations and refunds follow the cancellation policy published on the package page and applicable platform rules.
WayTero acts as a booking platform connecting travellers with the listed operator; the operator is responsible for delivering the tour services booked.
Any dispute is subject to the jurisdiction of Indian courts as per the platform terms of service."""

SHARED_INCLUSIONS = [
    "Accommodation on double/twin-sharing basis as per itinerary",
    "Daily breakfast at hotels (as indicated in the itinerary)",
    "Private air-conditioned vehicle for all transfers and sightseeing",
    "Experienced local driver with route knowledge",
    "Driver allowance, fuel, parking and toll charges",
    "Airport / railway station pickup and drop as per itinerary",
    "All sightseeing mentioned in the day-wise itinerary",
    "Tour coordination and on-trip assistance by phone",
    "All applicable vehicle taxes and permits",
]

SHARED_EXCLUSIONS = [
    "Airfare / train fare to and from the tour starting city",
    "Lunch and dinner unless specifically mentioned",
    "Entry fees, safari charges, camera fees and boating charges",
    "Special darshan or VIP passes at temples",
    "Room service, laundry, telephone and minibar expenses",
    "Personal expenses such as shopping, tips and porterage",
    "Travel insurance",
    "Anything not specifically mentioned under inclusions",
    "Cost arising out of natural events, strikes, road blocks or vehicle breakdown",
]

PACKAGES = [
    # ─────────────────────────────────────────────────────────────
    # 1. Golden Triangle of Odisha
    # ─────────────────────────────────────────────────────────────
    {
        "package_name": "Golden Triangle of Odisha — Temples, Beaches & Heritage",
        "slug": "golden-triangle-of-odisha",
        "package_code": "TP-OD-GLT-04",
        "package_type": "FIXED",
        "destination": "Bhubaneswar – Puri – Konark",
        "city_id": CITY_BHUBANESWAR,
        "duration_days": 4,
        "duration_nights": 3,
        "minimum_persons": 2,
        "maximum_persons": 12,
        "short_description": (
            "Cover Odisha's classic circuit in four days — the ancient temples "
            "of Bhubaneswar, the sacred beach town of Puri and the UNESCO-listed "
            "Konark Sun Temple, with stops at Raghurajpur, Pipli and "
            "Chandrabhaga beach."
        ),
        "description": (
            "The Golden Triangle of Odisha links three worlds in one short "
            "trip: the temple city, the dham and the sun chariot.\n\n"
            "Start in Bhubaneswar with its living temple heritage — Lingaraj, "
            "Mukteswar and Rajarani — and the Jain caves of Udayagiri and "
            "Khandagiri that once sheltered wandering monks. Walk through "
            "Ekamra Haat to see handloom and stone craft from across the "
            "state.\n\n"
            "Continue to Puri along the applique road, stopping at Pipli and "
            "Raghurajpur, where artists paint patachitra in open courtyards. "
            "At Puri, take darshan at Shri Jagannath Temple, walk the beach at "
            "sunset and watch the fishing boats come in.\n\n"
            "The route onward to Konark follows the scenic marine drive past "
            "Chandrabhaga beach — one of the best sunrise points on this coast "
            "— before arriving at the Sun Temple, a 13th-century stone chariot "
            "carved with wheels, dancers and scenes of everyday life.\n\n"
            "A balanced mix of darshan, heritage, craft villages and beaches "
            "makes this the ideal first visit to Odisha for families, couples "
            "and small groups."
        ),
        "itinerary": [
            {
                "day_number": 1,
                "title": "Arrival in Bhubaneswar | Temple City Sightseeing",
                "description": (
                    "Arrive at Bhubaneswar airport or railway station and check "
                    "in to your hotel. In the afternoon set out for the heritage "
                    "quarter — Lingaraj Temple, Mukteswar Temple with its carved "
                    "torana arch, Rajarani Temple, and the Jain caves of "
                    "Udayagiri and Khandagiri. Close the day with a stroll "
                    "through Ekamra Haat for handloom and handicraft shopping."
                ),
                "activities": [
                    "Pickup from Bhubaneswar Airport / Railway Station / Hotel",
                    "Hotel check-in",
                    "Lingaraj Temple visit",
                    "Mukteswar Temple visit",
                    "Rajarani Temple visit",
                    "Udayagiri & Khandagiri Jain Caves",
                    "Ekamra Haat shopping stop",
                    "Overnight stay in Bhubaneswar",
                ],
                "overnight_city_id": CITY_BHUBANESWAR,
            },
            {
                "day_number": 2,
                "title": "Bhubaneswar to Puri | Artisan Villages En Route",
                "description": (
                    "After breakfast drive towards Puri. Break the journey at "
                    "Pipli to browse the famous applique market and at "
                    "Raghurajpur, a heritage crafts village where patachitra "
                    "artists work in open courtyards. Reach Puri, check in, and "
                    "proceed for darshan at Shri Jagannath Temple followed by an "
                    "evening on Puri beach."
                ),
                "activities": [
                    "Breakfast at hotel",
                    "Drive Bhubaneswar to Puri",
                    "Pipli Applique Village stop",
                    "Raghurajpur Heritage Crafts Village visit",
                    "Hotel check-in at Puri",
                    "Shri Jagannath Temple darshan",
                    "Sunset at Puri Beach",
                    "Overnight stay in Puri",
                ],
                "overnight_city_id": CITY_PURI,
            },
            {
                "day_number": 3,
                "title": "Konark Excursion | Sun Temple & Chandrabhaga Beach",
                "description": (
                    "Leave after breakfast on the scenic marine drive to "
                    "Konark. Spend the morning at the Sun Temple — a colossal "
                    "stone chariot carved with wheels, horses and scenes of "
                    "medieval life. Continue to Chandrabhaga beach and the "
                    "fishing hamlet nearby, then return to Puri with a stop at "
                    "Ramachandi if time allows."
                ),
                "activities": [
                    "Breakfast at hotel",
                    "Marine drive from Puri to Konark",
                    "Konark Sun Temple guided visit",
                    "Konark Archaeological Museum (optional)",
                    "Chandrabhaga Beach visit",
                    "Ramachandi stop on return",
                    "Evening free in Puri",
                    "Overnight stay in Puri",
                ],
                "overnight_city_id": CITY_PURI,
            },
            {
                "day_number": 4,
                "title": "Puri to Bhubaneswar | Departure",
                "description": (
                    "Enjoy breakfast and a free morning at the beach or the "
                    "Swargadwar market. Later drive back to Bhubaneswar with an "
                    "optional stop at Dhauli Shanti Stupa, the peace pagoda on "
                    "the banks of the Daya river, and transfer to the airport "
                    "or railway station for your onward journey."
                ),
                "activities": [
                    "Breakfast at hotel",
                    "Hotel check-out",
                    "Swargadwar Market visit (optional)",
                    "Optional Dhauli Shanti Stupa stop",
                    "Drive Puri to Bhubaneswar",
                    "Airport / Railway Station drop-off",
                ],
                "overnight_city_id": None,
            },
        ],
        "pricing": [
            {"persons_count": 2, "package_price": 14500.00},
            {"persons_count": 4, "package_price": 24000.00},
            {"persons_count": 6, "package_price": 31000.00},
            {"persons_count": 8, "package_price": 38000.00},
            {"persons_count": 10, "package_price": 44000.00},
            {"persons_count": 12, "package_price": 50000.00},
        ],
        "inclusions": SHARED_INCLUSIONS
        + [
            "3 nights hotel stay (1 night Bhubaneswar, 2 nights Puri)",
            "Raghurajpur and Pipli village visits",
            "Konark Sun Temple guided walk",
        ],
        "exclusions": SHARED_EXCLUSIONS + ["Konark Museum entry fee"],
        "terms": SHARED_TERMS,
    },
    # ─────────────────────────────────────────────────────────────
    # 2. Jagannath Puri Spiritual Getaway
    # ─────────────────────────────────────────────────────────────
    {
        "package_name": "Jagannath Puri Spiritual Getaway with Konark Darshan",
        "slug": "jagannath-puri-spiritual-getaway",
        "package_code": "TP-OD-PRI-03",
        "package_type": "FIXED",
        "destination": "Puri – Konark",
        "city_id": CITY_PURI,
        "duration_days": 3,
        "duration_nights": 2,
        "minimum_persons": 2,
        "maximum_persons": 12,
        "short_description": (
            "A relaxed three-day pilgrimage based in Puri — Jagannath Temple "
            "darshan, the Maths and tanks of the holy town, a full Konark "
            "excursion and unhurried evenings on the beach."
        ),
        "description": (
            "Puri rewards travellers who stay a little longer. Beyond the "
            "great temple lies a whole sacred geography — the Gundicha house "
            "of the lord, the Narendra tank where the boat festival floats, "
            "centuries-old Maths and the long arc of the Bay of Bengal.\n\n"
            "This three-day itinerary keeps a single base in Puri so that "
            "senior citizens and families can settle in without daily packing. "
            "Mornings are reserved for darshan when the temple is most "
            "serene; afternoons carry you to Konark's Sun Temple along the "
            "marine drive; evenings stay free for the beach, bhog prasad and "
            "the buzz of Swargadwar market.\n\n"
            "Pickup and drop connect at Bhubaneswar, so trains and flights "
            "from any city slot cleanly into the plan. Rooms are kept close "
            "to the temple area to shorten every walk."
        ),
        "itinerary": [
            {
                "day_number": 1,
                "title": "Arrival at Puri | Jagannath Darshan & Beach Evening",
                "description": (
                    "Receive at Bhubaneswar airport or railway station and "
                    "drive to Puri. After hotel check-in and some rest, "
                    "proceed for darshan at Shri Jagannath Temple. In the "
                    "evening walk to the beach near Swargadwar and enjoy the "
                    "sea breeze before dinner."
                ),
                "activities": [
                    "Pickup from Bhubaneswar Airport / Railway Station",
                    "Drive to Puri and hotel check-in",
                    "Shri Jagannath Temple darshan",
                    "Gundicha Temple visit",
                    "Evening at Puri Beach",
                    "Overnight stay in Puri",
                ],
                "overnight_city_id": CITY_PURI,
            },
            {
                "day_number": 2,
                "title": "Konark Excursion | Sun Temple & Chandrabhaga",
                "description": (
                    "After breakfast, take the coastal road to Konark. Explore "
                    "the Sun Temple and its carved wheels that double as "
                    "sundials, then continue to Chandrabhaga beach where the "
                    "river meets the sea. Return by afternoon and spend the "
                    "evening visiting Lokanath Temple and Narendra Tank."
                ),
                "activities": [
                    "Breakfast at hotel",
                    "Drive to Konark via marine drive",
                    "Konark Sun Temple visit",
                    "Chandrabhaga Beach stop",
                    "Return drive to Puri",
                    "Lokanath Temple visit",
                    "Narendra Tank visit",
                    "Overnight stay in Puri",
                ],
                "overnight_city_id": CITY_PURI,
            },
            {
                "day_number": 3,
                "title": "Puri Temples & Departure",
                "description": (
                    "Spend the morning at leisure — an early beach walk, "
                    "prasad at the temple or shopping for applique and stone "
                    "craft. After breakfast and check-out, drive back to "
                    "Bhubaneswar with an optional Dhauli stop and connect to "
                    "your onward train or flight."
                ),
                "activities": [
                    "Breakfast at hotel",
                    "Hotel check-out",
                    "Free time for beach / market",
                    "Optional Dhauli Shanti Stupa stop",
                    "Drive Puri to Bhubaneswar",
                    "Airport / Railway Station drop-off",
                ],
                "overnight_city_id": None,
            },
        ],
        "pricing": [
            {"persons_count": 2, "package_price": 12500.00},
            {"persons_count": 4, "package_price": 19500.00},
            {"persons_count": 6, "package_price": 25000.00},
            {"persons_count": 8, "package_price": 30000.00},
            {"persons_count": 10, "package_price": 35000.00},
            {"persons_count": 12, "package_price": 39500.00},
        ],
        "inclusions": SHARED_INCLUSIONS
        + [
            "2 nights hotel stay in Puri near the temple area",
            "Konark Sun Temple excursion",
        ],
        "exclusions": SHARED_EXCLUSIONS,
        "terms": SHARED_TERMS,
    },
    # ─────────────────────────────────────────────────────────────
    # 3. Chilika Lake & Dolphin Point
    # ─────────────────────────────────────────────────────────────
    {
        "package_name": "Chilika Lake Escape — Dolphin Point, Islands & Nalabana",
        "slug": "chilika-lake-dolphin-point-escape",
        "package_code": "TP-OD-CHL-03",
        "package_type": "FIXED",
        "destination": "Chilika (Satapada / Barkul) – Puri",
        "city_id": CITY_BERHAMPUR,
        "duration_days": 3,
        "duration_nights": 2,
        "minimum_persons": 2,
        "maximum_persons": 10,
        "short_description": (
            "Two nights around Asia's largest brackish-water lagoon — boat "
            "rides to Irrawaddy dolphin waters at Satapada, birding at "
            "Nalabana island and a calm night by the lake at Barkul."
        ),
        "description": (
            "Chilika is not a sight; it is a stretch of weather. The lagoon "
            "changes colour with the sky, fishing boats stitch wakes across "
            "it, and in winter the horizon fills with wings from as far away "
            "as Siberia and the Caspian.\n\n"
            "This plan pairs the two best faces of the lake. At Satapada, "
            "where the lagoon meets the Bay of Bengal through a narrow mouth, "
            "boats nose out at first light for the resident Irrawaddy "
            "dolphins. At Barkul, the quieter side, you board smaller country "
            "boats towards Nalabana bird sanctuary and the miniature "
            "Kalijai temple on its own islet.\n\n"
            "Between boat rides there is crab and fresh fish at the shore "
            "shacks, a drive along the old Puri road and time to simply sit "
            "by the water. An easy, restful trip for couples, photographers "
            "and birdwatchers — best from November to February."
        ),
        "itinerary": [
            {
                "day_number": 1,
                "title": "Arrival via Bhubaneswar | To Barkul on the Lake",
                "description": (
                    "Pickup at Bhubaneswar airport or railway station and "
                    "drive south to Barkul on the western shore of Chilika. "
                    "Check in to the lake-view resort. In the evening take a "
                    "short boat ride to Kalijai island temple and watch the "
                    "sunset over the water."
                ),
                "activities": [
                    "Pickup from Bhubaneswar Airport / Railway Station",
                    "Drive to Barkul (Chilika)",
                    "Lake-view resort check-in",
                    "Evening boat ride to Kalijai Island",
                    "Sunset by the lagoon",
                    "Overnight stay at Barkul",
                ],
                "overnight_city_id": CITY_BERHAMPUR,
            },
            {
                "day_number": 2,
                "title": "Nalabana Birding & Transfer to Satapada",
                "description": (
                    "Take an early country boat towards Nalabana bird "
                    "sanctuary — a submerged island that turns into one of "
                    "Asia's great wintering grounds between November and "
                    "March. After breakfast, drive around the lake to "
                    "Satapada on the southern shore, the base for Irrawaddy "
                    "dolphin trips near the sea mouth."
                ),
                "activities": [
                    "Early morning Nalabana bird sanctuary boat ride",
                    "Breakfast at resort",
                    "Drive Barkul to Satapada around the lake",
                    "Check-in at Satapada lakeside stay",
                    "Evening walk along the lagoon shore",
                    "Overnight stay at Satapada",
                ],
                "overnight_city_id": CITY_BERHAMPUR,
            },
            {
                "day_number": 3,
                "title": "Dolphin Point Boat Ride & Departure",
                "description": (
                    "Board a motor boat at first light for the sea mouth — "
                    "the stretch of open water where Irrawaddy dolphins feed. "
                    "Walk the sandspit between lake and sea before returning "
                    "for breakfast. Check out and drive to Puri or "
                    "Bhubaneswar for your onward journey, with an optional "
                    "stop at Alarnath temple on the way."
                ),
                "activities": [
                    "Sunrise boat ride to Chilika sea mouth",
                    "Irrawaddy dolphin spotting",
                    "Sandspit walk between lake and sea",
                    "Breakfast and hotel check-out",
                    "Optional Alarnath Temple stop",
                    "Drop at Puri or Bhubaneswar",
                ],
                "overnight_city_id": None,
            },
        ],
        "pricing": [
            {"persons_count": 2, "package_price": 13500.00},
            {"persons_count": 4, "package_price": 21500.00},
            {"persons_count": 6, "package_price": 28000.00},
            {"persons_count": 8, "package_price": 33500.00},
            {"persons_count": 10, "package_price": 38500.00},
        ],
        "inclusions": SHARED_INCLUSIONS
        + [
            "2 nights lakeside stay (1 Barkul, 1 Satapada)",
            "Kalijai island boat ride",
            "Nalabana sanctuary boat ride (winter season)",
            "Satapada sea-mouth motor boat ride",
        ],
        "exclusions": SHARED_EXCLUSIONS
        + ["Additional boating hours beyond the included rides"],
        "terms": SHARED_TERMS,
    },
    # ─────────────────────────────────────────────────────────────
    # 4. Bhitarkanika Wildlife
    # ─────────────────────────────────────────────────────────────
    {
        "package_name": "Bhitarkanika Wilderness — Mangroves, Crocodiles & Kingfishers",
        "slug": "bhitarkanika-mangrove-wilderness",
        "package_code": "TP-OD-BTK-04",
        "package_type": "FIXED",
        "destination": "Bhitarkanika National Park",
        "city_id": CITY_CUTTACK,
        "duration_days": 4,
        "duration_nights": 3,
        "minimum_persons": 2,
        "maximum_persons": 8,
        "short_description": (
            "Three nights in India's second-largest mangrove ecosystem — "
            "creek safaris past gharial sandbanks, a climb up the hunting "
            "tower of Dangmal, bagagahana heronry and village walks in the "
            "Khola delta."
        ),
        "description": (
            "Bhitarkanika feels older than everything around it. Tidal creeks "
            "branch through root-tangled mangrove forest; kingfishers flash "
            "electric blue over brown water; and on the mud banks, saltwater "
            "crocodiles — some of the largest in the world — bask without "
            "hurry.\n\n"
            "This four-day plan keeps you inside the rhythm of the park. From "
            "a riverside eco-resort near Khola or Dangmal, you take licensed "
            "boat safaris into the creeks, walk the boardwalk trail through "
            "the Heritiera forest, and climb the Dangmal watchtower where a "
            "museum tells the story of this delta. In winter the Bagagahana "
            "heronry holds thousands of nesting open-billed storks — one of "
            "the largest colonies in Asia.\n\n"
            "Between safaris, the food is local and fresh, the nights "
            "unplugged, and the silence deep. A true wilderness escape for "
            "wildlife lovers, photographers and anyone tired of the city — "
            "closed seasonally during the estuarine crocodile nesting window "
            "(usually mid-May to mid-July)."
        ),
        "itinerary": [
            {
                "day_number": 1,
                "title": "Arrival via Cuttack / Bhubaneswar | To Khola Gate",
                "description": (
                    "Pickup from Cuttack or Bhubaneswar and drive through "
                    "coastal Odisha to the Khola gate of Bhitarkanika. Check "
                    "in to your riverside eco-stay. In the evening walk the "
                    "village paths and watch the tide come in over the "
                    "mudflats."
                ),
                "activities": [
                    "Pickup from Cuttack / Bhubaneswar",
                    "Drive to Khola (Bhitarkanika)",
                    "Eco-stay check-in",
                    "Evening village and riverside walk",
                    "Dinner and overnight at eco-stay",
                ],
                "overnight_city_id": CITY_CUTTACK,
            },
            {
                "day_number": 2,
                "title": "Creek Safari | Dangmal Watchtower & Museum",
                "description": (
                    "Board the morning boat safari into the core creeks. Glide "
                    "past basking crocodiles, spotted deer coming to drink and "
                    "a riot of kingfishers. Disembark at Dangmal for the "
                    "boardwalk trail, the hunting tower view over the forest "
                    "canopy and the interpretation centre museum."
                ),
                "activities": [
                    "Morning boat safari in tidal creeks",
                    "Crocodile and bird spotting",
                    "Dangmal boardwalk mangrove trail",
                    "Dangmal watchtower climb",
                    "Interpretation centre & museum visit",
                    "Evening at leisure by the river",
                    "Overnight at eco-stay",
                ],
                "overnight_city_id": CITY_CUTTACK,
            },
            {
                "day_number": 3,
                "title": "Bagagahana Heronry & Gupti Creek",
                "description": (
                    "An early boat towards Bagagahana, the heronry where "
                    "thousands of open-billed storks nest in the treetops "
                    "(seasonal). Continue by boat to Gupti through narrow "
                    "creeks, returning by afternoon. Sunset hour is for the "
                    "riverfront — deer herds often graze the lawns of the "
                    "eco-stay at dusk."
                ),
                "activities": [
                    "Early boat ride to Bagagahana heronry (seasonal)",
                    "Boat transit to Gupti creek",
                    "Return creek cruise",
                    "Riverside sunset watching",
                    "Campfire evening at eco-stay",
                    "Overnight at eco-stay",
                ],
                "overnight_city_id": CITY_CUTTACK,
            },
            {
                "day_number": 4,
                "title": "Departure via Ekakula Option | Return Drive",
                "description": (
                    "After breakfast, check out. Travelers with extra energy "
                    "can opt for a longer boat towards the Ekakula sandspit "
                    "where the Bay of Bengal meets the mangroves (advance "
                    "permission and extra boat hours apply). Later drive back "
                    "to Cuttack or Bhubaneswar for onward connection."
                ),
                "activities": [
                    "Breakfast and eco-stay check-out",
                    "Optional Ekakula sandspit boat trip (extra cost)",
                    "Drive back to Cuttack / Bhubaneswar",
                    "Airport / Railway Station drop-off",
                ],
                "overnight_city_id": None,
            },
        ],
        "pricing": [
            {"persons_count": 2, "package_price": 19500.00},
            {"persons_count": 4, "package_price": 31000.00},
            {"persons_count": 6, "package_price": 40500.00},
            {"persons_count": 8, "package_price": 49000.00},
        ],
        "inclusions": SHARED_INCLUSIONS
        + [
            "3 nights riverside eco-stay near the park",
            "Two core-creek boat safaris with licensed boatman",
            "Dangmal watchtower, boardwalk and museum visit",
            "Bagagahana heronry boat ride (seasonal)",
            "National park entry permits and camera fee assistance",
        ],
        "exclusions": SHARED_EXCLUSIONS
        + ["Ekakula sandspit extended boat trip", "Video camera fee if charged by the park"],
        "terms": SHARED_TERMS,
    },
    # ─────────────────────────────────────────────────────────────
    # 5. Daringbadi Hill Escape
    # ─────────────────────────────────────────────────────────────
    {
        "package_name": "Daringbadi Hill Escape — Pine Forests, Coffee & Waterfalls",
        "slug": "daringbadi-hill-escape",
        "package_code": "TP-OD-DRB-04",
        "package_type": "FIXED",
        "destination": "Daringbadi – Mandasaru",
        "city_id": CITY_BERHAMPUR,
        "duration_days": 4,
        "duration_nights": 3,
        "minimum_persons": 2,
        "maximum_persons": 10,
        "short_description": (
            "Three nights in the 'Kashmir of Odisha' — pine groves and coffee "
            "gardens at 3,000 feet, the silent gorge of Mandasaru, Emu farm "
            "sunrises and star-filled nights at 10°C."
        ),
        "description": (
            "At 3,000 feet on the Kandhamal plateau, Daringbadi is Odisha's "
            "cool secret — a hill town ringed by pine plantations, coffee "
            "gardens and pepper vines, where winter nights drop near zero and "
            "frost silvers the grass on the coldest mornings.\n\n"
            "Days here move slowly and deliberately. Walk the pine forest "
            "paths as morning mist lifts, stand under the hill-view tower at "
            "sunset, taste garden-fresh coffee, and drive out to the great "
            "silent gorge of Mandasaru — a 20-kilometre canyon of stacked "
            "green ridges that the state calls a silent valley.\n\n"
            "Waterfalls thread the surrounding hills — Badangia and Doluri "
            " tumble close to town — and the emu farm delights children. "
            "Evenings are for bonfires and local food. Ideal for families, "
            "honeymooners and anyone chasing winter; December and January are "
            "the magic months."
        ),
        "itinerary": [
            {
                "day_number": 1,
                "title": "Arrival via Bhubaneswar / Berhampur | To Daringbadi",
                "description": (
                    "Pickup from Bhubaneswar or Berhampur and climb through "
                    "the ghats into Kandhamal district. Check in to your hill "
                    "resort at Daringbadi. Evening walk through the pine "
                    "forest beside the resort and a bonfire dinner under the "
                    "stars."
                ),
                "activities": [
                    "Pickup from Bhubaneswar / Berhampur",
                    "Ghat road drive to Daringbadi",
                    "Hill resort check-in",
                    "Pine forest evening walk",
                    "Bonfire at the resort",
                    "Overnight stay at Daringbadi",
                ],
                "overnight_city_id": CITY_BERHAMPUR,
            },
            {
                "day_number": 2,
                "title": "Daringbadi Local Sightseeing",
                "description": (
                    "A full day around the hill town. Sunrise at the hill-view "
                    "park, the emu farm, the coffee and pepper gardens, "
                    "Doluri river and Badangia waterfall, and the nature "
                    "park's treetop walkway. Evenings stay free for the "
                    "market and hot momos by the roadside."
                ),
                "activities": [
                    "Sunrise at Hill View Park",
                    "Emu Farm visit",
                    "Coffee & Pepper Garden walk",
                    "Doluri River stop",
                    "Badangia Waterfall visit",
                    "Nature Park & treetop canopy walk",
                    "Evening free at local market",
                    "Overnight stay at Daringbadi",
                ],
                "overnight_city_id": CITY_BERHAMPUR,
            },
            {
                "day_number": 3,
                "title": "Mandasaru Silent Valley Excursion",
                "description": (
                    "Drive out after breakfast to Mandasaru, the great gorge "
                    "of Kandhamal. Walk the short rim trail to the viewpoint, "
                    "visit the tiny village of the gorge and, on the way "
                    "back, stop at Putudi waterfall where the river drops in "
                    "a single white ribbon. Return to Daringbadi by evening."
                ),
                "activities": [
                    "Drive to Mandasaru gorge",
                    "Mandasaru view point rim walk",
                    "Mandasaru village visit",
                    "Putudi Waterfall stop on return",
                    "Evening free at resort",
                    "Overnight stay at Daringbadi",
                ],
                "overnight_city_id": CITY_BERHAMPUR,
            },
            {
                "day_number": 4,
                "title": "Departure | Descent to the Plains",
                "description": (
                    "Wake to mist over the pines, enjoy a final breakfast, "
                    "then descend the ghats. Drop at Bhubaneswar airport or "
                    "railway station, or at Berhampur, with memories of the "
                    "coolest corner of Odisha."
                ),
                "activities": [
                    "Breakfast at resort",
                    "Resort check-out",
                    "Descent via ghat road",
                    "Drop at Bhubaneswar or Berhampur",
                ],
                "overnight_city_id": None,
            },
        ],
        "pricing": [
            {"persons_count": 2, "package_price": 15500.00},
            {"persons_count": 4, "package_price": 25000.00},
            {"persons_count": 6, "package_price": 32500.00},
            {"persons_count": 8, "package_price": 39500.00},
            {"persons_count": 10, "package_price": 45500.00},
        ],
        "inclusions": SHARED_INCLUSIONS
        + [
            "3 nights hill resort stay at Daringbadi",
            "Mandasaru and Putudi excursion",
            "Bonfire evening at the resort (weather permitting)",
        ],
        "exclusions": SHARED_EXCLUSIONS,
        "terms": SHARED_TERMS,
    },
    # ─────────────────────────────────────────────────────────────
    # 6. Tribal Odisha — Koraput & Rayagada
    # ─────────────────────────────────────────────────────────────
    {
        "package_name": "Tribal Odisha Odyssey — Koraput, Rayagada & Weekly Markets",
        "slug": "tribal-odisha-koraput-rayagada",
        "package_code": "TP-OD-TRB-06",
        "package_type": "FIXED",
        "destination": "Rayagada – Jeypore – Koraput – Gupteswar",
        "city_id": CITY_BERHAMPUR,
        "duration_days": 6,
        "duration_nights": 5,
        "minimum_persons": 2,
        "maximum_persons": 6,
        "short_description": (
            "A respectful six-day journey into southern Odisha's tribal "
            "heartland — the great weekly markets of the Bonda, Gadaba and "
            "Kutia Kondh country, Deomali peak, Duduma falls and the sacred "
            "caves of Gupteswar."
        ),
        "description": (
            "Southern Odisha is another country within the state — a land of "
            "blue hills, millet fields and some of the oldest living tribal "
            "cultures of the subcontinent. This journey is built around the "
            "weekly markets, where the Bonda, Gadaba, Paraja and Kutia Kondh "
            "communities come down from their villages to trade — mornings "
            "of extraordinary colour, jewellery, maize beer and quiet "
            "negotiation.\n\n"
            "Travel is unhurried and respectful: small groups, a local "
            "guide-interpreter, and fixed photography etiquette that puts "
            "dignity first. Beyond the markets, the land itself stuns — "
            "Deomali, the highest peak of Odisha; Duduma, where the "
            "Machkund river leaps 175 metres; the cave shrine of Gupteswar "
            "by the Sabari river; and the terraced valley of the Desia "
            "Koraput coffee country.\n\n"
            "Accommodation is clean and simple — the best available in each "
            "town. This is a journey for travellers, not tourists: those "
            "happy with basic comforts in exchange for deep, real "
            "experience."
        ),
        "itinerary": [
            {
                "day_number": 1,
                "title": "Arrival via Visakhapatnam / Bhubaneswar | To Rayagada",
                "description": (
                    "Pickup from Visakhapatnam or Bhubaneswar and drive to "
                    "Rayagada, the gateway of the Kondh highlands. Evening at "
                    "leisure to rest for the market mornings ahead."
                ),
                "activities": [
                    "Pickup from Visakhapatnam / Bhubaneswar",
                    "Drive to Rayagada",
                    "Hotel check-in",
                    "Evening orientation with guide",
                    "Overnight stay at Rayagada",
                ],
                "overnight_city_id": CITY_BERHAMPUR,
            },
            {
                "day_number": 2,
                "title": "Chatikona Market | Drive to Jeypore",
                "description": (
                    "Early drive to Chatikona for the weekly market of the "
                    "Dongria Kondh — the mountain community of the Niyamgiri "
                    "hills, famous for their jewellery and shamanic "
                    "traditions. After the market, descend to the Jeypore "
                    "plains for the night."
                ),
                "activities": [
                    "Early drive to Chatikona weekly market",
                    "Dongria Kondh market visit with guide",
                    "Drive to Jeypore",
                    "Hotel check-in",
                    "Overnight stay at Jeypore",
                ],
                "overnight_city_id": CITY_BERHAMPUR,
            },
            {
                "day_number": 3,
                "title": "Onukudelli Bonda Market | Duduma Falls",
                "description": (
                    "The day of the great market: Onukudelli, where the Bonda "
                    "women — in bead necklaces and metal head-bands — and the "
                    "Gadaba in their kerang shawls trade in the open field. "
                    "Later drive to Duduma falls where the Machkund river "
                    "drops into a gorge on the Andhra border, returning to "
                    "Jeypore by evening."
                ),
                "activities": [
                    "Early drive to Onukudelli weekly market",
                    "Bonda & Gadaba market visit with guide-interpreter",
                    "Drive to Duduma Waterfall",
                    "Machkund gorge view",
                    "Return drive to Jeypore",
                    "Overnight stay at Jeypore",
                ],
                "overnight_city_id": CITY_BERHAMPUR,
            },
            {
                "day_number": 4,
                "title": "Koraput | Tribal Museum, Kolab & Deomali",
                "description": (
                    "Drive to Koraput town for the tribal museum's fine "
                    "collection, the Sabar Srikhetra Jagannath temple on the "
                    "hill, and the Kolab reservoir. In the afternoon cross "
                    "the coffee country to Deomali, Odisha's highest peak, "
                    "for the valley panorama. Overnight at Koraput or "
                    "Jeypore."
                ),
                "activities": [
                    "Drive to Koraput",
                    "Tribal Museum & Research Institute visit",
                    "Sabar Srikhetra Jagannath Temple, Koraput",
                    "Kolab Reservoir stop",
                    "Deomali Peak viewpoint",
                    "Coffee estate drive-through",
                    "Overnight stay at Koraput",
                ],
                "overnight_city_id": CITY_BERHAMPUR,
            },
            {
                "day_number": 5,
                "title": "Gupteswar Caves & Bhaludungri | To Rayagada",
                "description": (
                    "Take the forest road through the Papadahandi shola to "
                    "Gupteswar, the limestone cave shrine of Shiva by the "
                    "Sabari river, where tribal priests perform the rituals. "
                    "Wade the shallow river crossing if the season allows. "
                    "Later return towards Rayagada with a stop at the "
                    "Bhaludungri hill viewpoint."
                ),
                "activities": [
                    "Forest drive to Gupteswar",
                    "Gupteswar cave temple & limestone formations",
                    "Sabari river bank walk",
                    "Bhaludungri hill viewpoint stop",
                    "Drive to Rayagada",
                    "Overnight stay at Rayagada",
                ],
                "overnight_city_id": CITY_BERHAMPUR,
            },
            {
                "day_number": 6,
                "title": "Departure",
                "description": (
                    "After breakfast, transfer to Visakhapatnam airport or "
                    "Bhubaneswar railway station for your onward journey — "
                    "carrying a very different picture of Odisha home."
                ),
                "activities": [
                    "Breakfast at hotel",
                    "Hotel check-out",
                    "Transfer to Visakhapatnam / Bhubaneswar",
                    "Airport / Railway Station drop-off",
                ],
                "overnight_city_id": None,
            },
        ],
        "pricing": [
            {"persons_count": 2, "package_price": 32500.00},
            {"persons_count": 4, "package_price": 52000.00},
            {"persons_count": 6, "package_price": 68500.00},
        ],
        "inclusions": SHARED_INCLUSIONS
        + [
            "5 nights stay in the best available hotels (Jeypore, Koraput, Rayagada)",
            "Local tribal-market guide-interpreter for market days",
            "Gupteswar and Duduma excursions",
            "Breakfast each morning",
        ],
        "exclusions": SHARED_EXCLUSIONS
        + [
            "Photography fees or donations at markets and tribal villages",
            "Any gift or trade purchases at the weekly markets",
        ],
        "terms": SHARED_TERMS,
    },
    # ─────────────────────────────────────────────────────────────
    # 7. Buddhist Circuit
    # ─────────────────────────────────────────────────────────────
    {
        "package_name": "Ancient Buddhist Circuit — Udayagiri, Ratnagiri, Lalitgiri & Dhauli",
        "slug": "buddhist-circuit-udayagiri-ratnagiri-lalitgiri",
        "package_code": "TP-OD-BUD-04",
        "package_type": "FIXED",
        "destination": "Dhauli – Lalitgiri – Ratnagiri – Udayagiri – Langudi",
        "city_id": CITY_BHUBANESWAR,
        "duration_days": 4,
        "duration_nights": 3,
        "minimum_persons": 2,
        "maximum_persons": 10,
        "short_description": (
            "Follow the traces of the Buddha's world in Odisha — the peace "
            "pagoda at Dhauli and the Diamond Triangle of Lalitgiri, "
            "Ratnagiri and Udayagiri, where monastic universities flourished "
            "over a thousand years ago."
        ),
        "description": (
            "Long before the sea trade shifted, the hills of Jajpur held "
            "great Buddhist universities. Lalitgiri yielded relic caskets of "
            "the Buddha; Ratnagiri's monastery carved doorframes of green "
            "chlorite that still gleam; and Udayagiri — the largest of the "
            "three — steps up the hill through stupas, a stepped stupa and a "
            "monastic complex with its own water system.\n\n"
            "The circuit begins more gently at Dhauli, on the Daya river, "
            "where Emperor Ashoka is believed to have renounced war after "
            "the Kalinga battle — marked today by the white peace pagoda "
            "and two rock edicts of his edict.\n\n"
            "This is a circuit for slow travellers: archaeology lovers, "
            "photographers, pilgrims of history. Evenings return you to the "
            "comfort of Bhubaneswar, so the days can be spent entirely in "
            "the landscape of the past."
        ),
        "itinerary": [
            {
                "day_number": 1,
                "title": "Arrival in Bhubaneswar | Dhauli Peace Pagoda",
                "description": (
                    "Arrive and check in. In the afternoon drive to Dhauli on "
                    "the banks of the Daya river — the Ashokan rock edicts, "
                    "the sculpted elephant above them, and the Shanti Stupa "
                    "built by Japanese devotees. Evening free in "
                    "Bhubaneswar."
                ),
                "activities": [
                    "Pickup from Bhubaneswar Airport / Railway Station",
                    "Hotel check-in",
                    "Dhauli Shanti Stupa visit",
                    "Ashokan rock edict viewing",
                    "Evening free in Bhubaneswar",
                    "Overnight stay in Bhubaneswar",
                ],
                "overnight_city_id": CITY_BHUBANESWAR,
            },
            {
                "day_number": 2,
                "title": "Diamond Triangle | Ratnagiri & Lalitgiri",
                "description": (
                    "Full-day excursion into Jajpur district. At Ratnagiri, "
                    "climb to the great monastery, its magnificently carved "
                    "doorframe, and the museum with its collection of "
                    "Buddhist bronzes. Continue to Lalitgiri, the oldest "
                    "site of the triangle, where relic caskets were "
                    "discovered inside the mahastupa."
                ),
                "activities": [
                    "Breakfast at hotel",
                    "Drive to Ratnagiri",
                    "Ratnagiri monastery & stupas visit",
                    "Ratnagiri Museum visit",
                    "Drive to Lalitgiri",
                    "Lalitgiri mahastupa & monastery visit",
                    "Return to Bhubaneswar",
                    "Overnight stay in Bhubaneswar",
                ],
                "overnight_city_id": CITY_BHUBANESWAR,
            },
            {
                "day_number": 3,
                "title": "Udayagiri Complex & Langudi",
                "description": (
                    "Drive to Udayagiri, the grandest of the three — a "
                    "stepped brick stupa, an image of the standing Buddha, "
                    "and a monastic complex that once had its own water "
                    "reservoirs. On the return leg, stop at Langudi hill "
                    "with its rock-cut stupas and votive panels. Evening at "
                    "leisure."
                ),
                "activities": [
                    "Breakfast at hotel",
                    "Drive to Udayagiri",
                    "Udayagiri stepped stupa & monastery complex",
                    "Udayagiri site museum (subject to timings)",
                    "Langudi hill rock-cut stupas stop",
                    "Return to Bhubaneswar",
                    "Overnight stay in Bhubaneswar",
                ],
                "overnight_city_id": CITY_BHUBANESWAR,
            },
            {
                "day_number": 4,
                "title": "Langudi Optional / Departure",
                "description": (
                    "A soft final morning — for those flying late, an "
                    "optional visit to the Tribal Museum or Ekamra Haat for "
                    "crafts, then airport or station transfer. The circuit "
                    "ends with the state's modern face and its oldest "
                    "memories together."
                ),
                "activities": [
                    "Breakfast at hotel",
                    "Hotel check-out",
                    "Optional Ekamra Haat / Tribal Museum stop",
                    "Airport / Railway Station drop-off",
                ],
                "overnight_city_id": None,
            },
        ],
        "pricing": [
            {"persons_count": 2, "package_price": 15500.00},
            {"persons_count": 4, "package_price": 25500.00},
            {"persons_count": 6, "package_price": 33000.00},
            {"persons_count": 8, "package_price": 40000.00},
            {"persons_count": 10, "package_price": 46000.00},
        ],
        "inclusions": SHARED_INCLUSIONS
        + [
            "3 nights hotel stay in Bhubaneswar",
            "All three Diamond Triangle sites with museum visits",
            "Dhauli peace pagoda excursion",
        ],
        "exclusions": SHARED_EXCLUSIONS,
        "terms": SHARED_TERMS,
    },
    # ─────────────────────────────────────────────────────────────
    # 8. Complete Odisha Grand Tour
    # ─────────────────────────────────────────────────────────────
    {
        "package_name": "Complete Odisha Grand Tour — Temples, Lake, Forests & Tribes",
        "slug": "complete-odisha-grand-tour",
        "package_code": "TP-OD-GRD-08",
        "package_type": "FIXED",
        "destination": "Bhubaneswar – Puri – Konark – Chilika – Gopalpur – Taptapani – Bhitarkanika",
        "city_id": CITY_BHUBANESWAR,
        "duration_days": 8,
        "duration_nights": 7,
        "minimum_persons": 2,
        "maximum_persons": 8,
        "short_description": (
            "The full sweep of Odisha in eight days — temple heritage, "
            "Jagannath darshan, Konark, Chilika's dolphins, Gopalpur beach, "
            "the hot springs of Taptapani and the mangrove wilds of "
            "Bhitarkanika, all in one seamless private-tour loop."
        ),
        "description": (
            "For travellers who want the whole story, this eight-day loop "
            "strings together every signature landscape of the state into "
            "one road journey: the temple quarter of Bhubaneswar; the dham "
            "at Puri; the sun chariot at Konark; the dolphin waters of "
            "Chilika; the palms and promenades of Gopalpur; a steaming hot "
            "spring under saal trees at Taptapani; and finally the mangrove "
            "creeks of Bhitarkanika, where crocodiles sun themselves like "
            "logs of driftwood.\n\n"
            "Nights are distributed so that no day feels rushed: two bases "
            "at Puri, one by the lake, one at Gopalpur, one at Taptapani and "
            "one in the forest belt. Every transfer is short enough to "
            "arrive by lunch, leaving afternoons and evenings free.\n\n"
            "This is the definitive first, or fifteenth, visit to Odisha — "
            "built for families and small groups who prefer depth over "
            "speed, with the vehicle, driver, stays and boat rides handled "
            "end to end."
        ),
        "itinerary": [
            {
                "day_number": 1,
                "title": "Arrival in Bhubaneswar | Temple City",
                "description": (
                    "Arrive and check in. Afternoon covers the great "
                    "temples — Lingaraj, Mukteswar, Rajarani — and the Jain "
                    "caves of Khandagiri-Udayagiri. Evening at Ekamra Haat "
                    "for craft shopping."
                ),
                "activities": [
                    "Pickup from Bhubaneswar Airport / Railway Station",
                    "Hotel check-in",
                    "Lingaraj & Mukteswar Temple visit",
                    "Rajarani Temple visit",
                    "Khandagiri & Udayagiri Caves",
                    "Ekamra Haat visit",
                    "Overnight stay in Bhubaneswar",
                ],
                "overnight_city_id": CITY_BHUBANESWAR,
            },
            {
                "day_number": 2,
                "title": "To Puri | Raghurajpur, Pipli & Jagannath Darshan",
                "description": (
                    "Drive to Puri with craft stops at Raghurajpur and "
                    "Pipli. Check in and take darshan at Shri Jagannath "
                    "Temple. Evening free on the beach."
                ),
                "activities": [
                    "Breakfast at hotel",
                    "Pipli applique stop",
                    "Raghurajpur crafts village visit",
                    "Hotel check-in at Puri",
                    "Shri Jagannath Temple darshan",
                    "Puri Beach evening",
                    "Overnight stay in Puri",
                ],
                "overnight_city_id": CITY_PURI,
            },
            {
                "day_number": 3,
                "title": "Konark Excursion | Second Night in Puri",
                "description": (
                    "Marine drive to Konark for the Sun Temple and "
                    "Chandrabhaga beach; return by afternoon. Evening free "
                    "for the tanks and Maths of Puri."
                ),
                "activities": [
                    "Breakfast at hotel",
                    "Konark Sun Temple visit",
                    "Chandrabhaga Beach stop",
                    "Return to Puri",
                    "Narendra Tank / Lokanath Temple visit",
                    "Overnight stay in Puri",
                ],
                "overnight_city_id": CITY_PURI,
            },
            {
                "day_number": 4,
                "title": "Chilika at Satapada | Dolphin Waters",
                "description": (
                    "Short drive to Satapada. Motor boat into the sea mouth "
                    "for Irrawaddy dolphins and the sandspit between lake "
                    "and sea. Night at a lakeside resort."
                ),
                "activities": [
                    "Breakfast and check-out",
                    "Drive to Satapada (Chilika)",
                    "Motor boat ride to sea mouth",
                    "Irrawaddy dolphin spotting",
                    "Sandspit walk",
                    "Lakeside resort check-in",
                    "Overnight at Satapada",
                ],
                "overnight_city_id": CITY_BERHAMPUR,
            },
            {
                "day_number": 5,
                "title": "To Gopalpur via Berhampur | Beach Evening",
                "description": (
                    "Drive south along the coast to Gopalpur-on-Sea, the "
                    "state's most graceful beach town. Check in at a sea-view "
                    "hotel; evening on the lighthouse beach and the fish "
                    "shacks."
                ),
                "activities": [
                    "Breakfast at resort",
                    "Drive to Gopalpur-on-Sea",
                    "Sea-view hotel check-in",
                    "Gopalpur Beach & lighthouse visit",
                    "Evening free at beachfront",
                    "Overnight stay at Gopalpur",
                ],
                "overnight_city_id": CITY_BERHAMPUR,
            },
            {
                "day_number": 6,
                "title": "Taptapani Hot Springs | Night in the Saal Forest",
                "description": (
                    "Wind up into the hills to Taptapani, where a hot spring "
                    "bubbles beside the deodar and saal forest. Bathe in the "
                    "hot-water tank, visit the hilltop deer park viewpoint, "
                    "and overnight at the forest panthanivas."
                ),
                "activities": [
                    "Breakfast at hotel",
                    "Drive to Taptapani",
                    "Hot spring bathing tank visit",
                    "Hilltop viewpoint stop",
                    "Forest resort check-in",
                    "Overnight stay at Taptapani",
                ],
                "overnight_city_id": CITY_BERHAMPUR,
            },
            {
                "day_number": 7,
                "title": "To Bhitarkanika Belt | Riverside Night",
                "description": (
                    "Long but rewarding drive north-east to the Bhitarkanika "
                    "belt. Arrive by evening at the riverside eco-stay; the "
                    "night belongs to the frogs, fireflies and the tide."
                ),
                "activities": [
                    "Breakfast at resort",
                    "Drive to Bhitarkanika belt",
                    "Eco-stay check-in",
                    "Riverside evening walk",
                    "Overnight at eco-stay",
                ],
                "overnight_city_id": CITY_CUTTACK,
            },
            {
                "day_number": 8,
                "title": "Bhitarkanika Creek Safari | Departure",
                "description": (
                    "A morning boat safari into the creeks for crocodiles, "
                    "kingfishers and the mangrove boardwalk at Dangmal. "
                    "Return, breakfast, check out and drive to Cuttack or "
                    "Bhubaneswar for onward connections."
                ),
                "activities": [
                    "Morning creek boat safari",
                    "Dangmal watchtower & boardwalk",
                    "Breakfast and eco-stay check-out",
                    "Drive to Cuttack / Bhubaneswar",
                    "Airport / Railway Station drop-off",
                ],
                "overnight_city_id": None,
            },
        ],
        "pricing": [
            {"persons_count": 2, "package_price": 52500.00},
            {"persons_count": 4, "package_price": 82000.00},
            {"persons_count": 6, "package_price": 108000.00},
            {"persons_count": 8, "package_price": 132000.00},
        ],
        "inclusions": SHARED_INCLUSIONS
        + [
            "7 nights stay: Bhubaneswar (1), Puri (2), Satapada (1), "
            "Gopalpur (1), Taptapani (1), Bhitarkanika belt (1)",
            "Satapada sea-mouth motor boat ride",
            "Bhitarkanika morning creek safari",
            "Taptapani hot spring visit",
        ],
        "exclusions": SHARED_EXCLUSIONS
        + ["Extended Ekakula boat trip at Bhitarkanika"],
        "terms": SHARED_TERMS,
    },
    # ─────────────────────────────────────────────────────────────
    # 9. Gopalpur Beach & Taptapani
    # ─────────────────────────────────────────────────────────────
    {
        "package_name": "Gopalpur Beach Holiday with Taptapani Hot Springs",
        "slug": "gopalpur-beach-taptapani-hot-springs",
        "package_code": "TP-OD-GPL-04",
        "package_type": "FIXED",
        "destination": "Gopalpur-on-Sea – Taptapani – Ichhapur",
        "city_id": CITY_BERHAMPUR,
        "duration_days": 4,
        "duration_nights": 3,
        "minimum_persons": 2,
        "maximum_persons": 10,
        "short_description": (
            "Three slow days by the gentlest sea in Odisha — Gopalpur's "
            "casuarina beach and lighthouse, a saal-forest night at "
            "Taptapani's hot springs, and the weavers' country of Ichhapur "
            "and Berhampur silk."
        ),
        "description": (
            "Gopalpur-on-Sea is Odisha at its most unhurried. A cobbled "
            "beach road under casuarinas, the old lighthouse you can climb "
            "for the bay view, fishermen hauling nets at dawn, and water "
            "calm enough for long, safe swims.\n\n"
            "This plan adds the two perfect counterweights. Into the hills "
            "to Taptapani, where a hot sulphur spring feeds bathing tanks "
            "under saal forest — blissful at sunrise — with a night at the "
            "forest resort. And back through the weaving country around "
            "Berhampur and Ichhapur, home of the famous Berhampuri phoda "
            "kumbha sarees woven on pit looms.\n\n"
            "Short drives, warm water, warm food and warm evenings make "
            "this the easiest of our Odisha holidays — couples, families "
            "with children and senior travellers all fit comfortably."
        ),
        "itinerary": [
            {
                "day_number": 1,
                "title": "Arrival via Berhampur | Gopalpur Beach",
                "description": (
                    "Pickup at Berhampur station or Bhubaneswar airport and "
                    "drive to Gopalpur-on-Sea. Check in to your sea-view "
                    "hotel. Evening on the beach — swim, walk to the "
                    "lighthouse and dinner of fresh catch."
                ),
                "activities": [
                    "Pickup from Berhampur Railway Station / Bhubaneswar Airport",
                    "Sea-view hotel check-in at Gopalpur",
                    "Gopalpur Beach evening",
                    "Lighthouse visit (open hours)",
                    "Seafood dinner at beachfront",
                    "Overnight stay at Gopalpur",
                ],
                "overnight_city_id": CITY_BERHAMPUR,
            },
            {
                "day_number": 2,
                "title": "To Taptapani | Hot Springs & Forest Night",
                "description": (
                    "After a lazy breakfast, wind up into the Eastern Ghats "
                    "to Taptapani. Bathe in the hot spring tanks, walk the "
                    "forest road and visit the hill viewpoint. Night at the "
                    "panthanivas among the trees — listen for owls after "
                    "dinner."
                ),
                "activities": [
                    "Breakfast at hotel",
                    "Drive to Taptapani",
                    "Hot spring bathing tanks",
                    "Forest walk & hill viewpoint",
                    "Forest resort check-in",
                    "Overnight stay at Taptapani",
                ],
                "overnight_city_id": CITY_BERHAMPUR,
            },
            {
                "day_number": 3,
                "title": "Weaving Country | Return to Gopalpur",
                "description": (
                    "Descend through the weavers' belt. Watch the pit looms "
                    "at Ichhapur and the silk sari workshops of "
                    "Berhampur — with a chance to buy directly from the "
                    "weavers. Return to Gopalpur by afternoon for a final "
                    "sunset swim."
                ),
                "activities": [
                    "Breakfast at resort",
                    "Ichhapur handloom village visit",
                    "Berhampur silk saree workshop stop",
                    "Return drive to Gopalpur",
                    "Evening free at the beach",
                    "Overnight stay at Gopalpur",
                ],
                "overnight_city_id": CITY_BERHAMPUR,
            },
            {
                "day_number": 4,
                "title": "Departure",
                "description": (
                    "A final sunrise walk, breakfast and check-out, then "
                    "drop at Berhampur station or Bhubaneswar airport for "
                    "your journey onward."
                ),
                "activities": [
                    "Sunrise beach walk",
                    "Breakfast and hotel check-out",
                    "Drop at Berhampur / Bhubaneswar",
                    "Airport / Railway Station drop-off",
                ],
                "overnight_city_id": None,
            },
        ],
        "pricing": [
            {"persons_count": 2, "package_price": 16500.00},
            {"persons_count": 4, "package_price": 26500.00},
            {"persons_count": 6, "package_price": 34500.00},
            {"persons_count": 8, "package_price": 41500.00},
            {"persons_count": 10, "package_price": 47500.00},
        ],
        "inclusions": SHARED_INCLUSIONS
        + [
            "3 nights stay: Gopalpur sea-view (2) and Taptapani forest resort (1)",
            "Taptapani hot spring visit",
            "Ichhapur weaving village visit",
        ],
        "exclusions": SHARED_EXCLUSIONS,
        "terms": SHARED_TERMS,
    },
    # ─────────────────────────────────────────────────────────────
    # 10. Sambalpur & Hirakud
    # ─────────────────────────────────────────────────────────────
    {
        "package_name": "Sambalpur & Hirakud — The Great Dam, Textiles & Hills of the West",
        "slug": "sambalpur-hirakud-dam-tour",
        "package_code": "TP-OD-SML-04",
        "package_type": "FIXED",
        "destination": "Sambalpur – Hirakud – Huma – Ushakothi",
        "city_id": CITY_ROURKELA,
        "duration_days": 4,
        "duration_nights": 3,
        "minimum_persons": 2,
        "maximum_persons": 10,
        "short_description": (
            "Western Odisha in four days — the 25-km Hirakud dam and its "
            "sunrise islands, the leaning temple of Huma, Samaleswari "
            "darshan, the wildlife belt of Ushakothi and the famous "
            "Sambalpuri handloom villages."
        ),
        "description": (
            "Western Odisha moves to its own rhythm — the mighty Mahanadi "
            "checked by the longest earthen dam in the world, hills the "
            "colour of old gold in the evening, and a handloom tradition "
            "that dresses half the state.\n\n"
            "At Hirakud, walk or cycle the dam-top road between the two "
            "cable-stayed towers — Gandhi Minar and Jawahar Minar — with "
            "the reservoir stretching to the horizon. Take the boat across "
            "to the drowned islands where the original villages once "
            "stood. At Huma, marvel at the leaning temple whose tower "
            "tilts yet has stood for centuries, and feed the fish at the "
            "ghat below.\n\n"
            "Darshan at Maa Samaleswari temple anchors the trip "
            "spiritually, while the villages of Barpali and Padigaon open "
            "their looms — Sambalpuri bandha ikat being tied, dyed and "
            "woven in front of you. Add Ushakothi wildlife sanctuary's "
            "sal forest for the nature fix, and the trip completes a face "
            "of Odisha most visitors never meet."
        ),
        "itinerary": [
            {
                "day_number": 1,
                "title": "Arrival via Rourkela / Sambalpur | Samaleswari Darshan",
                "description": (
                    "Pickup from Rourkela or Sambalpur and hotel check-in. "
                    "Afternoon darshan at Maa Samaleswari temple on the "
                    "Mahanadi, followed by the evening aarti and a walk on "
                    "the riverfront."
                ),
                "activities": [
                    "Pickup from Rourkela / Sambalpur",
                    "Hotel check-in at Sambalpur",
                    "Maa Samaleswari Temple darshan",
                    "Evening aarti & riverfront walk",
                    "Overnight stay in Sambalpur",
                ],
                "overnight_city_id": CITY_ROURKELA,
            },
            {
                "day_number": 2,
                "title": "Hirakud Dam Day | Towers, Islands & Sunset",
                "description": (
                    "A full day at Hirakud. Drive the dam-top road between "
                    "the two observation towers, visit the Gandhi Minar on "
                    "its island mound, take the boat ride across the "
                    "reservoir to the submerged-village islands, and stay "
                    "for the famous Hirakud sunset."
                ),
                "activities": [
                    "Breakfast at hotel",
                    "Hirakud dam-top drive",
                    "Gandhi Minar & Jawahar Minar towers",
                    "Reservoir boat ride",
                    "Debrigarh viewpoint stop (optional)",
                    "Hirakud sunset",
                    "Overnight stay in Sambalpur",
                ],
                "overnight_city_id": CITY_ROURKELA,
            },
            {
                "day_number": 3,
                "title": "Huma Leaning Temple & Weaving Villages",
                "description": (
                    "Morning to Huma, where the Shiva temple leans as one "
                    "mystery of the Mahanadi, and the ghat fish rise to "
                    "your hand. Continue into the handloom belt — watch "
                    "Sambalpuri ikat tied and dyed at Barpali or Padigaon, "
                    "and buy directly from weavers. Return by evening."
                ),
                "activities": [
                    "Breakfast at hotel",
                    "Huma leaning Shiva temple visit",
                    "Ghat fish feeding stop",
                    "Barpali / Padigaon weaving village visit",
                    "Ikat tying & dyeing demonstration",
                    "Return to Sambalpur",
                    "Overnight stay in Sambalpur",
                ],
                "overnight_city_id": CITY_ROURKELA,
            },
            {
                "day_number": 4,
                "title": "Ushakothi Sanctuary | Departure",
                "description": (
                    "A morning safari jeep ride through the sal forest of "
                    "Ushakothi (Badrama) wildlife sanctuary — deer, "
                    "elephant country and birdlife. Return for lunch, "
                    "check out and drop at Sambalpur or Rourkela for your "
                    "onward journey."
                ),
                "activities": [
                    "Early drive to Ushakothi (Badrama) Sanctuary",
                    "Forest jeep safari",
                    "Watchtower visit",
                    "Return to Sambalpur",
                    "Hotel check-out",
                    "Drop at Sambalpur / Rourkela",
                ],
                "overnight_city_id": None,
            },
        ],
        "pricing": [
            {"persons_count": 2, "package_price": 17500.00},
            {"persons_count": 4, "package_price": 28000.00},
            {"persons_count": 6, "package_price": 36000.00},
            {"persons_count": 8, "package_price": 43000.00},
            {"persons_count": 10, "package_price": 49000.00},
        ],
        "inclusions": SHARED_INCLUSIONS
        + [
            "3 nights hotel stay in Sambalpur",
            "Hirakud reservoir boat ride",
            "Ushakothi forest jeep safari",
            "Weaving village visit with demonstration",
        ],
        "exclusions": SHARED_EXCLUSIONS,
        "terms": SHARED_TERMS,
    },
]
