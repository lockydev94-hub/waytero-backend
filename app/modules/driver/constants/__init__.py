# ============================================================
# WAYTERO — DRIVER APP CONSTANTS
# File: app/modules/driver/constants/__init__.py
# Doc Ref: 04_API_Documentation/13_DRIVER_APP_API.md
#          06_Backend_Architecture/11_DRIVER_APP_BACKEND.md §5.2
# ============================================================

# A paused trip is auto-resumed after this many seconds so a driver who
# forgets to tap Resume does not leave the partner's live map and the
# passenger waiting indefinitely. The app shows the same deadline from
# `trip_auto_resume_at` in the booking payload.
DRIVER_PAUSE_AUTO_RESUME_SECONDS = 300

# Event written to the notification outbox when an accepted/offered job's
# scheduled pickup time has passed and the trip has not started.
DRIVER_BOOKING_OVERDUE_EVENT = "DRIVER_BOOKING_OVERDUE"
