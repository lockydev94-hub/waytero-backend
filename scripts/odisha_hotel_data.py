"""
WayTero — Odisha hotel content for the local admin-equivalent seed.

Six properties across the five Odisha cities WayTero already has active
(Bhubaneswar, Puri, Cuttack, Berhampur, Rourkela), shaped exactly like the rows
POST /api/v1/admin/hotels plus its stage tabs would have written: the hotel row,
one or two priced room categories, the rating row, the policy row and amenity
mappings picked from the hotel_amenities master by code.

LOCAL DEMO DATA ONLY. The owner asked for a local database that fills the
homepage bands so the layout can be judged with a realistic item count. Never
run this against production — the properties, prices, ratings and review
counts are invented for layout purposes, and QODER.md forbids publishing
invented claims.

All copy is written in-house for WayTero; nothing is lifted from another
website. Names are invented property names, not real businesses.

Images are intentionally NOT inserted — hotel_images stays empty and the admin
uploads photos from the admin dashboard afterwards. The public card already
falls back to a branded placeholder when primary_image_url is null.

Contact fields (contact_person / contact_number / email) are deliberately NULL:
inventing a phone number or e-mail for a property is not acceptable even for
demo data, and the admin fills them from the profile tab.
"""

# City ids present in the dev database: 1 Berhampur, 2 Rourkela, 3 Cuttack,
# 4 Puri, 5 Bhubaneswar (all Odisha, state_id 1).
CITY_BERHAMPUR = 1
CITY_ROURKELA = 2
CITY_CUTTACK = 3
CITY_PURI = 4
CITY_BHUBANESWAR = 5

HOTELS = [
    {
        "hotel_name": "Trishula Grand Residency",
        "city_id": CITY_BHUBANESWAR,
        "category_code": "PREMIUM",
        "hotel_type": "HOTEL",
        "star_rating": 4,
        "short_description": (
            "Executive rooms a few minutes from Lewis Road, with an all-day "
            "cafe, free parking and a gym on the lower level."
        ),
        "description": (
            "Trishula Grand Residency is a business-oriented property on Lewis "
            "Road, close to the hospital cluster and within a short drive of "
            "the airport on the Bhubaneswar side of the ring road.\n\n"
            "Rooms are air-conditioned and carry a work desk, kettle, wardrobe "
            "and an attached bathroom with hot water round the clock. The "
            "higher floors face the residential colony, so the nights are "
            "quiet, and every room gets housekeeping on request.\n\n"
            "The all-day cafe serves breakfast, South Indian and Odia thalis. "
            "There is covered parking for self-drive guests, a small gym, a "
            "conference room for ten to fifteen people on the mezzanine, and "
            "the front desk keeps luggage and accepts cards.\n\n"
            "Bindu Sagar Lake, the Mukteshwar temple lane and the Bhubaneswar "
            "Railway Station are all under twenty minutes away, which makes "
            "the property a convenient base for people who are in the city "
            "for work and want an evening walk option."
        ),
        "address": "Unit-8, Jaydev Path, Lewis Road",
        "address_line_2": "Near UGM Hospital",
        "landmark": "Opposite UGM Hospital, Lewis Road",
        "postal_code": "751002",
        "latitude": "20.2985000",
        "longitude": "85.8403000",
        "total_rooms": 42,
        "display_order": 1,
        "average_rating": "4.4",
        "total_reviews": 218,
        "amenity_codes": [
            "WIFI",
            "AC",
            "PARKING",
            "RESTAURANT",
            "BREAKFAST",
            "GYM",
            "LIFT",
            "POWER_BACKUP",
            "CCTV",
            "RECEPTION_24H",
        ],
        "rooms": [
            {
                "category_name": "Executive Room",
                "room_type": "EXECUTIVE",
                "description": (
                    "Air-conditioned executive room with a queen bed, work "
                    "desk, kettle, mini fridge and an attached bathroom with "
                    "hot water. City-facing, high floor on request."
                ),
                "base_occupancy": 2,
                "max_adults": 3,
                "max_children": 1,
                "max_occupancy": 4,
                "bed_type": "QUEEN",
                "room_size_sqft": 280,
                "view_type": "CITY_VIEW",
                "base_price": "3200.00",
                "published_price": "4200.00",
                "min_sellable_price": "2800.00",
                "meal_plan": "EP",
                "total_rooms": 26,
            },
            {
                "category_name": "Family Room",
                "room_type": "FAMILY",
                "description": (
                    "Larger family layout with one king bed and one twin bed, "
                    "so two adults and two children fit without an extra bed. "
                    "Includes a sofa and a dining-size work surface."
                ),
                "base_occupancy": 4,
                "max_adults": 4,
                "max_children": 2,
                "max_occupancy": 6,
                "bed_type": "KING",
                "room_size_sqft": 420,
                "view_type": "CITY_VIEW",
                "base_price": "4600.00",
                "published_price": "5900.00",
                "min_sellable_price": "4000.00",
                "meal_plan": "CP",
                "total_rooms": 8,
            },
        ],
    },
    {
        "hotel_name": "Bindusagar Courtyard Hotel",
        "city_id": CITY_BHUBANESWAR,
        "category_code": "STANDARD",
        "hotel_type": "HOTEL",
        "star_rating": 3,
        "short_description": (
            "Compact, clean rooms around the Bindu Sagar side of the old "
            "town, with a rooftop seating area and breakfast included."
        ),
        "description": (
            "Bindusagar Courtyard Hotel sits on the Bindu Sagar side of old "
            "Bhubaneswar, a short walk from the temple lane and a convenient "
            "stop for travellers who reach the city late and want to see the "
            "old town before moving on.\n\n"
            "The building is arranged around a shaded courtyard, and the rooms "
            "off it are compact but complete: air-conditioning, attached "
            "bathroom, hot water, television and a reading light. Rooms on the "
            "top floor open onto a rooftop seating area where breakfast is "
            "served.\n\n"
            "Breakfast is included in the tariff and is a fixed spread of "
            "poha, idli-sambar, paratha and eggs, with tea and coffee. The "
            "front desk stores luggage after check-out and can call an auto or "
            "a cab for the station.\n\n"
            "Parking is on the street with the property's own slot at the "
            "gate, so self-drive guests should confirm space while booking."
        ),
        "address": "12, Bindu Sagar Lane",
        "address_line_2": "Old Town",
        "landmark": "500 m from Bindu Sagar Lake",
        "postal_code": "751001",
        "latitude": "20.2413000",
        "longitude": "85.8339000",
        "total_rooms": 26,
        "display_order": 2,
        "average_rating": "4.1",
        "total_reviews": 96,
        "amenity_codes": [
            "WIFI",
            "AC",
            "TV",
            "BREAKFAST",
            "HOT_WATER",
            "LAUNDRY",
            "RECEPTION_24H",
            "TOILETRIES",
        ],
        "rooms": [
            {
                "category_name": "Standard Double",
                "room_type": "STANDARD",
                "description": (
                    "Compact double-occupancy room with one queen bed, "
                    "air-conditioning, television and an attached bathroom "
                    "with hot water. Breakfast included."
                ),
                "base_occupancy": 2,
                "max_adults": 2,
                "max_children": 1,
                "max_occupancy": 3,
                "bed_type": "QUEEN",
                "room_size_sqft": 190,
                "view_type": "NO_VIEW",
                "base_price": "2400.00",
                "published_price": "3000.00",
                "min_sellable_price": "2100.00",
                "meal_plan": "CP",
                "total_rooms": 18,
            },
            {
                "category_name": "Deluxe Twin",
                "room_type": "DELUXE",
                "description": (
                    "Twin-bed room on the courtyard side with a little more "
                    "floor space, a writing desk and a chair. Suitable for two "
                    "colleagues travelling together."
                ),
                "base_occupancy": 2,
                "max_adults": 3,
                "max_children": 1,
                "max_occupancy": 4,
                "bed_type": "TWIN",
                "room_size_sqft": 240,
                "view_type": "GARDEN_VIEW",
                "base_price": "2900.00",
                "published_price": "3600.00",
                "min_sellable_price": "2500.00",
                "meal_plan": "CP",
                "total_rooms": 8,
            },
        ],
    },
    {
        "hotel_name": "Balabhadri Beach Retreat",
        "city_id": CITY_PURI,
        "category_code": "RESORT",
        "hotel_type": "RESORT",
        "star_rating": 4,
        "short_description": (
            "Low-rise resort on the sea-facing side of the beach road, with a "
            "pool, two restaurants and garden cottages."
        ),
        "description": (
            "Balabhadri Beach Retreat is a low-rise resort on the "
            "Churchibandhi side of the Puri beach road, built so that most "
            "rooms look over the garden toward the sea rather than at the "
            "road.\n\n"
            "The property runs on two floors plus a block of garden cottages. "
            "Sea-facing rooms carry a balcony; the cottages open directly onto "
            "the lawn and suit families travelling with children. Every room "
            "has air-conditioning, a kettle, a wardrobe with a safe and an "
            "attached bathroom.\n\n"
            "A medium-sized pool sits at the centre of the lawn, with "
            "sunbeds and towels laid out in the morning. The main restaurant "
            "serves breakfast buffets and Odia and South Indian dinners, and a "
            "smaller sea-facing counter runs tea, coffee and snacks through "
            "the afternoon.\n\n"
            "The beach is a short gated walk away, and the resort keeps "
            "chairs and an umbrella section on the sand. Puri Railway "
            "Station and the Jagannath Temple are both a fifteen to twenty "
            "minute drive, and the desk arranges transfers on request."
        ),
        "address": "Churchibandhi Beach Road",
        "address_line_2": "Grand Road side",
        "landmark": "Opposite the gated beach walkway",
        "postal_code": "752001",
        "latitude": "19.8032000",
        "longitude": "85.8319000",
        "total_rooms": 54,
        "display_order": 3,
        "average_rating": "4.6",
        "total_reviews": 341,
        "amenity_codes": [
            "WIFI",
            "AC",
            "POOL",
            "RESTAURANT",
            "BREAKFAST",
            "BEACH_ACCESS",
            "GARDEN",
            "PARKING",
            "LIFT",
            "ROOM_SERVICE",
        ],
        "rooms": [
            {
                "category_name": "Garden Cottage",
                "room_type": "COTTAGE",
                "description": (
                    "Single-storey cottage on the lawn with a king bed, a "
                    "verandah seat and a family-friendly layout. Ground floor, "
                    "so no lift is needed with children."
                ),
                "base_occupancy": 2,
                "max_adults": 3,
                "max_children": 2,
                "max_occupancy": 5,
                "bed_type": "KING",
                "room_size_sqft": 360,
                "view_type": "GARDEN_VIEW",
                "base_price": "3800.00",
                "published_price": "4800.00",
                "min_sellable_price": "3400.00",
                "meal_plan": "CP",
                "total_rooms": 12,
            },
            {
                "category_name": "Sea Facing Room",
                "room_type": "DELUXE",
                "description": (
                    "First or second floor room with a private balcony "
                    "facing the sea line, a king bed, a daybed and a bathtub. "
                    "Sunrise side of the property."
                ),
                "base_occupancy": 2,
                "max_adults": 3,
                "max_children": 1,
                "max_occupancy": 4,
                "bed_type": "KING",
                "room_size_sqft": 320,
                "view_type": "SEA_VIEW",
                "base_price": "5200.00",
                "published_price": "6800.00",
                "min_sellable_price": "4600.00",
                "meal_plan": "CP",
                "total_rooms": 20,
            },
        ],
    },
    {
        "hotel_name": "Barabati Riverside Inn",
        "city_id": CITY_CUTTACK,
        "category_code": "PREMIUM",
        "hotel_type": "HOTEL",
        "star_rating": 4,
        "short_description": (
            "Super deluxe rooms on the riverfront side of Cuttack, with a "
            "banquet hall and an in-house restaurant."
        ),
        "description": (
            "Barabati Riverside Inn is a mid-rise property on the riverbank "
            "side of Cuttack, a short drive over the bridge from "
            "Bhubaneswar, built mainly around family travel and small "
            "functions.\n\n"
            "Super deluxe rooms are the bulk of the inventory: air-condition, "
            "a king or twin bed configuration, a seating corner, kettle and an "
            "attached bathroom with a rain shower. Rooms above the third floor "
            "look across the Mahanadi distributary, which is the reason most "
            "repeat guests ask for the higher floors.\n\n"
            "The in-house restaurant serves breakfast, thalis and Chinese, and "
            "the banquet hall on the ground floor takes weddings and corporate "
            "gatherings, so the front desk holds additional luggage space on "
            "function days.\n\n"
            "Covered parking, a lift, 24-hour reception and power backup are "
            "standard. Balata Tirtha and the Barabati Stadium are within a "
            "ten-minute drive, and Cuttack Railway Station is a little under "
            "that."
        ),
        "address": "Sector-14, Badambak",
        "address_line_2": "Cuttack",
        "landmark": "Near Barabati Stadium ring road",
        "postal_code": "753012",
        "latitude": "20.4718000",
        "longitude": "85.8607000",
        "total_rooms": 38,
        "display_order": 4,
        "average_rating": "4.3",
        "total_reviews": 152,
        "amenity_codes": [
            "WIFI",
            "AC",
            "TV",
            "PARKING",
            "RESTAURANT",
            "CONFERENCE_HALL",
            "LIFT",
            "POWER_BACKUP",
            "LAUNDRY",
            "SAFE",
        ],
        "rooms": [
            {
                "category_name": "Super Deluxe King",
                "room_type": "SUPER_DELUXE",
                "description": (
                    "Super deluxe room with a king bed, a seating corner, "
                    "kettle and a rain-shower bathroom. Available on the "
                    "river-facing side on request, subject to availability."
                ),
                "base_occupancy": 2,
                "max_adults": 3,
                "max_children": 1,
                "max_occupancy": 4,
                "bed_type": "KING",
                "room_size_sqft": 300,
                "view_type": "LAKE_VIEW",
                "base_price": "3400.00",
                "published_price": "4400.00",
                "min_sellable_price": "2950.00",
                "meal_plan": "EP",
                "total_rooms": 24,
            },
            {
                "category_name": "Suite",
                "room_type": "SUITE",
                "description": (
                    "Two-room suite with a living area that seats four, a "
                    "king bedroom, a second bathroom and a dining table. "
                    "Booked most often by families attending functions."
                ),
                "base_occupancy": 4,
                "max_adults": 5,
                "max_children": 2,
                "max_occupancy": 6,
                "bed_type": "KING",
                "room_size_sqft": 620,
                "view_type": "CITY_VIEW",
                "base_price": "6200.00",
                "published_price": "7800.00",
                "min_sellable_price": "5400.00",
                "meal_plan": "MAP",
                "total_rooms": 4,
            },
        ],
    },
    {
        "hotel_name": "Bahuda Beach Road Hotel",
        "city_id": CITY_BERHAMPUR,
        "category_code": "BUDGET",
        "hotel_type": "HOTEL",
        "star_rating": 3,
        "short_description": (
            "No-frills, value rooms on the beach road near the market end of "
            "Berhampur, with an in-house vegetarian restaurant."
        ),
        "description": (
            "Bahuda Beach Road Hotel is a value property at the market end of "
            "Berhampur, aimed at travellers who want a clean, affordable "
            "room close to the town rather than a resort stay.\n\n"
            "Rooms are simple: air-conditioning or a ceiling fan depending on "
            "the category, an attached bathroom with hot water, a television "
            "and a wardrobe. The front of the building faces the beach road, "
            "and the rear rooms look over the internal lane and are quieter at "
            "night.\n\n"
            "The ground floor runs a vegetarian restaurant open through "
            "dinner, which is where most guests eat rather than travel into "
            "town. Breakfast is served on request and is billed per plate.\n\n"
            "Two-wheeler parking is inside the gate and car parking is on the "
            "road. Bahuda Beach is a short auto ride away, and the Gopalpur "
            "coast road starts a few minutes south of the property, which "
            "makes this a convenient overnight stop for people driving down "
            "the coast."
        ),
        "address": "MG Market Road, near Dagabatta",
        "address_line_2": "Berhampur",
        "landmark": "Opposite the market bus stand",
        "postal_code": "760001",
        "latitude": "19.3162000",
        "longitude": "84.7955000",
        "total_rooms": 24,
        "display_order": 5,
        "average_rating": "4.0",
        "total_reviews": 63,
        "amenity_codes": [
            "WIFI",
            "AC",
            "TV",
            "RESTAURANT",
            "HOT_WATER",
            "POWER_BACKUP",
            "RECEPTION_24H",
        ],
        "rooms": [
            {
                "category_name": "Standard AC Room",
                "room_type": "STANDARD",
                "description": (
                    "Double-occupancy room with split air-conditioning, one "
                    "double bed, television and an attached bathroom with "
                    "hot water."
                ),
                "base_occupancy": 2,
                "max_adults": 2,
                "max_children": 1,
                "max_occupancy": 3,
                "bed_type": "DOUBLE",
                "room_size_sqft": 170,
                "view_type": "NO_VIEW",
                "base_price": "1600.00",
                "published_price": "2100.00",
                "min_sellable_price": "1400.00",
                "meal_plan": "EP",
                "total_rooms": 14,
            },
            {
                "category_name": "Family Room",
                "room_type": "FAMILY",
                "description": (
                    "Three-bed family room with air-conditioning and a larger "
                    "bathroom. Booked by families on the coast road who need "
                    "one room instead of two."
                ),
                "base_occupancy": 4,
                "max_adults": 4,
                "max_children": 2,
                "max_occupancy": 6,
                "bed_type": "TWIN",
                "room_size_sqft": 300,
                "view_type": "CITY_VIEW",
                "base_price": "2600.00",
                "published_price": "3300.00",
                "min_sellable_price": "2200.00",
                "meal_plan": "EP",
                "total_rooms": 6,
            },
        ],
    },
    {
        "hotel_name": "Hirabhadri Business Hotel",
        "city_id": CITY_ROURKELA,
        "category_code": "STANDARD",
        "hotel_type": "HOTEL",
        "star_rating": 3,
        "short_description": (
            "Business-focused rooms in the township end of Rourkela, with a "
            "meeting room, laundry and early-morning breakfast."
        ),
        "description": (
            "Hirabhadri Business Hotel is a purpose-built property for "
            "work travel in Rourkela, on the township side, close enough to "
            "the plant belt and the sector market that most guests walk to "
            "breakfast.\n\n"
            "Rooms are laid out the same way throughout: air-conditioning, a "
            "firm desk with a chair and a lamp, a television, kettle and an "
            "attached bathroom. Twin-bed configurations are the default, and "
            "the desk keeps a few king rooms for solo travellers who ask.\n\n"
            "Breakfast starts early for guests on shift, and the restaurant "
            "serves thalis through the evening. A ground-floor meeting room "
            "seats about ten, with a whiteboard and a projector on request, "
            "and laundry turnaround is same-day when clothes are in before "
            "ten in the morning.\n\n"
            "Rourkela Railway Station is a short drive, and the Hirabhad and "
            "Samakia-Khusuma falls are the usual weekend excursions, which the "
            "desk can arrange a cab for."
        ),
        "address": "Sector-16, Steel Township",
        "address_line_2": "Rourkela",
        "landmark": "Near Town Club",
        "postal_code": "769001",
        "latitude": "22.2521000",
        "longitude": "84.8705000",
        "total_rooms": 30,
        "display_order": 6,
        "average_rating": "4.2",
        "total_reviews": 118,
        "amenity_codes": [
            "WIFI",
            "AC",
            "TV",
            "PARKING",
            "RESTAURANT",
            "BREAKFAST",
            "LAUNDRY",
            "POWER_BACKUP",
            "CONFERENCE_HALL",
            "CCTV",
        ],
        "rooms": [
            {
                "category_name": "Business Twin",
                "room_type": "EXECUTIVE",
                "description": (
                    "Twin-bed room with split air-conditioning, a work desk "
                    "with a lamp and chair, kettle and an attached bathroom. "
                    "The default configuration for two colleagues."
                ),
                "base_occupancy": 2,
                "max_adults": 2,
                "max_children": 1,
                "max_occupancy": 3,
                "bed_type": "TWIN",
                "room_size_sqft": 220,
                "view_type": "CITY_VIEW",
                "base_price": "2200.00",
                "published_price": "2900.00",
                "min_sellable_price": "1900.00",
                "meal_plan": "CP",
                "total_rooms": 20,
            },
            {
                "category_name": "Single Occupancy",
                "room_type": "STANDARD",
                "description": (
                    "Smaller single-bed room sold on single occupancy, with "
                    "air-conditioning, desk and attached bathroom. Popular "
                    "for longer work stays."
                ),
                "base_occupancy": 1,
                "max_adults": 1,
                "max_children": 0,
                "max_occupancy": 1,
                "bed_type": "SINGLE",
                "room_size_sqft": 150,
                "view_type": "NO_VIEW",
                "base_price": "1800.00",
                "published_price": "2400.00",
                "min_sellable_price": "1600.00",
                "meal_plan": "CP",
                "total_rooms": 8,
            },
        ],
    },
]
