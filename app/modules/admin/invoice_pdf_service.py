# ============================================================
# WAYTERO — PREMIUM INVOICE PDF SERVICE
# File: app/modules/admin/invoice_pdf_service.py
#
# Purpose:
#   Generate a premium, print-quality PDF invoice for completed
#   cab bookings using ReportLab.
#
#   Platform branding (logo, name, GST, address, etc.) is pulled
#   from system_configurations at render time so that any admin
#   changes to Settings are immediately reflected in new PDFs.
#
# Config keys read from system_configurations:
#   PLATFORM_NAME             — display name (e.g. "WayTero")
#   PLATFORM_LOGO_URL         — Cloudinary URL of logo (fetched & embedded)
#   BUSINESS_LEGAL_NAME       — registered legal name
#   BUSINESS_GST_NUMBER       — GSTIN
#   BUSINESS_REGISTERED_ADDRESS — registered address
#   SUPPORT_EMAIL             — support contact email
#   SUPPORT_PHONE             — support contact phone
#
# Doc Ref:
#   BRD Part 3 §45 — Trip Completion & Invoice
#   DB Schema Part 1 §12 — system_configurations
#   DB Schema Part 7 §15 — invoice_number / invoice_url on cab_bookings
# ============================================================

from __future__ import annotations

import io
import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    HRFlowable,
    Image,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

log = logging.getLogger(__name__)

# ── Palette ──────────────────────────────────────────────────────────────────
BRAND_DARK = colors.HexColor("#0d1b2a")  # deep navy
BRAND_MAIN = colors.HexColor("#1565c0")  # WayTero blue
BRAND_ACCENT = colors.HexColor("#00b4d8")  # cyan accent
BRAND_GOLD = colors.HexColor("#f59e0b")  # amber for totals
GREY_100 = colors.HexColor("#f8fafc")
GREY_200 = colors.HexColor("#e2e8f0")
GREY_600 = colors.HexColor("#475569")
GREY_900 = colors.HexColor("#0f172a")
SUCCESS = colors.HexColor("#16a34a")
WHITE = colors.white


# ── Fetch logo bytes (with timeout, fail gracefully) ─────────────────────────
def _fetch_logo_bytes(url: str) -> bytes | None:
    if not url or not url.startswith("http"):
        return None
    try:
        resp = httpx.get(url, timeout=5)
        if resp.status_code == 200 and resp.content:
            return resp.content
    except Exception:
        pass
    return None


# ── Paragraph helpers ─────────────────────────────────────────────────────────
def _ps(
    name: str,
    size: float,
    color=GREY_900,
    bold=False,
    align=TA_LEFT,
    line_height: float | None = None,
    space_after: int = 0,
) -> ParagraphStyle:
    return ParagraphStyle(
        name,
        fontSize=size,
        fontName="Helvetica-Bold" if bold else "Helvetica",
        textColor=color,
        alignment=align,
        leading=line_height or size * 1.4,
        spaceAfter=space_after,
    )


def _p(text: str, style: ParagraphStyle) -> Paragraph:
    return Paragraph(text, style)


# ── Indian-numbering amount in words ─────────────────────────────────────────
# Advance receipts are hand-carried and countersigned in the field, so the
# amount is spelled out to make tampering with the digits obvious.
_ONES = [
    "",
    "One",
    "Two",
    "Three",
    "Four",
    "Five",
    "Six",
    "Seven",
    "Eight",
    "Nine",
    "Ten",
    "Eleven",
    "Twelve",
    "Thirteen",
    "Fourteen",
    "Fifteen",
    "Sixteen",
    "Seventeen",
    "Eighteen",
    "Nineteen",
]
_TENS = [
    "",
    "",
    "Twenty",
    "Thirty",
    "Forty",
    "Fifty",
    "Sixty",
    "Seventy",
    "Eighty",
    "Ninety",
]


def _two_digits(n: int) -> str:
    if n < 20:
        return _ONES[n]
    return (_TENS[n // 10] + (" " + _ONES[n % 10] if n % 10 else "")).strip()


def _amount_in_words(amount: float) -> str:
    """Render a rupee amount in Indian numbering (lakh / crore), e.g.
    1234.50 -> 'Rupees One Thousand Two Hundred Thirty Four and Fifty Paise Only'."""
    rupees = int(amount)
    paise = int(round((amount - rupees) * 100))
    if paise == 100:  # 99.999 rounds up into the next rupee
        rupees += 1
        paise = 0

    if rupees == 0:
        words = "Zero"
    else:
        parts: list[str] = []
        # Indian grouping: crore, lakh, thousand, then the trailing three digits.
        for divisor, label in (
            (10_000_000, "Crore"),
            (100_000, "Lakh"),
            (1_000, "Thousand"),
        ):
            if rupees >= divisor:
                parts.append(f"{_two_digits(rupees // divisor)} {label}")
                rupees %= divisor
        if rupees >= 100:
            parts.append(f"{_ONES[rupees // 100]} Hundred")
            rupees %= 100
        if rupees:
            parts.append(_two_digits(rupees))
        words = " ".join(parts)

    out = f"Rupees {words}"
    if paise:
        out += f" and {_two_digits(paise)} Paise"
    return out + " Only"


# ─────────────────────────────────────────────────────────────────────────────
# MAIN GENERATOR
# ─────────────────────────────────────────────────────────────────────────────


def generate_invoice_pdf(
    *,
    # Invoice meta
    invoice_number: str,
    cab_booking_number: str,
    booking_number: str,
    invoice_date: datetime | None = None,
    # Customer
    customer_name: str | None,
    customer_mobile: str | None,
    # Trip details
    city_name: str | None,
    pickup_location: str | None,
    drop_location: str | None,
    trip_type: str | None,
    vehicle_category_name: str | None,
    vehicle_reg: str | None,
    vehicle_model: str | None,
    driver_name: str | None,
    partner_name: str | None,
    trip_started_at: datetime | None,
    trip_ended_at: datetime | None,
    trip_start_km: float | None,
    trip_end_km: float | None,
    actual_distance: float | None,
    # Billing
    estimated_amount: float | None,
    final_amount: float,
    coupon_discount: float,
    advance_paid: float,
    payment_mode: str | None,
    payment_collected_by: str | None,
    platform_commission: float | None,
    # GST / Tax Invoice (migration 0024)
    is_tax_invoice: bool = False,
    gst_rate: float = 0.0,
    gst_amount: float = 0.0,
    # Platform branding (from system_configurations)
    platform_name: str = "WayTero",
    platform_logo_url: str = "",
    business_legal_name: str = "",
    business_gst_number: str = "",
    business_registered_address: str = "",
    support_email: str = "",
    support_phone: str = "",
) -> bytes:
    """
    Build and return a premium A4 PDF invoice as raw bytes.
    """

    buf = io.BytesIO()
    W, H = A4  # 595.28 x 841.89 pts

    # ── Page setup ───────────────────────────────────────────────────────────
    MARGIN = 18 * mm
    doc = BaseDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=12 * mm,
        bottomMargin=14 * mm,
        title=f"Invoice {invoice_number}",
        author=platform_name,
    )

    content_width = W - 2 * MARGIN

    # ── Page background & footer callback ────────────────────────────────────
    inv_dt = invoice_date or datetime.now(timezone.utc)
    inv_dt_str = inv_dt.strftime("%d %b %Y, %I:%M %p") + " IST"

    def _draw_page(canvas, doc):
        canvas.saveState()

        # Header band — deep navy full width
        canvas.setFillColor(BRAND_DARK)
        canvas.rect(0, H - 22 * mm, W, 22 * mm, fill=1, stroke=0)

        # Thin accent stripe below header
        canvas.setFillColor(BRAND_ACCENT)
        canvas.rect(0, H - 23.2 * mm, W, 1.2 * mm, fill=1, stroke=0)

        # Footer band
        footer_h = 10 * mm
        canvas.setFillColor(BRAND_DARK)
        canvas.rect(0, 0, W, footer_h, fill=1, stroke=0)

        # Footer text
        canvas.setFillColor(colors.HexColor("#94a3b8"))
        canvas.setFont("Helvetica", 7)
        footer_y = 3.5 * mm
        canvas.drawString(
            MARGIN, footer_y, f"Generated on {inv_dt_str}  |  {invoice_number}"
        )
        canvas.drawRightString(
            W - MARGIN,
            footer_y,
            f"{platform_name}  |  {support_email}  |  {support_phone}",
        )

        # Page number
        canvas.setFillColor(colors.HexColor("#64748b"))
        canvas.setFont("Helvetica", 6.5)
        canvas.drawCentredString(W / 2, footer_y, f"Page {doc.page}")

        canvas.restoreState()

    frame = Frame(
        MARGIN,
        14 * mm,
        content_width,
        H - 22 * mm - 14 * mm - 4 * mm,
        showBoundary=0,
    )
    doc.addPageTemplates([PageTemplate(id="main", frames=[frame], onPage=_draw_page)])

    # ── Styles ────────────────────────────────────────────────────────────────
    S = {
        "plat_name": _ps("plat_name", 16, WHITE, bold=True, align=TA_LEFT),
        "plat_tagline": _ps("plat_tag", 7, colors.HexColor("#94a3b8"), align=TA_LEFT),
        "inv_label": _ps("inv_lbl", 22, WHITE, bold=True, align=TA_RIGHT),
        "inv_num": _ps("inv_num", 9, BRAND_ACCENT, bold=True, align=TA_RIGHT),
        "section_h": _ps("sec_h", 9, BRAND_MAIN, bold=True, space_after=4),
        "label": _ps("lbl", 7.5, GREY_600),
        "value": _ps("val", 9, GREY_900, bold=True),
        "value_sm": _ps("val_sm", 8, GREY_900),
        "total_lbl": _ps("tot_lbl", 10, GREY_900, bold=True, align=TA_RIGHT),
        "total_val": _ps("tot_val", 14, SUCCESS, bold=True, align=TA_RIGHT),
        "footer_note": _ps("fn", 7, GREY_600, align=TA_CENTER),
        "gst_note": _ps("gst", 7.5, GREY_600),
        "paid_stamp": _ps("paid", 18, SUCCESS, bold=True, align=TA_CENTER),
    }

    story: list[Any] = []

    # ── 1. HEADER BLOCK ──────────────────────────────────────────────────────
    # We build a 2-col table that sits inside the dark header band
    # The header band is drawn by _draw_page at H-22mm
    # We need a spacer to clear the dark band (22mm) plus accent (1.2mm)
    story.append(Spacer(1, 22 * mm + 1.2 * mm + 3 * mm))

    # ── 2. LOGO + PLATFORM NAME row ──────────────────────────────────────────
    # Attempt to fetch & embed logo
    logo_img = None
    logo_bytes = _fetch_logo_bytes(platform_logo_url)
    if logo_bytes:
        try:
            logo_img = Image(
                io.BytesIO(logo_bytes),
                width=28 * mm,
                height=10 * mm,
                kind="proportional",
            )
        except Exception:
            logo_img = None

    header_left_content: list[Any] = []
    if logo_img:
        header_left_content.append(logo_img)
        header_left_content.append(Spacer(1, 2 * mm))
    header_left_content.append(
        _p(f"<b>{platform_name}</b>", _ps("hn", 14, BRAND_MAIN, bold=True))
    )
    if business_legal_name:
        header_left_content.append(_p(business_legal_name, _ps("bln", 7.5, GREY_600)))
    if business_gst_number:
        header_left_content.append(
            _p(f"GSTIN: <b>{business_gst_number}</b>", _ps("gst2", 7.5, GREY_600))
        )
    if business_registered_address:
        header_left_content.append(
            _p(business_registered_address, _ps("adr", 7, GREY_600))
        )

    header_right_content = [
        _p("INVOICE", _ps("INVTITLE", 24, BRAND_DARK, bold=True, align=TA_RIGHT)),
        Spacer(1, 1 * mm),
        _p(invoice_number, _ps("invn", 10, BRAND_MAIN, bold=True, align=TA_RIGHT)),
        Spacer(1, 1 * mm),
        _p(
            f"Date: {inv_dt.strftime('%d %B %Y')}",
            _ps("invdate", 8, GREY_600, align=TA_RIGHT),
        ),
    ]

    header_tbl = Table(
        [[header_left_content, header_right_content]],
        colWidths=[content_width * 0.55, content_width * 0.45],
    )
    header_tbl.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    story.append(header_tbl)
    story.append(Spacer(1, 5 * mm))
    story.append(HRFlowable(width="100%", thickness=1, color=GREY_200))
    story.append(Spacer(1, 4 * mm))

    # ── 3. BILLED TO / BOOKING INFO row ──────────────────────────────────────
    _gross_total = round(final_amount + (gst_amount if is_tax_invoice else 0.0), 2)
    balance_due = max(0.0, round(_gross_total - coupon_discount - advance_paid, 2))

    billed_block = [
        _p("BILLED TO", S["section_h"]),
        _p(customer_name or "—", S["value"]),
        _p(customer_mobile or "", S["value_sm"]),
        Spacer(1, 3 * mm),
        _p("TRIP REFERENCE", S["section_h"]),
        _p(f"Cab Booking: <b>{cab_booking_number}</b>", S["value_sm"]),
        _p(f"Master Booking: {booking_number}", S["gst_note"]),
        _p(f"City: {city_name or '—'}", S["gst_note"]),
    ]

    payment_status_label = "PAID" if balance_due <= 0 else "PARTIALLY PAID"
    booking_block = [
        _p("BOOKING SUMMARY", S["section_h"]),
        _p(
            f"Type: <b>{(trip_type or '—').replace('_', ' ').title()}</b>",
            S["value_sm"],
        ),
        _p(
            f"Vehicle: {vehicle_category_name or '—'} — {vehicle_model or '—'}",
            S["gst_note"],
        ),
        _p(f"Reg No.: {vehicle_reg or '—'}", S["gst_note"]),
        _p(f"Driver: {driver_name or '—'}", S["gst_note"]),
        _p(f"Partner: {partner_name or '—'}", S["gst_note"]),
        Spacer(1, 3 * mm),
        _p("PAYMENT STATUS", S["section_h"]),
        _p(
            f"<b>{payment_status_label}</b>",
            _ps("psl", 9, SUCCESS if balance_due <= 0 else BRAND_GOLD, bold=True),
        ),
        _p(
            f"Mode: {payment_mode or '—'} | Collected by: {(payment_collected_by or '—').title()}",
            S["gst_note"],
        ),
    ]

    bill_tbl = Table(
        [[billed_block, booking_block]],
        colWidths=[content_width * 0.5, content_width * 0.5],
    )
    bill_tbl.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    story.append(bill_tbl)
    story.append(Spacer(1, 5 * mm))

    # ── 4. TRIP ROUTE SECTION ─────────────────────────────────────────────────
    story.append(HRFlowable(width="100%", thickness=0.5, color=GREY_200))
    story.append(Spacer(1, 3 * mm))
    story.append(_p("TRIP DETAILS", S["section_h"]))
    story.append(Spacer(1, 2 * mm))

    def _fmt_dt(dt: datetime | None) -> str:
        if not dt:
            return "—"
        return dt.strftime("%d %b %Y, %I:%M %p")

    route_data = [
        ["", "PICKUP", "DROP"],
        [
            "Location",
            Paragraph(pickup_location or "—", _ps("pl", 8, GREY_900)),
            Paragraph(drop_location or "—", _ps("dl", 8, GREY_900)),
        ],
        ["Date / Time", _fmt_dt(trip_started_at), _fmt_dt(trip_ended_at)],
        [
            "Odometer",
            f"{trip_start_km:.1f} km" if trip_start_km is not None else "—",
            f"{trip_end_km:.1f} km" if trip_end_km is not None else "—",
        ],
    ]
    route_tbl = Table(
        route_data,
        colWidths=[content_width * 0.20, content_width * 0.40, content_width * 0.40],
    )
    route_tbl.setStyle(
        TableStyle(
            [
                # Header row
                ("BACKGROUND", (1, 0), (2, 0), BRAND_DARK),
                ("TEXTCOLOR", (1, 0), (2, 0), WHITE),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, 0), 8),
                ("ALIGN", (1, 0), (2, 0), "CENTER"),
                # Body
                ("FONTNAME", (0, 1), (0, -1), "Helvetica-Bold"),
                ("FONTSIZE", (0, 1), (0, -1), 8),
                ("TEXTCOLOR", (0, 1), (0, -1), GREY_600),
                ("FONTNAME", (1, 1), (-1, -1), "Helvetica"),
                ("FONTSIZE", (1, 1), (-1, -1), 8.5),
                ("TEXTCOLOR", (1, 1), (-1, -1), GREY_900),
                # Row shading
                ("BACKGROUND", (0, 1), (-1, 1), GREY_100),
                ("BACKGROUND", (0, 3), (-1, 3), GREY_100),
                # Grid
                ("GRID", (0, 0), (-1, -1), 0.4, GREY_200),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                # Actual distance footer
                ("SPAN", (0, -1), (0, -1)),
            ]
        )
    )
    story.append(route_tbl)

    if actual_distance is not None:
        story.append(Spacer(1, 1.5 * mm))
        story.append(
            _p(
                f"&#9656; Actual Distance Travelled: <b>{actual_distance:.2f} km</b>",
                _ps("ad", 8, GREY_600),
            )
        )

    story.append(Spacer(1, 5 * mm))

    # ── 5. FARE BREAKDOWN TABLE ───────────────────────────────────────────────
    story.append(HRFlowable(width="100%", thickness=0.5, color=GREY_200))
    story.append(Spacer(1, 3 * mm))
    story.append(_p("FARE BREAKDOWN", S["section_h"]))
    story.append(Spacer(1, 2 * mm))

    fare_rows: list[list] = [
        [
            Paragraph("Description", _ps("fh1", 8, WHITE, bold=True)),
            Paragraph("Amount", _ps("fh2", 8, WHITE, bold=True, align=TA_RIGHT)),
        ],
    ]

    # Base fare / estimated vs actual
    if estimated_amount and estimated_amount != final_amount:
        fare_rows.append(
            [
                Paragraph("Estimated Fare", _ps("fi", 8.5, GREY_900)),
                Paragraph(
                    f"Rs. {estimated_amount:,.2f}",
                    _ps("fv", 8.5, GREY_900, align=TA_RIGHT),
                ),
            ]
        )
        fare_rows.append(
            [
                Paragraph("Fare Adjustment (Actual)", _ps("fi", 8.5, GREY_900)),
                Paragraph(
                    f"Rs. {final_amount - estimated_amount:+,.2f}",
                    _ps("fv", 8.5, GREY_900, align=TA_RIGHT),
                ),
            ]
        )
    else:
        fare_rows.append(
            [
                Paragraph("Trip Fare", _ps("fi", 8.5, GREY_900)),
                Paragraph(
                    f"Rs. {final_amount:,.2f}", _ps("fv", 8.5, GREY_900, align=TA_RIGHT)
                ),
            ]
        )

    if coupon_discount > 0:
        fare_rows.append(
            [
                Paragraph("Coupon / Discount", _ps("fi", 8.5, SUCCESS)),
                Paragraph(
                    f"− Rs. {coupon_discount:,.2f}",
                    _ps("fv", 8.5, SUCCESS, align=TA_RIGHT),
                ),
            ]
        )

    if advance_paid > 0:
        fare_rows.append(
            [
                Paragraph("Advance Paid", _ps("fi", 8.5, GREY_600)),
                Paragraph(
                    f"− Rs. {advance_paid:,.2f}",
                    _ps("fv", 8.5, GREY_600, align=TA_RIGHT),
                ),
            ]
        )

    # Subtotal after discounts
    subtotal = final_amount - coupon_discount
    if coupon_discount > 0:
        fare_rows.append(
            [
                Paragraph("Subtotal (after discount)", _ps("fi", 8, GREY_600)),
                Paragraph(
                    f"Rs. {subtotal:,.2f}", _ps("fv", 8, GREY_600, align=TA_RIGHT)
                ),
            ]
        )

    # GST row — only when this booking is a tax invoice
    if is_tax_invoice and gst_amount > 0:
        fare_rows.append(
            [
                Paragraph(f"GST ({gst_rate:.0f}%)", _ps("gst_lbl", 8.5, GREY_600)),
                Paragraph(
                    f"Rs. {gst_amount:,.2f}",
                    _ps("gst_val", 8.5, GREY_600, align=TA_RIGHT),
                ),
            ]
        )

    # Blank separator
    fare_rows.append(["", ""])

    # Grand total row — includes GST when applicable
    total_payable = round(final_amount + (gst_amount if is_tax_invoice else 0.0), 2)
    fare_rows.append(
        [
            Paragraph("TOTAL PAYABLE", _ps("gtl", 10, BRAND_DARK, bold=True)),
            Paragraph(
                f"Rs. {total_payable:,.2f}",
                _ps("gtv", 11, BRAND_DARK, bold=True, align=TA_RIGHT),
            ),
        ]
    )

    # Balance due
    if balance_due > 0:
        fare_rows.append(
            [
                Paragraph("Balance Due", _ps("bdl", 9, BRAND_GOLD, bold=True)),
                Paragraph(
                    f"Rs. {balance_due:,.2f}",
                    _ps("bdv", 9, BRAND_GOLD, bold=True, align=TA_RIGHT),
                ),
            ]
        )
    else:
        fare_rows.append(
            [
                Paragraph("✔  Fully Paid", _ps("fpl", 9, SUCCESS, bold=True)),
                Paragraph(
                    "Rs. 0.00", _ps("fpv", 9, SUCCESS, bold=True, align=TA_RIGHT)
                ),
            ]
        )

    n_rows = len(fare_rows)
    fare_tbl = Table(
        fare_rows,
        colWidths=[content_width * 0.65, content_width * 0.35],
    )
    fare_style = [
        # Header
        ("BACKGROUND", (0, 0), (-1, 0), BRAND_MAIN),
        ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 8),
        # Alternate row shading
        ("GRID", (0, 0), (-1, -1), 0.3, GREY_200),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        # Grand total row highlight
        ("BACKGROUND", (0, n_rows - 2), (-1, n_rows - 2), colors.HexColor("#e0f2fe")),
        ("LINEABOVE", (0, n_rows - 2), (-1, n_rows - 2), 1.2, BRAND_MAIN),
        # Final (balance/paid) row
        (
            "BACKGROUND",
            (0, n_rows - 1),
            (-1, n_rows - 1),
            (
                colors.HexColor("#dcfce7")
                if balance_due <= 0
                else colors.HexColor("#fefce8")
            ),
        ),
        (
            "LINEBELOW",
            (0, n_rows - 1),
            (-1, n_rows - 1),
            1.5,
            SUCCESS if balance_due <= 0 else BRAND_GOLD,
        ),
    ]
    # Shade every other body row
    for i in range(1, n_rows - 2):
        if i % 2 == 0:
            fare_style.append(("BACKGROUND", (0, i), (-1, i), GREY_100))

    fare_tbl.setStyle(TableStyle(fare_style))
    story.append(fare_tbl)
    story.append(Spacer(1, 6 * mm))

    # ── 6. THANK YOU + TERMS ─────────────────────────────────────────────────
    story.append(HRFlowable(width="100%", thickness=1, color=BRAND_ACCENT))
    story.append(Spacer(1, 4 * mm))

    thank_you_block = [
        _p(
            f"Thank you for choosing <b>{platform_name}</b>!",
            _ps("ty", 10, BRAND_DARK, bold=True, align=TA_CENTER),
        ),
        Spacer(1, 1.5 * mm),
        _p(
            "This is a computer-generated invoice and does not require a physical signature. "
            "For queries, contact our support team.",
            _ps("tyb", 7.5, GREY_600, align=TA_CENTER),
        ),
    ]
    if support_email or support_phone:
        contact = "  |  ".join(filter(None, [support_email, support_phone]))
        thank_you_block.append(
            _p(f"✉ {contact}", _ps("ct", 7.5, BRAND_MAIN, bold=True, align=TA_CENTER))
        )
    if is_tax_invoice and gst_amount > 0:
        thank_you_block.append(Spacer(1, 1.5 * mm))
        thank_you_block.append(
            _p(
                f"This is a <b>Tax Invoice</b> | GST @ {gst_rate:.0f}%: Rs. {gst_amount:,.2f} included.",
                _ps("taxnote", 7.5, BRAND_MAIN, align=TA_CENTER),
            )
        )
    if business_gst_number:
        thank_you_block.append(Spacer(1, 1.5 * mm))
        thank_you_block.append(
            _p(
                f"Platform GSTIN: <b>{business_gst_number}</b>",
                _ps("gstf", 7, GREY_600, align=TA_CENTER),
            )
        )
    if not is_tax_invoice:
        thank_you_block.append(Spacer(1, 1.5 * mm))
        thank_you_block.append(
            _p(
                "Non-tax receipt — GST not applicable on this booking.",
                _ps("nontax", 7, GREY_600, align=TA_CENTER),
            )
        )

    for item in thank_you_block:
        story.append(item)

    # ── BUILD ─────────────────────────────────────────────────────────────────
    doc.build(story)
    return buf.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
# ADVANCE RECEIPT GENERATOR
#
# Why a separate generator rather than a flag on generate_invoice_pdf:
#   generate_invoice_pdf hard-requires trip facts — odometer readings, actual
#   distance, trip start/end timestamps, a final fare — none of which exist
#   when an advance is taken (the trip has not run yet). Threading "all of
#   this is None" through that function would riddle it with branches.
#
#   This is deliberately a *receipt*, not an invoice: no GST is charged on an
#   advance, no invoice number is burned, and the document carries an explicit
#   disclaimer so it can never be mistaken for the tax invoice that follows at
#   trip end. It reuses the same palette, helpers and header/footer frames so
#   the customer sees one visual family across both documents.
#
# Doc Ref: BRD Part 3 §45 — Advance collection & settlement custody
# ─────────────────────────────────────────────────────────────────────────────

_RECEIVER_LABELS = {
    "ADMIN": "Platform Office",
    "PARTNER": "Fleet Partner",
    "DRIVER": "Driver",
}


def generate_advance_receipt_pdf(
    *,
    # Receipt meta
    receipt_number: str,
    cab_booking_number: str,
    booking_number: str,
    # Customer
    customer_name: str | None,
    customer_mobile: str | None,
    # Booking context (known at advance time — no trip facts yet)
    city_name: str | None = None,
    trip_type: str | None = None,
    pickup_location: str | None = None,
    drop_location: str | None = None,
    pickup_datetime: datetime | None = None,
    vehicle_category_name: str | None = None,
    partner_name: str | None = None,
    driver_name: str | None = None,
    # The advance itself
    amount: float,
    payment_mode: str,
    received_by: str,
    receiver_name: str | None = None,
    reference_note: str | None = None,
    collected_at: datetime | None = None,
    # Indicative figures — the fare can still move before trip end
    estimated_amount: float | None = None,
    balance_after_advance: float | None = None,
    # Platform branding (from system_configurations)
    platform_name: str = "WayTero",
    platform_logo_url: str = "",
    business_legal_name: str = "",
    business_gst_number: str = "",
    business_registered_address: str = "",
    support_email: str = "",
    support_phone: str = "",
) -> bytes:
    """
    Build and return an A4 advance-payment receipt as raw bytes.
    """

    buf = io.BytesIO()
    W, H = A4

    MARGIN = 18 * mm
    doc = BaseDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=12 * mm,
        bottomMargin=14 * mm,
        title=f"Advance Receipt {receipt_number}",
        author=platform_name,
    )

    content_width = W - 2 * MARGIN

    rec_dt = collected_at or datetime.now(timezone.utc)
    rec_dt_str = rec_dt.strftime("%d %b %Y, %I:%M %p") + " IST"

    def _draw_page(canvas, doc):
        canvas.saveState()

        # Header band — deep navy full width (matches the invoice)
        canvas.setFillColor(BRAND_DARK)
        canvas.rect(0, H - 22 * mm, W, 22 * mm, fill=1, stroke=0)

        # Amber accent stripe — the one intentional difference from the invoice's
        # cyan: an advance is provisional money, the invoice is settled money.
        canvas.setFillColor(BRAND_GOLD)
        canvas.rect(0, H - 23.2 * mm, W, 1.2 * mm, fill=1, stroke=0)

        # Footer band
        footer_h = 10 * mm
        canvas.setFillColor(BRAND_DARK)
        canvas.rect(0, 0, W, footer_h, fill=1, stroke=0)

        canvas.setFillColor(colors.HexColor("#94a3b8"))
        canvas.setFont("Helvetica", 7)
        footer_y = 3.5 * mm
        canvas.drawString(
            MARGIN, footer_y, f"Issued on {rec_dt_str}  |  {receipt_number}"
        )
        canvas.drawRightString(
            W - MARGIN,
            footer_y,
            f"{platform_name}  |  {support_email}  |  {support_phone}",
        )

        canvas.setFillColor(colors.HexColor("#64748b"))
        canvas.setFont("Helvetica", 6.5)
        canvas.drawCentredString(W / 2, footer_y, f"Page {doc.page}")

        canvas.restoreState()

    frame = Frame(
        MARGIN,
        14 * mm,
        content_width,
        H - 22 * mm - 14 * mm - 4 * mm,
        showBoundary=0,
    )
    doc.addPageTemplates([PageTemplate(id="main", frames=[frame], onPage=_draw_page)])

    S = {
        "section_h": _ps("sec_h", 9, BRAND_MAIN, bold=True, space_after=4),
        "value": _ps("val", 9, GREY_900, bold=True),
        "value_sm": _ps("val_sm", 8, GREY_900),
        "note": _ps("note", 7.5, GREY_600),
    }

    story: list[Any] = []

    # ── 1. HEADER ────────────────────────────────────────────────────────────
    story.append(Spacer(1, 22 * mm + 1.2 * mm + 3 * mm))

    logo_img = None
    logo_bytes = _fetch_logo_bytes(platform_logo_url)
    if logo_bytes:
        try:
            logo_img = Image(
                io.BytesIO(logo_bytes),
                width=28 * mm,
                height=10 * mm,
                kind="proportional",
            )
        except Exception:
            logo_img = None

    header_left: list[Any] = []
    if logo_img:
        header_left.append(logo_img)
        header_left.append(Spacer(1, 2 * mm))
    header_left.append(
        _p(f"<b>{platform_name}</b>", _ps("hn", 14, BRAND_MAIN, bold=True))
    )
    if business_legal_name:
        header_left.append(_p(business_legal_name, _ps("bln", 7.5, GREY_600)))
    if business_gst_number:
        header_left.append(
            _p(f"GSTIN: <b>{business_gst_number}</b>", _ps("gst2", 7.5, GREY_600))
        )
    if business_registered_address:
        header_left.append(_p(business_registered_address, _ps("adr", 7, GREY_600)))

    header_right = [
        _p(
            "ADVANCE RECEIPT",
            _ps("RCPTTITLE", 17, BRAND_DARK, bold=True, align=TA_RIGHT),
        ),
        Spacer(1, 1 * mm),
        _p(receipt_number, _ps("rcptn", 10, BRAND_GOLD, bold=True, align=TA_RIGHT)),
        Spacer(1, 1 * mm),
        _p(
            f"Date: {rec_dt.strftime('%d %B %Y')}",
            _ps("rcptdate", 8, GREY_600, align=TA_RIGHT),
        ),
    ]

    header_tbl = Table(
        [[header_left, header_right]],
        colWidths=[content_width * 0.55, content_width * 0.45],
    )
    header_tbl.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    story.append(header_tbl)
    story.append(Spacer(1, 5 * mm))
    story.append(HRFlowable(width="100%", thickness=1, color=GREY_200))
    story.append(Spacer(1, 4 * mm))

    # ── 2. BILLED TO / BOOKING SUMMARY ───────────────────────────────────────
    billed_block = [
        _p("RECEIVED FROM", S["section_h"]),
        _p(customer_name or "—", S["value"]),
        _p(customer_mobile or "", S["value_sm"]),
        Spacer(1, 3 * mm),
        _p("TRIP REFERENCE", S["section_h"]),
        _p(f"Cab Booking: <b>{cab_booking_number}</b>", S["value_sm"]),
        _p(f"Master Booking: {booking_number}", S["note"]),
        _p(f"City: {city_name or '—'}", S["note"]),
    ]

    def _fmt_dt(dt: datetime | None) -> str:
        return dt.strftime("%d %b %Y, %I:%M %p") if dt else "—"

    booking_block = [
        _p("BOOKING SUMMARY", S["section_h"]),
        _p(
            f"Type: <b>{(trip_type or '—').replace('_', ' ').title()}</b>",
            S["value_sm"],
        ),
        _p(f"Pickup on: {_fmt_dt(pickup_datetime)}", S["note"]),
        _p(f"Vehicle: {vehicle_category_name or '—'}", S["note"]),
        _p(f"Partner: {partner_name or 'Not assigned yet'}", S["note"]),
        _p(f"Driver: {driver_name or 'Not assigned yet'}", S["note"]),
    ]

    bill_tbl = Table(
        [[billed_block, booking_block]],
        colWidths=[content_width * 0.5, content_width * 0.5],
    )
    bill_tbl.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    story.append(bill_tbl)
    story.append(Spacer(1, 5 * mm))

    # ── 3. ROUTE ─────────────────────────────────────────────────────────────
    story.append(HRFlowable(width="100%", thickness=0.5, color=GREY_200))
    story.append(Spacer(1, 3 * mm))
    story.append(_p("ROUTE", S["section_h"]))
    story.append(Spacer(1, 2 * mm))

    route_tbl = Table(
        [
            ["", "PICKUP", "DROP"],
            [
                "Location",
                Paragraph(pickup_location or "—", _ps("pl", 8, GREY_900)),
                Paragraph(drop_location or "—", _ps("dl", 8, GREY_900)),
            ],
        ],
        colWidths=[content_width * 0.20, content_width * 0.40, content_width * 0.40],
    )
    route_tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (1, 0), (2, 0), BRAND_DARK),
                ("TEXTCOLOR", (1, 0), (2, 0), WHITE),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, 0), 8),
                ("ALIGN", (1, 0), (2, 0), "CENTER"),
                ("FONTNAME", (0, 1), (0, -1), "Helvetica-Bold"),
                ("FONTSIZE", (0, 1), (0, -1), 8),
                ("TEXTCOLOR", (0, 1), (0, -1), GREY_600),
                ("BACKGROUND", (0, 1), (-1, 1), GREY_100),
                ("GRID", (0, 0), (-1, -1), 0.4, GREY_200),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    story.append(route_tbl)
    story.append(Spacer(1, 5 * mm))

    # ── 4. ADVANCE RECEIVED BANNER ───────────────────────────────────────────
    # The whole point of the document — amount, custody and mode in one block.
    receiver_key = (received_by or "").upper()
    receiver_label = _RECEIVER_LABELS.get(receiver_key, receiver_key or "—")
    if receiver_name:
        receiver_label = f"{receiver_label} — {receiver_name}"

    amount_block = [
        _p("ADVANCE RECEIVED", _ps("arh", 8, GREY_600, bold=True)),
        Spacer(1, 1 * mm),
        _p(f"Rs. {amount:,.2f}", _ps("arv", 22, SUCCESS, bold=True)),
        Spacer(1, 1 * mm),
        _p(_amount_in_words(amount), _ps("arw", 8, GREY_600)),
    ]

    meta_rows = [
        ["Payment Mode", (payment_mode or "—").upper()],
        ["Received By", receiver_label],
        ["Received On", rec_dt.strftime("%d %b %Y, %I:%M %p")],
    ]
    if reference_note:
        meta_rows.append(["Reference", reference_note])

    meta_tbl = Table(meta_rows, colWidths=[content_width * 0.16, content_width * 0.28])
    meta_tbl.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (0, -1), "Helvetica"),
                ("TEXTCOLOR", (0, 0), (0, -1), GREY_600),
                ("FONTNAME", (1, 0), (1, -1), "Helvetica-Bold"),
                ("TEXTCOLOR", (1, 0), (1, -1), GREY_900),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )

    banner_tbl = Table(
        [[amount_block, meta_tbl]],
        colWidths=[content_width * 0.52, content_width * 0.48],
    )
    banner_tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f0fdf4")),
                ("BOX", (0, 0), (-1, -1), 0.8, SUCCESS),
                ("LINEBEFORE", (1, 0), (1, -1), 0.5, GREY_200),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    story.append(banner_tbl)
    story.append(Spacer(1, 5 * mm))

    # ── 5. INDICATIVE BALANCE ────────────────────────────────────────────────
    # Labelled "indicative" throughout: the fare is only fixed at trip end, so
    # a customer must never read these numbers as a final bill.
    if estimated_amount is not None or balance_after_advance is not None:
        story.append(_p("INDICATIVE BALANCE", S["section_h"]))
        story.append(Spacer(1, 2 * mm))

        bal_rows: list[list] = [
            [
                Paragraph("Description", _ps("bh1", 8, WHITE, bold=True)),
                Paragraph("Amount", _ps("bh2", 8, WHITE, bold=True, align=TA_RIGHT)),
            ]
        ]
        if estimated_amount is not None:
            bal_rows.append(
                [
                    Paragraph("Estimated Fare", _ps("bi", 8.5, GREY_900)),
                    Paragraph(
                        f"Rs. {estimated_amount:,.2f}",
                        _ps("bv", 8.5, GREY_900, align=TA_RIGHT),
                    ),
                ]
            )
        bal_rows.append(
            [
                Paragraph("Advance Received (this receipt)", _ps("bi", 8.5, SUCCESS)),
                Paragraph(
                    f"− Rs. {amount:,.2f}", _ps("bv", 8.5, SUCCESS, align=TA_RIGHT)
                ),
            ]
        )
        if balance_after_advance is not None:
            bal_rows.append(
                [
                    Paragraph(
                        "Balance Payable at Trip End",
                        _ps("bl", 9.5, BRAND_GOLD, bold=True),
                    ),
                    Paragraph(
                        f"Rs. {balance_after_advance:,.2f}",
                        _ps("bvv", 10, BRAND_GOLD, bold=True, align=TA_RIGHT),
                    ),
                ]
            )

        n = len(bal_rows)
        bal_tbl = Table(
            bal_rows, colWidths=[content_width * 0.65, content_width * 0.35]
        )
        bal_tbl.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), BRAND_MAIN),
                    ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, 0), 8),
                    ("GRID", (0, 0), (-1, -1), 0.3, GREY_200),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                    ("BACKGROUND", (0, n - 1), (-1, n - 1), colors.HexColor("#fefce8")),
                    ("LINEABOVE", (0, n - 1), (-1, n - 1), 1.2, BRAND_GOLD),
                ]
            )
        )
        story.append(bal_tbl)
        story.append(Spacer(1, 2 * mm))
        story.append(
            _p(
                "Indicative only. The final fare is computed at trip end from the actual "
                "distance and waiting time, and may differ from the estimate above.",
                _ps("indic", 7, GREY_600),
            )
        )
        story.append(Spacer(1, 5 * mm))

    # ── 6. DISCLAIMER + SIGNATURES ───────────────────────────────────────────
    story.append(HRFlowable(width="100%", thickness=1, color=BRAND_GOLD))
    story.append(Spacer(1, 3 * mm))

    story.append(
        _p(
            "<b>This is an advance receipt, not a tax invoice.</b> The balance is payable "
            "at trip end, when a GST tax invoice will be issued for the full trip fare.",
            _ps("disc", 8, BRAND_DARK, align=TA_CENTER),
        )
    )
    story.append(Spacer(1, 4 * mm))

    sign_tbl = Table(
        [
            ["", ""],
            [
                Paragraph(
                    "Received By (signature)", _ps("sg1", 7, GREY_600, align=TA_CENTER)
                ),
                Paragraph(
                    "Customer (signature)", _ps("sg2", 7, GREY_600, align=TA_CENTER)
                ),
            ],
        ],
        colWidths=[content_width * 0.5, content_width * 0.5],
        rowHeights=[12 * mm, None],
    )
    sign_tbl.setStyle(
        TableStyle(
            [
                ("LINEBELOW", (0, 0), (0, 0), 0.5, GREY_600),
                ("LINEBELOW", (1, 0), (1, 0), 0.5, GREY_600),
                ("LEFTPADDING", (0, 0), (-1, -1), 14),
                ("RIGHTPADDING", (0, 0), (-1, -1), 14),
                ("TOPPADDING", (0, 1), (-1, 1), 2),
            ]
        )
    )
    story.append(sign_tbl)
    story.append(Spacer(1, 4 * mm))

    story.append(
        _p(
            f"Thank you for choosing <b>{platform_name}</b>. This is a computer-generated "
            "receipt; retain it until the trip is invoiced.",
            _ps("ty", 7.5, GREY_600, align=TA_CENTER),
        )
    )
    if support_email or support_phone:
        contact = "  |  ".join(filter(None, [support_email, support_phone]))
        story.append(Spacer(1, 1.5 * mm))
        story.append(
            _p(contact, _ps("ct", 7.5, BRAND_MAIN, bold=True, align=TA_CENTER))
        )

    doc.build(story)
    return buf.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
# HOTEL INVOICE GENERATOR
#
# Sibling to generate_invoice_pdf above — reuses the same palette, helpers
# (_ps, _p, _fetch_logo_bytes), header/footer frames, and branding config,
# but presents hotel facts: property name, room category, check-in/out dates,
# nights, rooms, guests, room charge, overtime, extras, discount, GST, advances,
# and balance.
#
# Doc Ref: BRD Part 4 §57-92 — Hotel Booking Lifecycle
#          BRD Part 3 §45 — Advance collection & settlement custody
#          Migration 0035 — hotel_payments (invoice_number, overtime, advances)
# ─────────────────────────────────────────────────────────────────────────────


def generate_hotel_invoice_pdf(
    *,
    # Invoice meta
    invoice_number: str,
    hotel_booking_number: str,
    booking_number: str,
    invoice_date: datetime | None = None,
    # Customer
    customer_name: str | None,
    customer_mobile: str | None,
    # Hotel / Stay details
    hotel_name: str | None,
    hotel_address: str | None,
    room_category_name: str | None,
    room_type: str | None,
    meal_plan: str | None,
    check_in_date: str | None,  # ISO date str or None
    check_out_date: str | None,  # ISO date str or None
    actual_check_in_at: datetime | None,
    actual_check_out_at: datetime | None,
    num_nights: int | None,
    num_rooms: int | None,
    num_guests: int | None,
    # Billing — amounts in float
    room_charge: float,
    overtime_charge: float,
    extra_charges: float,
    discount_amount: float,
    coupon_code: str | None,
    taxable_amount: float,
    gst_percent: float,
    gst_amount: float,
    is_tax_invoice: bool,
    grand_total: float,
    advance_paid: float,
    payment_mode: str | None,
    payment_collected_by: str | None,
    # Occupancy surcharge breakdown (rate_snapshot["occupancy"]); when present the
    # single Room Charge row is split into tariff + extra adult/child/bed rows.
    occupancy: dict | None = None,
    # Platform branding (from system_configurations)
    platform_name: str = "WayTero",
    platform_logo_url: str = "",
    business_legal_name: str = "",
    business_gst_number: str = "",
    business_registered_address: str = "",
    support_email: str = "",
    support_phone: str = "",
) -> bytes:
    """
    Build and return a premium A4 hotel stay invoice PDF as raw bytes.
    """

    buf = io.BytesIO()
    W, H = A4

    # ── Page setup ───────────────────────────────────────────────────────────
    MARGIN = 18 * mm
    doc = BaseDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=12 * mm,
        bottomMargin=14 * mm,
        title=f"Hotel Invoice {invoice_number}",
        author=platform_name,
    )

    content_width = W - 2 * MARGIN

    # ── Page background & footer callback ────────────────────────────────────
    inv_dt = invoice_date or datetime.now(timezone.utc)
    inv_dt_str = inv_dt.strftime("%d %b %Y, %I:%M %p") + " IST"

    def _draw_page(canvas, doc):
        canvas.saveState()
        # Header band
        canvas.setFillColor(BRAND_DARK)
        canvas.rect(0, H - 22 * mm, W, 22 * mm, fill=1, stroke=0)
        canvas.setFillColor(BRAND_ACCENT)
        canvas.rect(0, H - 23.2 * mm, W, 1.2 * mm, fill=1, stroke=0)
        # Footer band
        footer_h = 10 * mm
        canvas.setFillColor(BRAND_DARK)
        canvas.rect(0, 0, W, footer_h, fill=1, stroke=0)
        canvas.setFillColor(colors.HexColor("#94a3b8"))
        canvas.setFont("Helvetica", 7)
        footer_y = 3.5 * mm
        canvas.drawString(
            MARGIN, footer_y, f"Generated on {inv_dt_str}  |  {invoice_number}"
        )
        canvas.drawRightString(
            W - MARGIN,
            footer_y,
            f"{platform_name}  |  {support_email}  |  {support_phone}",
        )
        canvas.setFillColor(colors.HexColor("#64748b"))
        canvas.setFont("Helvetica", 6.5)
        canvas.drawCentredString(W / 2, footer_y, f"Page {doc.page}")
        canvas.restoreState()

    frame = Frame(
        MARGIN,
        14 * mm,
        content_width,
        H - 22 * mm - 14 * mm - 4 * mm,
        showBoundary=0,
    )
    doc.addPageTemplates([PageTemplate(id="main", frames=[frame], onPage=_draw_page)])

    # ── Styles ────────────────────────────────────────────────────────────────
    S = {
        "section_h": _ps("sec_h", 9, BRAND_MAIN, bold=True, space_after=4),
        "label": _ps("lbl", 7.5, GREY_600),
        "value": _ps("val", 9, GREY_900, bold=True),
        "value_sm": _ps("val_sm", 8, GREY_900),
        "gst_note": _ps("gst", 7.5, GREY_600),
    }

    story: list[Any] = []

    # ── 1. HEADER SPACER ──────────────────────────────────────────────────────
    story.append(Spacer(1, 22 * mm + 1.2 * mm + 3 * mm))

    # ── 2. LOGO + PLATFORM NAME row ──────────────────────────────────────────
    logo_img = None
    logo_bytes = _fetch_logo_bytes(platform_logo_url)
    if logo_bytes:
        try:
            logo_img = Image(
                io.BytesIO(logo_bytes),
                width=28 * mm,
                height=10 * mm,
                kind="proportional",
            )
        except Exception:
            logo_img = None

    header_left_content: list[Any] = []
    if logo_img:
        header_left_content.append(logo_img)
        header_left_content.append(Spacer(1, 2 * mm))
    header_left_content.append(
        _p(f"<b>{platform_name}</b>", _ps("hn", 14, BRAND_MAIN, bold=True))
    )
    if business_legal_name:
        header_left_content.append(_p(business_legal_name, _ps("bln", 7.5, GREY_600)))
    if business_gst_number:
        header_left_content.append(
            _p(f"GSTIN: <b>{business_gst_number}</b>", _ps("gst2", 7.5, GREY_600))
        )
    if business_registered_address:
        header_left_content.append(
            _p(business_registered_address, _ps("adr", 7, GREY_600))
        )

    header_right_content = [
        _p("HOTEL INVOICE", _ps("INVTITLE", 22, BRAND_DARK, bold=True, align=TA_RIGHT)),
        Spacer(1, 1 * mm),
        _p(invoice_number, _ps("invn", 10, BRAND_MAIN, bold=True, align=TA_RIGHT)),
        Spacer(1, 1 * mm),
        _p(
            f"Date: {inv_dt.strftime('%d %B %Y')}",
            _ps("invdate", 8, GREY_600, align=TA_RIGHT),
        ),
    ]

    header_tbl = Table(
        [[header_left_content, header_right_content]],
        colWidths=[content_width * 0.55, content_width * 0.45],
    )
    header_tbl.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    story.append(header_tbl)
    story.append(Spacer(1, 5 * mm))
    story.append(HRFlowable(width="100%", thickness=1, color=GREY_200))
    story.append(Spacer(1, 4 * mm))

    # ── 3. BILLED TO / STAY INFO row ─────────────────────────────────────────
    balance_due = max(0.0, round(grand_total - advance_paid, 2))

    billed_block = [
        _p("BILLED TO", S["section_h"]),
        _p(customer_name or "—", S["value"]),
        _p(customer_mobile or "", S["value_sm"]),
        Spacer(1, 3 * mm),
        _p("BOOKING REFERENCE", S["section_h"]),
        _p(f"Hotel Booking: <b>{hotel_booking_number}</b>", S["value_sm"]),
        _p(f"Master Booking: {booking_number}", S["gst_note"]),
    ]

    payment_status_label = "PAID" if balance_due <= 0 else "PARTIALLY PAID"
    stay_block = [
        _p("STAY SUMMARY", S["section_h"]),
        _p(f"Property: <b>{hotel_name or '—'}</b>", S["value_sm"]),
        _p(f"Room: {room_category_name or '—'} ({room_type or '—'})", S["gst_note"]),
        _p(f"Meal Plan: {meal_plan or 'N/A'}", S["gst_note"]),
        _p(
            f"Nights: {num_nights or 0} | Rooms: {num_rooms or 0} | Guests: {num_guests or 0}",
            S["gst_note"],
        ),
        Spacer(1, 3 * mm),
        _p("PAYMENT STATUS", S["section_h"]),
        _p(
            f"<b>{payment_status_label}</b>",
            _ps("psl", 9, SUCCESS if balance_due <= 0 else BRAND_GOLD, bold=True),
        ),
        _p(
            f"Mode: {payment_mode or '—'} | Collected by: {(payment_collected_by or '—').title()}",
            S["gst_note"],
        ),
    ]

    bill_tbl = Table(
        [[billed_block, stay_block]],
        colWidths=[content_width * 0.5, content_width * 0.5],
    )
    bill_tbl.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    story.append(bill_tbl)
    story.append(Spacer(1, 5 * mm))

    # ── 4. CHECK-IN / CHECK-OUT DATES SECTION ─────────────────────────────────
    story.append(HRFlowable(width="100%", thickness=0.5, color=GREY_200))
    story.append(Spacer(1, 3 * mm))
    story.append(_p("STAY DETAILS", S["section_h"]))
    story.append(Spacer(1, 2 * mm))

    def _fmt_dt(dt: datetime | None) -> str:
        if not dt:
            return "—"
        return dt.strftime("%d %b %Y, %I:%M %p")

    dates_data = [
        ["", "CHECK-IN", "CHECK-OUT"],
        ["Scheduled Date", check_in_date or "—", check_out_date or "—"],
        [
            "Actual Date / Time",
            _fmt_dt(actual_check_in_at),
            _fmt_dt(actual_check_out_at),
        ],
    ]
    dates_tbl = Table(
        dates_data,
        colWidths=[content_width * 0.25, content_width * 0.375, content_width * 0.375],
    )
    dates_tbl.setStyle(
        TableStyle(
            [
                # Header row
                ("BACKGROUND", (1, 0), (2, 0), BRAND_DARK),
                ("TEXTCOLOR", (1, 0), (2, 0), WHITE),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, 0), 8),
                ("ALIGN", (1, 0), (2, 0), "CENTER"),
                # Body
                ("FONTNAME", (0, 1), (0, -1), "Helvetica-Bold"),
                ("FONTSIZE", (0, 1), (0, -1), 8),
                ("TEXTCOLOR", (0, 1), (0, -1), GREY_600),
                ("FONTNAME", (1, 1), (-1, -1), "Helvetica"),
                ("FONTSIZE", (1, 1), (-1, -1), 8.5),
                ("TEXTCOLOR", (1, 1), (-1, -1), GREY_900),
                ("BACKGROUND", (0, 1), (-1, 1), GREY_100),
                ("GRID", (0, 0), (-1, -1), 0.4, GREY_200),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    story.append(dates_tbl)
    story.append(Spacer(1, 5 * mm))

    # ── 5. CHARGES BREAKDOWN TABLE ────────────────────────────────────────────
    story.append(HRFlowable(width="100%", thickness=0.5, color=GREY_200))
    story.append(Spacer(1, 3 * mm))
    story.append(_p("CHARGES BREAKDOWN", S["section_h"]))
    story.append(Spacer(1, 2 * mm))

    charges_rows: list[list] = [
        [
            Paragraph("Description", _ps("ch1", 8, WHITE, bold=True)),
            Paragraph("Amount", _ps("ch2", 8, WHITE, bold=True, align=TA_RIGHT)),
        ],
    ]

    # Room charge, itemised when an occupancy surcharge was frozen at booking.
    # The tariff + extra rows sum back to room_charge, so subtotal math below is
    # unchanged; without a surcharge it's the same single "Room Charge" row.
    occ = occupancy or {}
    surcharge = float(occ.get("surcharge", 0) or 0)
    if surcharge > 0:
        nights = int(num_nights or 1)
        room_tariff = room_charge - surcharge
        charges_rows.append(
            [
                Paragraph("Room Tariff", _ps("ci", 8.5, GREY_900)),
                Paragraph(
                    f"Rs. {room_tariff:,.2f}", _ps("cv", 8.5, GREY_900, align=TA_RIGHT)
                ),
            ]
        )
        extra_adults = int(occ.get("extra_adults", 0) or 0)
        extra_children = int(occ.get("extra_children", 0) or 0)
        extra_beds = int(occ.get("extra_beds", 0) or 0)
        adult_rate = float(occ.get("extra_adult_charge", 0) or 0)
        child_rate = float(occ.get("extra_child_charge", 0) or 0)
        bed_rate = float(occ.get("extra_bed_charge", 0) or 0)
        if extra_adults > 0 and adult_rate > 0:
            charges_rows.append(
                [
                    Paragraph(
                        f"Extra Adult x {extra_adults} "
                        f"(Rs. {adult_rate:,.0f}/night x {nights})",
                        _ps("ci", 8.5, GREY_900),
                    ),
                    Paragraph(
                        f"Rs. {adult_rate * extra_adults * nights:,.2f}",
                        _ps("cv", 8.5, GREY_900, align=TA_RIGHT),
                    ),
                ]
            )
        if extra_children > 0 and child_rate > 0:
            charges_rows.append(
                [
                    Paragraph(
                        f"Extra Child x {extra_children} "
                        f"(Rs. {child_rate:,.0f}/night x {nights})",
                        _ps("ci", 8.5, GREY_900),
                    ),
                    Paragraph(
                        f"Rs. {child_rate * extra_children * nights:,.2f}",
                        _ps("cv", 8.5, GREY_900, align=TA_RIGHT),
                    ),
                ]
            )
        if extra_beds > 0 and bed_rate > 0:
            charges_rows.append(
                [
                    Paragraph(
                        f"Extra Bed x {extra_beds} (Rs. {bed_rate:,.0f} one-time)",
                        _ps("ci", 8.5, GREY_900),
                    ),
                    Paragraph(
                        f"Rs. {bed_rate * extra_beds:,.2f}",
                        _ps("cv", 8.5, GREY_900, align=TA_RIGHT),
                    ),
                ]
            )
    else:
        charges_rows.append(
            [
                Paragraph("Room Charge", _ps("ci", 8.5, GREY_900)),
                Paragraph(
                    f"Rs. {room_charge:,.2f}", _ps("cv", 8.5, GREY_900, align=TA_RIGHT)
                ),
            ]
        )
    if overtime_charge > 0:
        charges_rows.append(
            [
                Paragraph("Late Check-out / Overtime", _ps("ci", 8.5, GREY_900)),
                Paragraph(
                    f"Rs. {overtime_charge:,.2f}",
                    _ps("cv", 8.5, GREY_900, align=TA_RIGHT),
                ),
            ]
        )
    if extra_charges > 0:
        charges_rows.append(
            [
                Paragraph(
                    "Additional Charges (extras, laundry, etc.)",
                    _ps("ci", 8.5, GREY_900),
                ),
                Paragraph(
                    f"Rs. {extra_charges:,.2f}",
                    _ps("cv", 8.5, GREY_900, align=TA_RIGHT),
                ),
            ]
        )
    if discount_amount > 0:
        desc_txt = f"Coupon / Discount ({coupon_code})" if coupon_code else "Discount"
        charges_rows.append(
            [
                Paragraph(desc_txt, _ps("ci", 8.5, SUCCESS)),
                Paragraph(
                    f"− Rs. {discount_amount:,.2f}",
                    _ps("cv", 8.5, SUCCESS, align=TA_RIGHT),
                ),
            ]
        )
    if advance_paid > 0:
        charges_rows.append(
            [
                Paragraph("Advance Paid", _ps("ci", 8.5, GREY_600)),
                Paragraph(
                    f"− Rs. {advance_paid:,.2f}",
                    _ps("cv", 8.5, GREY_600, align=TA_RIGHT),
                ),
            ]
        )

    # Subtotal after discount
    subtotal = room_charge + overtime_charge + extra_charges - discount_amount
    if discount_amount > 0:
        charges_rows.append(
            [
                Paragraph("Subtotal (after discount)", _ps("ci", 8, GREY_600)),
                Paragraph(
                    f"Rs. {subtotal:,.2f}", _ps("cv", 8, GREY_600, align=TA_RIGHT)
                ),
            ]
        )

    # GST row — only when this is a tax invoice
    if is_tax_invoice and gst_amount > 0:
        charges_rows.append(
            [
                Paragraph(f"GST ({gst_percent:.0f}%)", _ps("gst_lbl", 8.5, GREY_600)),
                Paragraph(
                    f"Rs. {gst_amount:,.2f}",
                    _ps("gst_val", 8.5, GREY_600, align=TA_RIGHT),
                ),
            ]
        )

    # Blank separator
    charges_rows.append(["", ""])

    # Grand total row
    charges_rows.append(
        [
            Paragraph("TOTAL PAYABLE", _ps("gtl", 10, BRAND_DARK, bold=True)),
            Paragraph(
                f"Rs. {grand_total:,.2f}",
                _ps("gtv", 11, BRAND_DARK, bold=True, align=TA_RIGHT),
            ),
        ]
    )

    # Balance due
    if balance_due > 0:
        charges_rows.append(
            [
                Paragraph("Balance Due", _ps("bdl", 9, BRAND_GOLD, bold=True)),
                Paragraph(
                    f"Rs. {balance_due:,.2f}",
                    _ps("bdv", 9, BRAND_GOLD, bold=True, align=TA_RIGHT),
                ),
            ]
        )
    else:
        charges_rows.append(
            [
                Paragraph("✔  Fully Paid", _ps("fpl", 9, SUCCESS, bold=True)),
                Paragraph(
                    "Rs. 0.00", _ps("fpv", 9, SUCCESS, bold=True, align=TA_RIGHT)
                ),
            ]
        )

    n_rows = len(charges_rows)
    charges_tbl = Table(
        charges_rows,
        colWidths=[content_width * 0.65, content_width * 0.35],
    )
    charges_style = [
        # Header
        ("BACKGROUND", (0, 0), (-1, 0), BRAND_MAIN),
        ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 8),
        ("GRID", (0, 0), (-1, -1), 0.3, GREY_200),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        # Grand total row highlight
        ("BACKGROUND", (0, n_rows - 2), (-1, n_rows - 2), colors.HexColor("#e0f2fe")),
        ("LINEABOVE", (0, n_rows - 2), (-1, n_rows - 2), 1.2, BRAND_MAIN),
        # Final (balance/paid) row
        (
            "BACKGROUND",
            (0, n_rows - 1),
            (-1, n_rows - 1),
            (
                colors.HexColor("#dcfce7")
                if balance_due <= 0
                else colors.HexColor("#fefce8")
            ),
        ),
        (
            "LINEBELOW",
            (0, n_rows - 1),
            (-1, n_rows - 1),
            1.5,
            SUCCESS if balance_due <= 0 else BRAND_GOLD,
        ),
    ]
    # Shade every other body row
    for i in range(1, n_rows - 2):
        if i % 2 == 0:
            charges_style.append(("BACKGROUND", (0, i), (-1, i), GREY_100))

    charges_tbl.setStyle(TableStyle(charges_style))
    story.append(charges_tbl)
    story.append(Spacer(1, 6 * mm))

    # ── 6. THANK YOU + TERMS ──────────────────────────────────────────────────
    story.append(HRFlowable(width="100%", thickness=1, color=BRAND_ACCENT))
    story.append(Spacer(1, 4 * mm))

    thank_you_block = [
        _p(
            f"Thank you for choosing <b>{platform_name}</b>!",
            _ps("ty", 10, BRAND_DARK, bold=True, align=TA_CENTER),
        ),
        Spacer(1, 1.5 * mm),
        _p(
            "This is a computer-generated invoice and does not require a physical signature. "
            "For queries, contact our support team.",
            _ps("tyb", 7.5, GREY_600, align=TA_CENTER),
        ),
    ]
    if support_email or support_phone:
        contact = "  |  ".join(filter(None, [support_email, support_phone]))
        thank_you_block.append(
            _p(f"✉ {contact}", _ps("ct", 7.5, BRAND_MAIN, bold=True, align=TA_CENTER))
        )
    if is_tax_invoice and gst_amount > 0:
        thank_you_block.append(Spacer(1, 1.5 * mm))
        thank_you_block.append(
            _p(
                f"This is a <b>Tax Invoice</b> | GST @ {gst_percent:.0f}%: Rs. {gst_amount:,.2f} included.",
                _ps("taxnote", 7.5, BRAND_MAIN, align=TA_CENTER),
            )
        )
    if business_gst_number:
        thank_you_block.append(Spacer(1, 1.5 * mm))
        thank_you_block.append(
            _p(
                f"Platform GSTIN: <b>{business_gst_number}</b>",
                _ps("gstf", 7, GREY_600, align=TA_CENTER),
            )
        )
    if not is_tax_invoice:
        thank_you_block.append(Spacer(1, 1.5 * mm))
        thank_you_block.append(
            _p(
                "Non-tax receipt — GST not applicable on this booking.",
                _ps("nontax", 7, GREY_600, align=TA_CENTER),
            )
        )
    if hotel_address:
        thank_you_block.append(Spacer(1, 1.5 * mm))
        thank_you_block.append(
            _p(f"Property: {hotel_address}", _ps("haddr", 7, GREY_600, align=TA_CENTER))
        )

    for item in thank_you_block:
        story.append(item)

    # ── BUILD ──────────────────────────────────────────────────────────────────
    doc.build(story)
    return buf.getvalue()
