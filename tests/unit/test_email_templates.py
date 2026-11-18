# ============================================================
# WAYTERO — EMAIL TEMPLATE RENDERING
# File: tests/unit/test_email_templates.py
#
# The admin template overrides the words; the branded shell (logo, platform
# name, support footer) must always come from platform settings.
# ============================================================

from app.infrastructure.email import render_email

CFG = {
    "platform_name": "WayTero",
    "logo_url": "https://cdn.example.com/logo.png",
    "support_email": "support@waytero.com",
    "support_phone": "+91 90000 00000",
    "company_address": "Bhubaneswar, Odisha",
    "web_url": "https://www.waytero.com",
}


def test_template_override_fills_placeholders_and_keeps_branding():
    template = {
        "subject": "Trip {booking_number} started",
        "heading": "On the way",
        "body": "Hi {name},\n\nYour trip {booking_number} has started.",
        "cta_label": "Track",
        "cta_url": "{web_url}/bookings",
        "tone": "#0F9D58",
        "is_active": True,
    }

    subject, html = render_email(
        "trip_started",
        {"booking_number": "WT-CAB-1", "name": "Anita"},
        CFG,
        template,
    )

    assert subject == "Trip WT-CAB-1 started"
    assert "On the way" in html
    assert "Hi Anita" in html
    assert "Your trip WT-CAB-1 has started." in html
    assert "https://cdn.example.com/logo.png" in html
    assert "support@waytero.com" in html
    assert "Track" in html


def test_template_body_is_escaped():
    template = {
        "subject": "Hello {name}",
        "body": "<script>alert('x')</script>",
        "is_active": True,
    }

    _, html = render_email(
        "trip_started",
        {"name": "<b>Anita</b>", "booking_number": "WT-2"},
        CFG,
        template,
    )

    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_default_template_still_renders_without_override():
    subject, html = render_email(
        "booking_requested",
        {"booking_number": "WT-3", "service": "Cab"},
        CFG,
    )

    assert "WT-3" in subject
    assert "WayTero" in html
