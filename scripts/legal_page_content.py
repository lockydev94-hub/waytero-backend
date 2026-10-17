# ============================================================
# WAYTERO — WEBSITE PAGE BODIES (seeded copy)
# File: scripts/legal_page_content.py
#
# Single source of truth for the page bodies that live in
# system_configurations under LEGAL_PAGE_{SLUG} and are served by
# GET /public/legal/{slug}. scripts.seed_legal_content inserts these rows
# (ON CONFLICT DO NOTHING); scripts.rewrite_legal_copy upgrades the five
# original rows to this text.
#
# Everything in here has to be true of the platform as it is built today:
# no invented SLAs, no claims about tooling that does not exist. The
# customer-web page files carry the same text as their built-in fallback so
# the page reads identically when the API is down — change one, change the
# other.
#
# Bodies that quote a number (refund tiers, desk hours) must say "listed on
# this page" instead of repeating it: those numbers come from
# GET /public/cancellation-policy at render time, and static prose that
# restates them goes stale the moment an admin edits a ladder.
# ============================================================

# What the pages print as "Last updated" for this generation of copy. An edit
# saved through Website → Pages overwrites it with whatever the admin types.
UPDATED_AT = "2026-10-17"

PAGE_BODIES = {
    "LEGAL_PAGE_PRIVACY": {
        "slug": "privacy",
        "title": "Privacy Policy",
        "eyebrow": "Legal",
        "description": "What WayTero holds about you, who sees it, and how to have it removed.",
        "effective_date": "2026-01-01",
        "updated_at": UPDATED_AT,
        "sections": [
            {
                "heading": "1. What we collect",
                "body": (
                    'WayTero Travel Technologies Pvt Ltd ("WayTero", "we", "us") holds what a '
                    "booking needs and nothing more: your name, email and mobile number; the trip "
                    "you booked (pickup, drop, dates, passengers, guest names); the money record "
                    "(amount, payment mode, receipt or invoice number, status); the sign-in "
                    "sessions on your account; and the audit trail of every money movement. Online "
                    "payments run on the payment gateway — we keep the reference and the status, "
                    "never the card number. When an advance is collected by the driver or the "
                    "property in cash or UPI, we record that it was collected, by whom, and how much."
                ),
            },
            {
                "heading": "2. How we use it",
                "body": (
                    "To run the booking end to end: find you an available driver, room or package, "
                    "record the payment, tell you when the status changes (assigned, started, "
                    "completed, cancelled), compute refunds and answer support. We do not sell "
                    "your personal data."
                ),
            },
            {
                "heading": "3. Who sees your booking",
                "body": (
                    "The partner delivering it, and only the part of it they need. A driver gets "
                    "your name, mobile, pickup, drop and time. A hotel gets the guest names and the "
                    "stay dates. A tour operator gets the travellers on that package and the number "
                    "we reach you on. Nothing else on your account travels with the booking."
                ),
            },
            {
                "heading": "4. Payments, refunds and your wallet",
                "body": (
                    "The cancellation engine computes your refund from the policy in force at the "
                    "moment you cancel — the cab time ladder, the hotel's own ladder, or the tour "
                    "day ladder — and the amount is credited to your WayTero wallet as part of "
                    "processing the cancellation. A refund can never be more than you paid, and "
                    "nothing is charged on top of it. The tiers are listed on the Refund Policy page."
                ),
            },
            {
                "heading": "5. Deleting your account",
                "body": (
                    "Raise a request from the Account Deletion page — signed in, or through the "
                    "public form if you have lost access. A team member reviews it. When it is "
                    "approved your sign-in sessions end immediately and your name, email and mobile "
                    "are anonymised. The booking, wallet and ledger rows stay in the system as "
                    "financial records but no longer join to a live account; accounting and tax law "
                    "is why they stay, not marketing."
                ),
            },
            {
                "heading": "6. Your choices",
                "body": (
                    "Correct your own details in Profile. Download your invoices and receipts from "
                    "My Bookings. Ask us to delete the account, as above. There is no self-service "
                    "export of everything we hold — write to support@waytero.com and the team will "
                    "pull what exists while the desk is open."
                ),
            },
            {
                "heading": "7. How the account is protected",
                "body": (
                    "The site is served over HTTPS. Signing in is a one-time password on your "
                    "mobile number, or email / Google sign-in through Firebase; sessions use "
                    "short-lived access tokens with a refresh token the platform revokes — and does, "
                    "when you sign out or a deletion request is approved. Partners and staff sign in "
                    "through separate, role-scoped portals, and staff actions on money are recorded "
                    "against the user who took them. See the Security page for the same list."
                ),
            },
        ],
    },
    "LEGAL_PAGE_TERMS": {
        "slug": "terms",
        "title": "Terms of Service",
        "eyebrow": "Legal",
        "description": "The agreement between you and WayTero when you book on the platform.",
        "effective_date": "2026-01-01",
        "updated_at": UPDATED_AT,
        "sections": [
            {
                "heading": "1. What WayTero is",
                "body": (
                    "WayTero is a travel marketplace: it connects you with independent cab drivers, "
                    "hotels and tour operators, and it handles the booking, the money and the "
                    "support around it. The service itself — the drive, the room, the tour — is "
                    "delivered by the partner you booked, and delivering it is their responsibility. "
                    "WayTero acts as the intermediary and settles disputes in good faith."
                ),
            },
            {
                "heading": "2. Bookings, payment and confirmation",
                "body": (
                    "A booking is not confirmed the moment you place it. Cab bookings wait for a "
                    "driver to be assigned and to accept the trip; hotels wait for the property to "
                    "confirm the stay; tour packages wait for the operator. Where a partner asks for "
                    "an advance, the booking moves once that payment is recorded. Prices are "
                    "calculated on our servers from the rate or policy that applies to the service, "
                    "not in your browser."
                ),
            },
            {
                "heading": "3. Before you book",
                "body": (
                    "Check the pickup and drop, the dates and the passenger or guest count; read the "
                    "cancellation policy and the advance the service asks for; keep the mobile "
                    "number you booked with reachable, because that is the number the partner and "
                    "the driver use. The Booking Instructions page is the full guide."
                ),
            },
            {
                "heading": "4. Cancelling",
                "body": (
                    "You cancel from My Bookings and the preview shows the exact refund before you "
                    "commit. What comes back is decided by the tier in force at the moment you "
                    "cancel — the cab ladder, the hotel's own policy, or the tour ladder — and it is "
                    "credited to your WayTero wallet when the cancellation is processed. Once a "
                    "driver has been assigned, or a stay or package is close enough, the charge "
                    "rises. The numbers are on the Refund Policy page and they are the numbers the "
                    "engine uses."
                ),
            },
            {
                "heading": "5. Your conduct",
                "body": (
                    "Give accurate booking information, treat the partner and the driver "
                    "respectfully, and do not use the platform to place bookings you do not intend "
                    "to take up. WayTero can suspend an account that violates these terms."
                ),
            },
            {
                "heading": "6. Deleting your account",
                "body": (
                    "You can delete your account through the Account Deletion page. Deletion is "
                    "irreversible: sign-in stops working, your identity fields are anonymised, and "
                    "the account can no longer book or spend. The booking, wallet and ledger rows "
                    "stay in the records as financial history, detached from the account."
                ),
            },
            {
                "heading": "7. Governing law",
                "body": (
                    "These terms are governed by the laws of India. Disputes are subject to the "
                    "jurisdiction of courts in Bhubaneswar, Odisha."
                ),
            },
        ],
    },
    "LEGAL_PAGE_REFUND": {
        "slug": "refund",
        "title": "Refund & Cancellation Policy",
        "eyebrow": "Policy",
        "description": "How a WayTero refund is calculated, when it is credited, and what it can be used for.",
        "effective_date": "2026-01-01",
        "updated_at": UPDATED_AT,
        "sections": [
            {
                "heading": "How refunds work",
                "body": (
                    "WayTero uses a tiered, time-based refund engine. The closer you cancel to "
                    "pickup, check-in or the travel start date, the higher the cancellation charge. "
                    "The amount is computed when you cancel and the preview on the booking shows it "
                    "before you commit. It is credited to your WayTero wallet as part of processing "
                    "the cancellation. A refund can never be more than you paid, and nothing is "
                    "charged on top."
                ),
            },
            {
                "heading": "Cab bookings",
                "body": (
                    "Cabs follow one platform-wide ladder measured against pickup time, with a "
                    "separate charge once a driver has been assigned to the trip. The tiers in force "
                    "are listed on this page — they are read from the same configuration the "
                    "cancellation engine uses, so what is written here is what is applied."
                ),
            },
            {
                "heading": "Hotel bookings",
                "body": (
                    "Each hotel sets its own ladder; there is no platform-wide hotel rule. The "
                    "policy that applies to your stay is shown on the hotel page before you book and "
                    "again in the refund preview before you cancel, so the numbers you see are the "
                    "numbers that are used."
                ),
            },
            {
                "heading": "Tour packages",
                "body": (
                    "Tours follow a day-based ladder measured against the travel start date. Some "
                    "packages carry their own policy instead — the package page says which one "
                    "applies. The platform tiers in force are listed on this page."
                ),
            },
            {
                "heading": "Where the money goes",
                "body": (
                    "A refund lands in your WayTero wallet and can be spent on any future booking. "
                    "It is not a bank transfer — the platform does not offer a withdrawal to a bank "
                    "account today, so the wallet is the form a refund takes. A refund is not "
                    "clawed back later for the same cancellation."
                ),
            },
        ],
    },
    "LEGAL_PAGE_COOKIES": {
        "slug": "cookies",
        "title": "Cookies & Browser Storage",
        "eyebrow": "Legal",
        "description": "The cookies and browser storage waytero.com keeps, and what each one is for.",
        "effective_date": "2026-01-01",
        "updated_at": UPDATED_AT,
        "sections": [
            {
                "heading": "1. What this site stores in your browser",
                "body": (
                    "This site does not set advertising or tracking cookies and runs no third-party "
                    "analytics. There is no consent banner because there is nothing optional to "
                    "consent to. What it does keep is a small set of entries in your browser's own "
                    "storage: your sign-in state, a few choices about your visit, and where the chat "
                    "widget left off."
                ),
            },
            {
                "heading": "2. Keeping you signed in",
                "body": (
                    "wt_customer_access and wt_customer_refresh hold the access token and the "
                    "refresh token for your WayTero account. The access token is short-lived; the "
                    "refresh token is what lets the site sign you back in without a new OTP. They "
                    "are sent only to WayTero's own API over HTTPS. Signing out, or having a "
                    "deletion request approved, removes them."
                ),
            },
            {
                "heading": "3. Choices about your visit",
                "body": (
                    "wt_intent_place remembers the city or place you last searched for, and "
                    "wt_intent_dismissed remembers that you closed the search prompt, so the site "
                    "does not ask twice. wt_geo_asked records that you have already been offered "
                    "location-based suggestions. Each one is about the current visit and can be "
                    "cleared without losing anything else."
                ),
            },
            {
                "heading": "4. The chat widget",
                "body": (
                    "If you open the support chat, wt_chat_guest_key, wt_chat_guest_name and "
                    "wt_chat_conversation let the conversation continue when you come back. The "
                    "guest name is only what you typed into that box."
                ),
            },
            {
                "heading": "5. Turning it off",
                "body": (
                    "You can clear or block this storage in your browser settings at any time. "
                    "Blocking the sign-in keys means you are signed out and will need a fresh OTP "
                    "to book; the rest of the site keeps working. Clearing site data removes all of "
                    "it and the site starts from a clean slate."
                ),
            },
        ],
    },
    "LEGAL_PAGE_BOOKING_INSTRUCTIONS": {
        "slug": "booking-instructions",
        "title": "Booking Instructions",
        "eyebrow": "Guide",
        "description": "How to book a cab, a hotel or a tour on WayTero, and what happens after you do.",
        "effective_date": "2026-01-01",
        "updated_at": UPDATED_AT,
        "sections": [
            {
                "heading": "1. Book a cab",
                "body": (
                    "Enter the pickup and drop, choose the trip type (one-way, round trip, "
                    "outstation) and the vehicle category. The fare is calculated on our servers and "
                    "shown before you confirm. Pay the advance the booking asks for — online, or to "
                    "the driver where that is the arrangement. The trip then waits for a driver to "
                    "accept it. The Track page shows each step, and once a driver is assigned it "
                    "shows the name, the vehicle number and a masked mobile number."
                ),
            },
            {
                "heading": "2. Book a hotel",
                "body": (
                    "Search by city and dates, filter by price and amenities, and read that "
                    "property's cancellation policy before you book — it is the one that will be "
                    "applied. Confirm the room category and the guest count. Where the property "
                    "asks for an advance you pay it and settle the balance with the property. Some "
                    "properties confirm the stay straight away and others take a while; the booking "
                    "shows which of the two you are waiting for."
                ),
            },
            {
                "heading": "3. Book a tour package",
                "body": (
                    "Browse packages by destination and duration, check what the package includes "
                    "(transport, stay, meals, activities) and which cancellation policy it carries, "
                    "then pay the advance to confirm. The operator confirms the booking and the "
                    "itinerary becomes downloadable from the booking."
                ),
            },
            {
                "heading": "4. Payments and advances",
                "body": (
                    "Online payments run through the payment gateway; WayTero keeps the amount, the "
                    "mode and the receipt reference, not your card details. Every advance has a "
                    "receipt number on the booking and the balance due is shown there too. A driver "
                    "or property can collect a payment in cash or UPI and record it against the "
                    "booking — ask for the receipt entry before you hand anything over."
                ),
            },
            {
                "heading": "5. Cancelling a booking",
                "body": (
                    "Cancel from My Bookings. The preview shows the exact refund — computed by the "
                    "engine from the tier in force at that moment — before you commit. Once you "
                    "confirm, the refund is credited to your wallet as the cancellation is "
                    "processed. The tiers are on the Refund Policy page; a hotel's own policy is on "
                    "its page."
                ),
            },
            {
                "heading": "6. Invoices and support",
                "body": (
                    "The invoice for a cab, hotel or tour booking downloads from the booking in My "
                    "Bookings, along with the receipts and a tour itinerary. For anything else, the "
                    "Support page lists the routes to the team — keep the booking number handy and "
                    "whoever picks it up can see the trip."
                ),
            },
        ],
    },
    # ── Trust and company pages ───────────────────────────────────────────────
    # These four carry no `effective_date`: nothing here took effect on a date,
    # they describe how the platform works today, and `updated_at` is the only
    # honest stamp the hero can print.
    "LEGAL_PAGE_SAFETY": {
        "slug": "safety",
        "title": "Safety",
        "eyebrow": "Trust & Safety",
        "description": (
            "What is checked before a driver or property goes live, what happens to "
            "your money and your data on the way, and what to do if a trip goes wrong."
        ),
        "updated_at": UPDATED_AT,
        "sections": [
            {
                "heading": "1. Before a partner goes live",
                "body": (
                    "A cab partner submits the vehicle registration, the insurance and the "
                    "driver's licence; a hotel or tour operator submits its business and property "
                    "documents. Someone on the WayTero team reads each set before the listing goes "
                    "live, and a partner whose complaints keep repeating is taken off the "
                    "platform. There is no automatic approval and no automatic suspension — both "
                    "are decisions people make, which is also why neither happens the second a "
                    "document lands."
                ),
            },
            {
                "heading": "2. What the site can tell you mid-trip",
                "body": (
                    "The Track page answers a booking number with the booking's status and, once a "
                    "driver is assigned, the driver's name, the vehicle number and a masked mobile "
                    "number. It is a status check, not a map: no vehicle position, no playback of "
                    "the route, no arrival clock. Anyone you give the booking number to sees "
                    "exactly what you see, which is the honest version of sharing a trip."
                ),
            },
            {
                "heading": "3. Money",
                "body": (
                    "Online payment runs on the payment gateway. WayTero keeps the amount, the "
                    "mode, the reference and the status — never the card number. Where a driver or "
                    "property collects an advance in cash or UPI, the booking records that it was "
                    "collected, by whom, and how much, and the receipt entry is on the booking. If "
                    "someone asks for money that is not on the booking, that is the moment to call."
                ),
            },
            {
                "heading": "4. If something goes wrong",
                "body": (
                    "Anything that puts you in danger: call 112, the national emergency number, "
                    "first. The police do not wait for us, and we cannot send them. For the "
                    "booking itself, the number printed on your confirmation is the fastest route "
                    "while the desk is staffed; the hours are listed on this page. Outside them, "
                    "WhatsApp or email reaches the team and the message is answered the next "
                    "working day — a reassignment, a move or a cancellation is still possible "
                    "while there is something left to change."
                ),
            },
            {
                "heading": "5. Ratings",
                "body": (
                    "Every completed booking can be rated and the rating stays on the listing, so "
                    "the next traveler sees what you saw. A low score does not remove a partner by "
                    "itself — repeat complaints and failed checks do. That is why the reviews you "
                    "leave are read, not just stored."
                ),
            },
        ],
        "faq": [
            {
                "question": "How are drivers and properties verified?",
                "answer": "Registration, insurance and licence documents for cabs; business and property documents for hotels and tour operators. A person on the team checks them before the listing goes live, and they are looked at again when a partner's record changes.",
            },
            {
                "question": "Can my family watch the vehicle on a map?",
                "answer": "No. Share the booking number and they see what you see on Track: the status and, once assigned, the driver's name, vehicle number and a masked mobile. There is no live position and no route playback on this platform.",
            },
            {
                "question": "Is my card stored by WayTero?",
                "answer": "No. Online payment runs through the gateway and we keep the transaction reference, the amount and the status. A cash or UPI advance taken by a driver or property is recorded on the booking, not stored as a payment instrument.",
            },
            {
                "question": "What happens if I need help outside staffed hours?",
                "answer": "For danger, call 112 first. For the booking, leave the message on WhatsApp or email and the desk picks it up the next working day; the hours printed above are the hours we can promise an answer inside.",
            },
            {
                "question": "Does a bad review take a partner off the site?",
                "answer": "Not on its own. One rating is one person's trip; the team acts on patterns — repeat complaints, failed document checks, bookings that did not happen.",
            },
        ],
    },
    "LEGAL_PAGE_SUPPORT": {
        "slug": "support",
        "title": "Support",
        "eyebrow": "Help",
        "description": (
            "How to reach the desk fastest, what to have in front of you, and which "
            "question belongs on which channel."
        ),
        "updated_at": UPDATED_AT,
        "sections": [
            {
                "heading": "1. Have the booking number",
                "body": (
                    "It is the key to the whole record: the status, who is assigned, what was "
                    "paid, what is refundable and what the property or driver was told. The same "
                    "question without it takes three times as long, because the first thing the "
                    "desk does is find the booking. It is on your confirmation and in My Bookings."
                ),
            },
            {
                "heading": "2. Which channel for which question",
                "body": (
                    "The phone number on your confirmation is fastest while the desk is staffed — "
                    "those hours are printed on this page. WhatsApp suits trip changes and photos "
                    "of a room or a vehicle. Email suits anything that needs a paper trail: "
                    "invoices, name changes, complaints. The chat bubble on this site reaches an "
                    "agent who is online, and outside staffed hours it takes a message rather than "
                    "a conversation."
                ),
            },
            {
                "heading": "3. Cancellations and refunds",
                "body": (
                    "You cancel from My Bookings and the preview shows the exact refund before you "
                    "commit; the tier that applies is the one in force at that moment. The amount "
                    "is credited to your WayTero wallet as the cancellation is processed and can be "
                    "spent on any future booking. The Refund Policy page lists the live ladders, "
                    "and reads them from the same configuration the engine uses."
                ),
            },
            {
                "heading": "4. When a trip goes wrong",
                "body": (
                    "Tell us before it is over. A driver who never arrives, a room nothing like its "
                    "photos, an itinerary that changed without notice — while the pickup window or "
                    "the stay is still open there is something the desk can reassign, move or "
                    "cancel. After the fact there is usually only a refund to argue about, which "
                    "is worse for you."
                ),
            },
            {
                "heading": "5. What we answer when",
                "body": (
                    "A message that arrives while the desk is staffed is answered the same working "
                    "day. Anything else is answered the next working day. There is no minute-count "
                    "promise here — the platform is small, and a response-time guarantee nobody "
                    "measures is a claim, not a service."
                ),
            },
        ],
        "faq": [
            {
                "question": "How do I cancel a hotel booking?",
                "answer": "Open the booking in My Bookings and choose Cancel. The refund the property's own policy allows at that moment is shown in the preview before you confirm it.",
            },
            {
                "question": "Where does my refund go?",
                "answer": "Into your WayTero wallet, as the cancellation is processed. The wallet is the form a refund takes today — there is no bank withdrawal on the platform — and it can be spent on any future booking.",
            },
            {
                "question": "How do I get a GST invoice?",
                "answer": "Eligible bookings generate a tax invoice, downloadable from the booking in My Bookings. If it is not there, email the booking number and the billing details and the desk issues it.",
            },
            {
                "question": "My driver has not arrived. What now?",
                "answer": "Call the number on your confirmation, and the driver's number on Track if one has been assigned. A reassignment is possible while the pickup window is open, which is why it is worth calling rather than waiting.",
            },
            {
                "question": "Can you change my check-in date?",
                "answer": "Yes, subject to the property's availability and any difference in rate. Ask before the property's change window closes — after check-in date passes there is usually nothing left to change.",
            },
        ],
    },
    "LEGAL_PAGE_CAREERS": {
        "slug": "careers",
        "title": "Careers",
        "eyebrow": "Work with us",
        "description": (
            "How the team works, what it is like to build here, and how to introduce "
            "yourself when nothing is posted."
        ),
        "updated_at": UPDATED_AT,
        "sections": [
            {
                "heading": "1. How we work",
                "body": (
                    "A small team in Bhubaneswar, owning its own work end to end. The book of "
                    "rules is short: the traveler decides what matters, a feature ships polished or "
                    "does not ship, and nobody hides behind a hand-off. Booking, fares, payments "
                    "and partner onboarding all live on one stack, so an engineer sees the customer "
                    "complaint that follows their own code — which is the fastest way to stop "
                    "writing things that break."
                ),
            },
            {
                "heading": "2. What a role here actually is",
                "body": (
                    "Generalist work on a young product: you will touch a fare rule and a "
                    "settlement screen in the same week, and you will talk to a driver, a hotel "
                    "front desk or a customer at some point, because that is where the bugs are. "
                    "If you want a narrow role with a team of twenty around it, this is not it."
                ),
            },
            {
                "heading": "3. When you send a profile",
                "body": (
                    "Say what you have built, not what you have read, and name the part of the "
                    "platform you would want to own. Every introduction that arrives is read; we "
                    "reply when there is something to reply to — a role, a paid trial, or a clear "
                    "reason why neither fits yet. We do not collect CVs to look busy."
                ),
            },
        ],
        "faq": [],
    },
    "LEGAL_PAGE_PRESS": {
        "slug": "press",
        "title": "Press & Media",
        "eyebrow": "For journalists",
        "description": (
            "What WayTero will give a journalist, what it will not, and how to ask."
        ),
        "updated_at": UPDATED_AT,
        "sections": [
            {
                "heading": "1. Figures you can quote",
                "body": (
                    "The numbers on this page are read from the same database the site sells from: "
                    "cities with live cab fares, routes with a set fare, properties bookable today, "
                    "tour packages live. Quote them with the date you looked, because they move "
                    "when the catalogue moves. Anything the platform cannot count — total "
                    "bookings, revenue, retention — we will not put in a sentence for you."
                ),
            },
            {
                "heading": "2. Assets",
                "body": (
                    "The logo, product screenshots and the background of the founding team are sent "
                    "on request, with a date on them. There is no downloadable media kit on this "
                    "page: a zip that goes stale gets printed with the wrong wordmark, and an email "
                    "reply takes ten minutes."
                ),
            },
            {
                "heading": "3. What we will not share",
                "body": (
                    "Customer names, driver names, what any single partner earns, and anything "
                    "about money raised that has not been announced. Aggregate numbers with a date "
                    "on them, an explanation of how the fare engine works, and a person to speak to "
                    "are all available."
                ),
            },
            {
                "heading": "4. How to ask",
                "body": (
                    "Write to the address on this page with the outlet, the deadline and the "
                    "specific question. A deadline that is real gets an answer; a general request "
                    "for \"anything interesting\" gets the honest reply that there is nothing to "
                    "sell you this week."
                ),
            },
        ],
        "faq": [],
    },
}
