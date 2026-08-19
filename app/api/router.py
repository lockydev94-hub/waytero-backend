# ============================================================
# WAY TERO — MAIN API ROUTER
# File: app/api/router.py
# Doc Ref: Backend Architecture Part 2, Section 4 & 5
# All v1 routers are registered here.
# ============================================================

from fastapi import APIRouter, Depends
from app.core.dependencies import require_roles
from app.modules.auth.api import router as auth_router
from app.modules.customer.api import router as customer_router
from app.modules.partner.api import router as partner_router
from app.modules.driver.api import router as driver_router
from app.modules.vehicle.api import router as vehicle_router
from app.modules.admin.api import router as admin_settings_router
from app.modules.admin.api_dashboard import router as admin_dashboard_router
from app.modules.admin.coupon_api import router as coupon_router
from app.modules.admin.booking_api import router as admin_booking_router
from app.modules.admin.customer_care_api import router as customer_care_router
from app.modules.admin.trip_assistance_api import router as trip_assist_router
from app.modules.admin.settlement_api import router as settlement_router
from app.modules.admin.advance_api import router as advance_router
from app.modules.admin.wallet_api import router as wallet_router
from app.modules.admin.reports_api import router as reports_router
from app.modules.partner.cash_collection_api import router as partner_cash_router
from app.modules.admin.blog_api import router as admin_blog_router
from app.modules.admin.public_blog_api import router as public_blog_router
from app.modules.driver.breakdown_api import router as driver_breakdown_router
from app.modules.admin.cab_ops_api import router as admin_cab_ops_router
from app.modules.admin.cab_breakdown_api import router as admin_breakdown_router
from app.modules.admin.vehicle_admin_api import router as admin_vehicle_admin_router
from app.modules.partner.booking_api import router as partner_booking_router
from app.modules.partner.wallet_api import router as partner_wallet_router
from app.modules.partner.gst_api import router as partner_gst_router
from app.modules.partner.hotel_booking_api import router as partner_hotel_booking_router
from app.modules.admin.payment_api import router as admin_payment_router
from app.modules.hotel.admin_api import router as admin_hotel_router
from app.modules.notification.api import router as notification_router
from app.modules.admin.cancellation_api import router as admin_cancellation_router
from app.modules.admin.notices_api import router as admin_notices_router
from app.modules.partner.notices_api import router as partner_notices_router
from app.modules.partner.cancellation_request_api import (
    router as partner_cancellation_router,
)
from app.modules.customer.cancellation_api import router as customer_cancellation_router
from app.modules.admin.public_homepage_api import router as public_homepage_router
from app.modules.admin.public_legal_api import router as public_legal_router
from app.modules.admin.account_deletion_api import (
    admin_router as admin_deletion_router,
    customer_router as customer_deletion_router,
    public_router as public_deletion_router,
)
from app.modules.admin.email_api import router as admin_email_router
from app.modules.admin.public_cab_api import router as public_cab_router
from app.modules.admin.public_hotel_api import router as public_hotel_router
from app.modules.admin.public_coupon_api import router as public_coupon_router
from app.modules.tour.api import (
    admin_router as admin_tour_router,
    partner_router as partner_tour_router,
    public_router as public_tour_router,
    care_router as tour_care_router,
)
from app.modules.admin.public_leads_api import router as public_leads_router
from app.modules.admin.leads_api import router as admin_leads_router
from app.modules.chat.api import router as chat_router
from app.modules.chat.admin_api import router as admin_chat_router
from app.modules.admin.partner_settings_api import (
    router as partner_settings_router,
)
from app.modules.admin.public_firebase_api import router as public_firebase_router
from app.modules.admin.onboarding_docs_api import router as onboarding_docs_router

api_router = APIRouter()

# ── Role groups used by the router-level auth deps below ────────────────────
# These mirror the admin portal's nav matrix (Frontend/admin-portal/src/constants).
# A router-level dep runs for EVERY route on that router, so endpoints that
# previously relied on being "just unauthenticated" are now gated here. Routers
# that need finer-grained or cross-role access (e.g. admin_hotel_router, which
# already guards each route with ADMIN_OFFICER_OR_PARTNER etc.) are left to
# their own per-route deps.
ALL_STAFF = ("SUPER_ADMIN", "ADMIN", "CCO", "FINANCE_MANAGER", "VERIFICATION_OFFICER")
ADMIN_CCO = ("SUPER_ADMIN", "ADMIN", "CCO")
FINANCE_STAFF = ("SUPER_ADMIN", "ADMIN", "FINANCE_MANAGER")
ADMIN_VERIFIER = ("SUPER_ADMIN", "ADMIN", "VERIFICATION_OFFICER")
SUPER_OR_ADMIN = ("SUPER_ADMIN", "ADMIN")

# --- Phase 1 ---
api_router.include_router(auth_router, prefix="/auth", tags=["Authentication"])

# --- Phase 2 ---
api_router.include_router(customer_router, prefix="/customers", tags=["Customers"])
api_router.include_router(partner_router, prefix="/partners", tags=["Partners"])
api_router.include_router(driver_router, prefix="/drivers", tags=["Drivers"])

# --- Driver-app breakdown (mounted under /drivers) ---
api_router.include_router(
    driver_breakdown_router, prefix="/drivers", tags=["Driver Breakdown"]
)

api_router.include_router(vehicle_router, prefix="/vehicles", tags=["Vehicles"])

# --- Phase 3 Admin ---
api_router.include_router(
    admin_settings_router,
    prefix="/admin/settings",
    tags=["Admin Settings"],
    dependencies=[Depends(require_roles(*ALL_STAFF))],
)

# Reports must be registered BEFORE the dashboard router: the dashboard is mounted at
# the bare "/admin" prefix and defines its own legacy /reports/* paths, so whichever
# router is registered first wins the match.
api_router.include_router(
    reports_router,
    prefix="/admin/reports",
    tags=["Admin Reports"],
    dependencies=[Depends(require_roles(*FINANCE_STAFF))],
)

api_router.include_router(
    admin_dashboard_router,
    prefix="/admin",
    tags=["Admin Dashboard"],
    dependencies=[Depends(require_roles(*ALL_STAFF))],
)

# --- Phase 3 Coupons ---
api_router.include_router(
    coupon_router,
    prefix="/admin/coupons",
    tags=["Coupons"],
    dependencies=[Depends(require_roles(*SUPER_OR_ADMIN))],
)

# --- Phase 3 Bookings ---
api_router.include_router(
    admin_booking_router,
    prefix="/admin/bookings",
    tags=["Admin Bookings"],
    dependencies=[Depends(require_roles(*ADMIN_CCO))],
)

# --- Phase 3 Customer Care ---
api_router.include_router(
    customer_care_router,
    prefix="/admin/customer-care",
    tags=["Customer Care"],
    dependencies=[Depends(require_roles(*ADMIN_CCO))],
)

# --- Phase 3 Trip Assistance ---
api_router.include_router(
    trip_assist_router,
    prefix="/admin/trip-assist",
    tags=["Trip Assistance"],
    dependencies=[Depends(require_roles(*ADMIN_CCO))],
)

# --- Cab Operations Console (BRD Part 3 §40, §44) ---
api_router.include_router(
    admin_cab_ops_router,
    prefix="/admin/cab-ops",
    tags=["Cab Operations"],
    dependencies=[Depends(require_roles(*ADMIN_CCO))],
)

# --- Cab Breakdown / Swap (BRD Part 3 §40, §42; spec: in-trip swap) ---
api_router.include_router(
    admin_breakdown_router,
    prefix="/admin/cab-ops/breakdown",
    tags=["Cab Breakdown"],
    dependencies=[Depends(require_roles(*ADMIN_CCO))],
)

# --- Admin Vehicle Maintenance (BRD Part 3 §133-138) ---
api_router.include_router(
    admin_vehicle_admin_router,
    prefix="/admin/vehicles",
    tags=["Admin Vehicles"],
    dependencies=[Depends(require_roles(*ADMIN_VERIFIER))],
)

# --- Phase 3 Settlements ---
api_router.include_router(
    settlement_router,
    prefix="/admin/settlements",
    tags=["Settlements"],
    dependencies=[Depends(require_roles(*FINANCE_STAFF))],
)

# --- Advance Payments ---
api_router.include_router(
    advance_router,
    prefix="/admin/advance",
    tags=["Advance Payments"],
    dependencies=[Depends(require_roles(*FINANCE_STAFF))],
)

# --- Phase 3 Wallets ---
api_router.include_router(
    wallet_router,
    prefix="/admin/wallets",
    tags=["Wallets"],
    dependencies=[Depends(require_roles(*FINANCE_STAFF))],
)

# --- Phase 3 Partner Bookings ---
api_router.include_router(
    partner_booking_router, prefix="/partners", tags=["Partner Bookings"]
)

# --- Partner Driver Cash Collection ---
api_router.include_router(
    partner_cash_router, prefix="/partners", tags=["Partner Cash Collection"]
)

# --- Blog ---
api_router.include_router(
    admin_blog_router,
    prefix="/admin/settings",
    tags=["Admin Blog"],
    dependencies=[Depends(require_roles(*SUPER_OR_ADMIN))],
)
api_router.include_router(public_blog_router, prefix="/public", tags=["Public Blog"])

# --- Partner Wallet ---
api_router.include_router(
    partner_wallet_router, prefix="/partners", tags=["Partner Wallet"]
)

# --- Partner GST Challan ---
api_router.include_router(partner_gst_router, prefix="/partners", tags=["Partner GST"])

# --- Partner Hotel Bookings ---
api_router.include_router(
    partner_hotel_booking_router, prefix="/partners", tags=["Partner Hotel Bookings"]
)

# --- Admin Payments ---
api_router.include_router(
    admin_payment_router,
    prefix="/admin/payments",
    tags=["Admin Payments"],
    dependencies=[Depends(require_roles(*FINANCE_STAFF))],
)

# --- Admin Hotels ---
api_router.include_router(
    admin_hotel_router, prefix="/admin/hotels", tags=["Admin Hotels"]
)

# --- Notifications (Doc Ref: BRD Part 7 §155) ---
# Mounted at /notifications so the partner portal endpoints
# land at /api/v1/notifications/me/fcm-token, /me/notifications, etc.
api_router.include_router(
    notification_router, prefix="/notifications", tags=["Notifications"]
)

# --- Phase 3 Cancellation Engine (Doc Ref: BRD Part 3 §46/§47, BRD Part 4 §82) ---
# Admin-managed cancellation policy + partner request flow + customer self-cancel.
api_router.include_router(
    admin_cancellation_router,
    prefix="/admin/cancellation",
    tags=["Admin Cancellation"],
    dependencies=[Depends(require_roles(*ADMIN_CCO))],
)
api_router.include_router(
    admin_notices_router,
    prefix="/admin/notices",
    tags=["Admin Notices"],
    dependencies=[Depends(require_roles(*ADMIN_CCO))],
)
api_router.include_router(
    partner_notices_router, prefix="/partners", tags=["Partner Notices"]
)

# --- Partner Cancellation Requests ---
# Partners cannot cancel directly — they submit requests, admin approves.
api_router.include_router(
    partner_cancellation_router, prefix="/partners", tags=["Partner Cancellation"]
)

# --- Customer Self-Cancel + Booking List ---
api_router.include_router(
    customer_cancellation_router, prefix="/customers", tags=["Customer Cancellation"]
)

# --- Public Homepage (CMS-driven marketing site) ---
# Doc Ref: Migration 0044_website_cms
# No auth — used by customer-web's SSR homepage.
api_router.include_router(
    public_homepage_router, prefix="/public", tags=["Public Homepage"]
)
api_router.include_router(public_legal_router, prefix="/public", tags=["Public Legal"])
api_router.include_router(
    public_cab_router, prefix="/public/cab", tags=["Public Cab Search"]
)
api_router.include_router(
    public_hotel_router, prefix="/public/hotel", tags=["Public Hotel Search"]
)
api_router.include_router(
    public_coupon_router, prefix="/public/coupon", tags=["Public Coupons"]
)

# --- Tour Packages ---
api_router.include_router(
    admin_tour_router,
    prefix="/admin/tours",
    tags=["Admin Tours"],
    dependencies=[Depends(require_roles(*SUPER_OR_ADMIN))],
)
api_router.include_router(
    partner_tour_router, prefix="/partners/tours", tags=["Partner Tours"]
)
api_router.include_router(
    public_tour_router, prefix="/public/tours", tags=["Public Tours"]
)
api_router.include_router(
    tour_care_router,
    prefix="/admin/customer-care",
    tags=["Tour Customer Care"],
    dependencies=[Depends(require_roles(*ADMIN_CCO))],
)

# --- Website Lead Capture (Doc Ref: Website Lead Capture §2/§3) ---
# Public: partner applications + contact messages + booking tracking.
# Admin: review surfaces in the admin portal (/admin/leads/*).
api_router.include_router(public_leads_router, prefix="/public", tags=["Public Leads"])

# --- Account Deletion (customer website form + admin review) ---
api_router.include_router(
    public_deletion_router, prefix="/public", tags=["Account Deletion"]
)
api_router.include_router(
    customer_deletion_router, prefix="/customers", tags=["Account Deletion"]
)
api_router.include_router(
    admin_deletion_router,
    prefix="/admin/account-deletion",
    tags=["Account Deletion"],
    dependencies=[Depends(require_roles(*ADMIN_CCO))],
)
api_router.include_router(
    admin_leads_router,
    prefix="/admin/leads",
    tags=["Admin Leads"],
    dependencies=[Depends(require_roles(*ADMIN_CCO))],
)
api_router.include_router(
    admin_email_router,
    prefix="/admin/email",
    tags=["Email"],
    dependencies=[Depends(require_roles(*SUPER_OR_ADMIN))],
)

# --- Live Chat (Doc Ref: Website Chat §2/§4) ---
# Public: customer-web chat widget (smart routing + realtime).
# Admin: portal chat board + presence heartbeat for online routing.
api_router.include_router(chat_router, prefix="/chat", tags=["Chat"])
api_router.include_router(
    admin_chat_router,
    prefix="/admin/chat",
    tags=["Admin Chat"],
    dependencies=[Depends(require_roles(*ADMIN_CCO))],
)

# --- Partner Settings (partner-portal dependencies formerly on /admin/settings) ---
api_router.include_router(
    partner_settings_router,
    prefix="/partners/settings",
    tags=["Partner Settings"],
    dependencies=[Depends(require_roles("PARTNER", "ADMIN", "SUPER_ADMIN"))],
)

# --- Public FCM web config (replaces partner-portal read of api-integrations) ---
api_router.include_router(
    public_firebase_router,
    prefix="/public",
    tags=["Public Firebase Config"],
)

# --- Onboarding Guides + Forms (Docs/22_Partner_Onboarding_Guides) ---
# Print-quality PDFs for partners: requirement guides + hand-fillable forms.
api_router.include_router(
    onboarding_docs_router,
    prefix="/admin/onboarding-docs",
    tags=["Onboarding Docs"],
    dependencies=[Depends(require_roles(*ALL_STAFF))],
)
