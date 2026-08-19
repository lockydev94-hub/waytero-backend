# ============================================================
# WAYTERO — ONBOARDING GUIDE + FORM PDF SERVICE
# File: app/modules/admin/onboarding_pdf_service.py
#
# Purpose:
#   Generate print-quality A4 PDFs for the partner onboarding
#   programme using ReportLab. Produces two families of documents:
#
#   1. GUIDES  — "what you need to prepare" documents:
#        - Partner Registration guide
#        - Driver Onboarding guide
#        - Vehicle Onboarding guide
#   2. FORMS   — printable, hand-fillable registration forms that an
#        admin or field agent carries, fills by hand, then uses to
#        register the partner / vehicle / driver in the portal:
#        - Partner Registration form
#        - Vehicle Registration form (takes Partner ID as reference)
#        - Driver Registration form
#
#   Documents follow the same design language as the invoice PDFs
#   (invoice_pdf_service.py): dark navy header band with platform
#   branding pulled from system_configurations at render time, accent
#   stripe, and footer band with support contact.
#
# Config keys read from system_configurations:
#   PLATFORM_NAME, PLATFORM_LOGO_URL, BUSINESS_LEGAL_NAME,
#   BUSINESS_GST_NUMBER, BUSINESS_REGISTERED_ADDRESS,
#   SUPPORT_EMAIL, SUPPORT_PHONE
#
# Doc Ref:
#   Docs/22_Partner_Onboarding_Guides/ (source content for the guides)
#   BRD Part 2 §17-22 — Partner KYC & onboarding
#   BRD Part 3 §133-138 — Vehicle onboarding
#   DB Schema Part 1 §12 — system_configurations
# ============================================================

from __future__ import annotations

import io
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
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

# ── Palette (mirrors invoice_pdf_service.py) ────────────────────────────────
BRAND_DARK = colors.HexColor("#0d1b2a")
BRAND_MAIN = colors.HexColor("#1565c0")
BRAND_ACCENT = colors.HexColor("#00b4d8")
BRAND_GOLD = colors.HexColor("#f59e0b")
GREY_100 = colors.HexColor("#f8fafc")
GREY_200 = colors.HexColor("#e2e8f0")
GREY_600 = colors.HexColor("#475569")
GREY_900 = colors.HexColor("#0f172a")
SUCCESS = colors.HexColor("#16a34a")
WARN = colors.HexColor("#d97706")
WHITE = colors.white

_BRANDING_KEYS = (
    "PLATFORM_NAME",
    "PLATFORM_LOGO_URL",
    "BUSINESS_LEGAL_NAME",
    "BUSINESS_GST_NUMBER",
    "BUSINESS_REGISTERED_ADDRESS",
    "SUPPORT_EMAIL",
    "SUPPORT_PHONE",
)


# ── Logo fetch (fail gracefully) ─────────────────────────────────────────────
def _fetch_logo_bytes(url: str) -> bytes | None:
    if not url or not url.startswith("http"):
        return None
    try:
        import httpx

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


# ── Base document (shared page template) ─────────────────────────────────────
# Reuses the invoice layout: dark header band + accent stripe on top, footer
# band on the bottom, platform branding in the header block.


def _build_document(
    cfg: dict[str, str],
    doc_title: str,
    header_title: str,
    header_subtitle: str,
    build_story: Callable[[float, dict[str, str]], list[Any]],
) -> bytes:
    platform_name = cfg.get("PLATFORM_NAME") or "WayTero"
    support_email = cfg.get("SUPPORT_EMAIL") or ""
    support_phone = cfg.get("SUPPORT_PHONE") or ""

    buf = io.BytesIO()
    W, H = A4  # 595.28 x 841.89 pts
    MARGIN = 18 * mm
    doc = BaseDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=12 * mm,
        bottomMargin=14 * mm,
        title=doc_title,
        author=platform_name,
    )
    content_width = W - 2 * MARGIN

    gen_dt = datetime.now(timezone.utc)
    gen_dt_str = gen_dt.strftime("%d %b %Y, %I:%M %p") + " IST"

    def _draw_page(canvas, doc_obj):
        canvas.saveState()
        # Header band — deep navy full width
        canvas.setFillColor(BRAND_DARK)
        canvas.rect(0, H - 22 * mm, W, 22 * mm, fill=1, stroke=0)
        # Accent stripe below header
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
            MARGIN, footer_y, f"Generated on {gen_dt_str}  |  {doc_title}"
        )
        canvas.drawRightString(
            W - MARGIN,
            footer_y,
            f"{platform_name}  |  {support_email}  |  {support_phone}",
        )
        canvas.setFillColor(colors.HexColor("#64748b"))
        canvas.setFont("Helvetica", 6.5)
        canvas.drawCentredString(W / 2, footer_y, f"Page {doc_obj.page}")
        canvas.restoreState()

    frame = Frame(
        MARGIN,
        14 * mm,
        content_width,
        H - 22 * mm - 14 * mm - 4 * mm,
        showBoundary=0,
    )
    doc.addPageTemplates([PageTemplate(id="main", frames=[frame], onPage=_draw_page)])

    story = _header_block(cfg, content_width, header_title, header_subtitle)
    story.extend(build_story(content_width))

    doc.build(story)
    return buf.getvalue()


def _header_block(
    cfg: dict[str, str], content_width: float, header_title: str, header_subtitle: str
) -> list[Any]:
    """2-col header that sits inside the dark band: branding left, title right."""
    platform_name = cfg.get("PLATFORM_NAME") or "WayTero"
    story: list[Any] = [Spacer(1, 22 * mm + 1.2 * mm + 3 * mm)]

    logo_img = None
    logo_bytes = _fetch_logo_bytes(cfg.get("PLATFORM_LOGO_URL") or "")
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

    left: list[Any] = []
    if logo_img:
        left.append(logo_img)
        left.append(Spacer(1, 2 * mm))
    left.append(_p(f"<b>{platform_name}</b>", _ps("hn", 14, BRAND_MAIN, bold=True)))
    if cfg.get("BUSINESS_LEGAL_NAME"):
        left.append(_p(cfg["BUSINESS_LEGAL_NAME"], _ps("bln", 7.5, GREY_600)))
    if cfg.get("BUSINESS_GST_NUMBER"):
        left.append(
            _p(
                f"GSTIN: <b>{cfg['BUSINESS_GST_NUMBER']}</b>",
                _ps("gst2", 7.5, GREY_600),
            )
        )
    if cfg.get("BUSINESS_REGISTERED_ADDRESS"):
        left.append(_p(cfg["BUSINESS_REGISTERED_ADDRESS"], _ps("adr", 7, GREY_600)))

    right = [
        _p(header_title, _ps("TITLE", 20, BRAND_DARK, bold=True, align=TA_RIGHT)),
        Spacer(1, 1 * mm),
        _p(header_subtitle, _ps("sub", 8, GREY_600, align=TA_RIGHT)),
        Spacer(1, 1 * mm),
        _p(
            f"Prepared: {datetime.now(timezone.utc).strftime('%d %B %Y')}",
            _ps("date", 7.5, GREY_600, align=TA_RIGHT),
        ),
    ]

    tbl = Table(
        [[left, right]],
        colWidths=[content_width * 0.55, content_width * 0.45],
    )
    tbl.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    story.append(tbl)
    story.append(Spacer(1, 5 * mm))
    story.append(HRFlowable(width="100%", thickness=1, color=GREY_200))
    story.append(Spacer(1, 4 * mm))
    return story


# ── Reusable story helpers ────────────────────────────────────────────────────
def _section_heading(text: str) -> Paragraph:
    return _p(text, _ps("sec_h", 10, BRAND_MAIN, bold=True, space_after=4))


def _note(text: str, color=GREY_600) -> Paragraph:
    return _p(text, _ps("note", 7.5, color))


def _bullet(text: str) -> Paragraph:
    return _p(f"• {text}", _ps("bul", 8.5, GREY_900))


def _bullets(items: Iterable[str]) -> list[Paragraph]:
    return [_bullet(i) for i in items]


def _data_table(
    rows: list[list[str]],
    col_widths: list[float],
    header_bg=BRAND_DARK,
    zebra=False,
) -> Table:
    """Render rows of strings as a table; first row becomes the header."""
    data = [
        [
            _p(
                c,
                _ps(
                    f"c{r}{cidx}",
                    8,
                    WHITE if r == 0 else GREY_900,
                    bold=(r == 0 or cidx == 0),
                ),
            )
            for cidx, c in enumerate(row)
        ]
        for r, row in enumerate(rows)
    ]
    tbl = Table(data, colWidths=col_widths, repeatRows=1)
    style: list[tuple] = [
        ("GRID", (0, 0), (-1, -1), 0.5, GREY_200),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("BACKGROUND", (0, 0), (-1, 0), header_bg),
    ]
    if zebra:
        for i in range(1, len(rows)):
            if i % 2 == 0:
                style.append(("BACKGROUND", (0, i), (-1, i), GREY_100))
    tbl.setStyle(TableStyle(style))
    return tbl


def _field_row(
    label: str,
    value_placeholder: str = "",
    total_width: float = 493,
    label_frac: float = 0.38,
) -> Table:
    """A single labelled blank line for hand-filling on a form."""
    tbl = Table(
        [
            [
                _p(label, _ps("flbl", 8.5, GREY_900, bold=True)),
                _p(value_placeholder or " ", _ps("fval", 8.5, GREY_600)),
            ]
        ],
        colWidths=[total_width * label_frac, total_width * (1 - label_frac)],
    )
    tbl.setStyle(
        TableStyle(
            [
                ("LINEBELOW", (1, 0), (1, 0), 0.6, GREY_600),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    return tbl


def _checkbox_row(
    label: str, total_width: float = 493, box_frac: float = 0.05
) -> Table:
    # Box is a drawn border cell (Helvetica lacks the ☐ glyph), label sits beside it.
    box = Table([[""]], colWidths=[10], rowHeights=[10])
    box.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.8, GREY_600),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    tbl = Table(
        [
            [
                box,
                _p(label, _ps("cbl", 8.5, GREY_900)),
            ]
        ],
        colWidths=[total_width * box_frac, total_width * (1 - box_frac)],
    )
    tbl.setStyle(
        TableStyle(
            [
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    return tbl


def _signature_block(content_width: float = 493) -> list[Any]:
    story: list[Any] = [
        Spacer(1, 6 * mm),
        HRFlowable(width="100%", thickness=0.8, color=GREY_600),
        Spacer(1, 1 * mm),
        _p(
            "Declaration — I confirm the information above is true and correct, "
            "and I authorise WayTero to verify the submitted documents.",
            _ps("decl", 8, GREY_600),
        ),
        Spacer(1, 8 * mm),
        Table(
            [
                [
                    _p("Signatory Name:", _ps("sn", 8.5, GREY_900, bold=True)),
                    _p("Signature:", _ps("sgs", 8.5, GREY_900, bold=True)),
                    _p("Date:", _ps("sdt", 8.5, GREY_900, bold=True)),
                ]
            ],
            colWidths=[
                content_width * 0.30,
                content_width * 0.45,
                content_width * 0.25,
            ],
        ),
    ]
    return story


# ── GUIDE CONTENT ─────────────────────────────────────────────────────────────
_GUIDE_PARTNER = "partner"
_GUIDE_DRIVER = "driver"
_GUIDE_VEHICLE = "vehicle"
GUIDE_TYPES = (_GUIDE_PARTNER, _GUIDE_DRIVER, _GUIDE_VEHICLE)

_FORM_PARTNER = "partner"
_FORM_VEHICLE = "vehicle"
_FORM_DRIVER = "driver"
FORM_TYPES = (_FORM_PARTNER, _FORM_VEHICLE, _FORM_DRIVER)


def _partner_guide_story(content_width: float) -> list[Any]:
    s: list[Any] = [
        _note(
            "Who this is for: Anyone who wants to become a WayTero partner "
            "(cab / hotel / tour service provider). What this covers: the exact "
            "information you must enter and the documents you must upload to "
            "complete partner registration and get approved."
        ),
        Spacer(1, 4 * mm),
        _section_heading("1. Partner Types"),
        _note(
            "Before registering, decide which type of partner you are. The type "
            "determines which documents are mandatory."
        ),
        _data_table(
            [
                ["Partner Type", "Who it is for", "Tax requirement"],
                [
                    "INDIVIDUAL",
                    "Solo operator (cab operator, travel agent, tour operator)",
                    "GST / PAN optional",
                ],
                [
                    "COMPANY",
                    "Registered business entity (travel company, hotel company, fleet company)",
                    "GST + PAN mandatory",
                ],
            ],
            [content_width * 0.22, content_width * 0.48, content_width * 0.30],
        ),
        Spacer(1, 4 * mm),
        _section_heading("2. Information You Must Provide (Registration Form)"),
        _note("2.1 Identity & Contact"),
        _data_table(
            [
                ["Field", "Required?", "Rules / Format"],
                ["Partner Type", "Yes", "INDIVIDUAL or COMPANY only"],
                ["Owner / Contact Name", "Yes", "2–255 characters, full legal name"],
                [
                    "Business / Trade Name",
                    "No",
                    "Max 255 characters (usually company name)",
                ],
                [
                    "Mobile Number",
                    "Yes",
                    "10-digit Indian mobile, must start with 6–9 (e.g. 9876543210)",
                ],
                [
                    "Email Address",
                    "No",
                    "Valid email format. Used for login + notifications.",
                ],
            ],
            [content_width * 0.28, content_width * 0.14, content_width * 0.58],
        ),
        Spacer(1, 3 * mm),
        _note("2.2 Location & Business"),
        _data_table(
            [
                ["Field", "Required?", "Rules / Format"],
                ["Operating City", "Yes", "Select one city from the active city list"],
                ["Office Address Line 1", "No", "Max 255 chars"],
                ["Office Address Line 2", "No", "Max 255 chars"],
                ["Office City", "No", "Must match the Operating City"],
                ["Office Postal Code", "No", "6-digit PIN"],
                ["Partner Logo", "No", "PNG/JPG, max 2 MB"],
            ],
            [content_width * 0.28, content_width * 0.14, content_width * 0.58],
        ),
        Spacer(1, 3 * mm),
        _note("2.3 Tax Information (only shown based on type)"),
        _data_table(
            [
                ["Field", "INDIVIDUAL", "COMPANY"],
                [
                    "GST Number",
                    "Optional",
                    "Required — 15-char GSTIN, format 22AAAAA0000A1Z5",
                ],
                ["PAN Number", "Optional", "Required — 10-char format ABCDE1234F"],
                ["GST Legal Name", "—", "Name exactly as on GST certificate"],
            ],
            [content_width * 0.28, content_width * 0.30, content_width * 0.42],
        ),
        Spacer(1, 3 * mm),
        _note("2.4 Services — choose which services this partner provides:"),
        *_bullets(
            [
                "CAB — Cab rentals, airport transfers, outstation trips",
                "HOTEL — Hotel bookings and accommodation",
                "TOUR — Tour packages, sightseeing, itineraries",
            ]
        ),
        Spacer(1, 4 * mm),
        _section_heading("3. Bank Account (For Settlements)"),
        _note(
            "Settlement payments are made to your linked bank account. The account "
            "must be verified before payouts."
        ),
        _data_table(
            [
                ["Field", "Required?", "Rules / Format"],
                ["Account Holder Name", "Yes", "Max 255 chars"],
                ["Account Number", "Yes", "Stored encrypted"],
                ["IFSC Code", "Yes", "Max 20 chars"],
                ["Bank Name", "Yes", "Max 255 chars"],
                ["Branch Name", "No", ""],
                ["Account Type", "No", "SAVINGS (default) or CURRENT"],
                [
                    "Set as Primary",
                    "No",
                    "Choose the account that receives settlements",
                ],
            ],
            [content_width * 0.28, content_width * 0.14, content_width * 0.58],
        ),
        Spacer(1, 4 * mm),
        _section_heading("4. Documents You Must Upload (KYC)"),
        _note("4.1 Required documents by partner type"),
        _data_table(
            [
                ["Document", "INDIVIDUAL", "COMPANY"],
                ["Aadhaar Card", "Required", "—"],
                ["PAN Card", "Required", "Required"],
                ["GST Certificate", "—", "Required"],
                ["Company / Business Registration", "—", "Required"],
                ["Bank Proof / Cancelled Cheque", "Required", "Required"],
                ["Signed Agreement", "As advised", "As advised"],
            ],
            [content_width * 0.40, content_width * 0.30, content_width * 0.30],
        ),
        Spacer(1, 3 * mm),
        _note("4.2 All supported document types"),
        _data_table(
            [
                ["Code", "Document", "Number field", "Expiry needed?"],
                ["AADHAAR", "Aadhaar Card", "Aadhaar Number (XXXX XXXX XXXX)", "No"],
                ["PAN", "PAN Card", "PAN Number (ABCDE1234F)", "No"],
                ["GST_CERTIFICATE", "GST Certificate", "GSTIN (22AAAAA0000A1Z5)", "No"],
                [
                    "TRADE_LICENSE",
                    "Trade License",
                    "License Number (TL-2024-XXXXX)",
                    "Yes",
                ],
                [
                    "BUSINESS_REGISTRATION",
                    "Business Registration",
                    "Registration Number (CIN / LLP)",
                    "No",
                ],
                [
                    "BANK_PROOF",
                    "Bank Proof / Cancelled Cheque",
                    "Account number (last 4 digits)",
                    "No",
                ],
                ["AGREEMENT", "Signed Agreement", "—", "No"],
            ],
            [
                content_width * 0.22,
                content_width * 0.24,
                content_width * 0.32,
                content_width * 0.22,
            ],
        ),
        Spacer(1, 3 * mm),
        _note(
            "4.3 File rules — Accepted formats: PDF, JPG, PNG. Maximum size: 10 MB per file."
        ),
        Spacer(1, 4 * mm),
        _section_heading("5. Application Status Flow"),
        _data_table(
            [
                ["Status", "Meaning", "What happens next"],
                ["PENDING", "Application created", "Admin starts KYC review"],
                [
                    "UNDER_REVIEW",
                    "Admin is checking profile + documents",
                    "Admin approves or asks for more docs",
                ],
                [
                    "DOCUMENT_PENDING",
                    "Some documents are missing/rejected",
                    "Upload the requested documents",
                ],
                ["APPROVED", "KYC cleared", "Admin activates the account"],
                ["ACTIVE", "Live — can receive bookings", "Normal operations"],
                ["SUSPENDED", "Temporarily disabled by admin", "Contact support"],
                ["BLOCKED", "Permanently disabled", "Contact support"],
            ],
            [content_width * 0.22, content_width * 0.36, content_width * 0.42],
        ),
        Spacer(1, 3 * mm),
        _note(
            "Note: A wallet is auto-created for every partner (PREPAID, balance Rs. 0) "
            "at registration.",
            WARN,
        ),
        Spacer(1, 4 * mm),
        _section_heading("6. Quick Checklist Before You Apply"),
        *_bullets(
            [
                "Decide partner type (INDIVIDUAL / COMPANY).",
                "Keep your Aadhaar (individuals) or GST + PAN + registration (companies) ready.",
                "Keep a 10-digit mobile starting with 6–9 and a valid email.",
                "Choose your operating city.",
                "Prepare a bank account for settlements.",
                "Have PDF/JPG/PNG scans of every required document ready (max 10 MB each).",
            ]
        ),
    ]
    return s


def _driver_guide_story(content_width: float) -> list[Any]:
    s: list[Any] = [
        _note(
            "Who this is for: WayTero partners who need to add a driver to their "
            "fleet. What this covers: the exact information you must enter and the "
            "documents you must upload for each driver, plus the verification steps."
        ),
        Spacer(1, 4 * mm),
        _section_heading("1. Before You Start"),
        *_bullets(
            [
                "You must already be a registered partner.",
                "A driver is always linked to your partner account.",
                "Each driver's mobile number must be unique across the platform.",
                "A driver only becomes available for bookings after admin approves the profile and sets it to ACTIVE.",
            ]
        ),
        Spacer(1, 4 * mm),
        _section_heading("2. Information You Must Provide (Driver Profile)"),
        _data_table(
            [
                ["Field", "Required?", "Rules / Format"],
                ["Full Name", "Yes", "Max 255 chars"],
                ["Mobile", "Yes", "10-digit number; unique in the system"],
                ["Email", "No", "Valid email"],
                [
                    "Driving License Number",
                    "Yes",
                    "Max 100 chars — must match the physical license",
                ],
                [
                    "License Expiry Date",
                    "No",
                    "Date — an expired license blocks the driver",
                ],
                ["Date of Birth", "No", "Date"],
                ["Joining Date", "No", "Date — when the driver joined your fleet"],
            ],
            [content_width * 0.28, content_width * 0.14, content_width * 0.58],
        ),
        Spacer(1, 3 * mm),
        _note(
            "Every new driver starts with status PENDING. Availability is OFFLINE "
            "until approved.",
            WARN,
        ),
        Spacer(1, 4 * mm),
        _section_heading("3. Documents You Must Upload (Driver KYC)"),
        _note("3.1 Required documents"),
        _data_table(
            [
                ["Code", "Document", "Mandatory?"],
                ["DRIVING_LICENSE", "Driving License", "Mandatory"],
                ["AADHAAR", "Aadhaar Card (Government ID)", "Mandatory"],
                ["PHOTO", "Driver Photo", "Mandatory"],
                ["PAN", "PAN Card", "Recommended"],
                ["POLICE_VERIFICATION", "Police Verification", "Optional"],
                ["MEDICAL_CERTIFICATE", "Medical Certificate", "Recommended"],
            ],
            [content_width * 0.26, content_width * 0.44, content_width * 0.30],
        ),
        Spacer(1, 3 * mm),
        _note(
            "3.2 What admin verifies — Identity, Documents (authenticity + expiry), Address, Background / compliance."
        ),
        Spacer(1, 3 * mm),
        _note(
            "3.3 File rules — Accepted formats: PDF, JPG, PNG. Maximum size: 10 MB per file."
        ),
        Spacer(1, 4 * mm),
        _section_heading("4. Driver Status Flow"),
        _data_table(
            [
                ["Status", "Meaning"],
                ["PENDING", "Profile created, documents pending"],
                ["UNDER_REVIEW", "Admin is verifying documents"],
                ["APPROVED", "KYC cleared"],
                ["ACTIVE", "Can go ONLINE and take trips"],
                ["INACTIVE", "Not currently operating"],
                ["SUSPENDED", "Disabled by admin"],
            ],
            [content_width * 0.30, content_width * 0.70],
        ),
        Spacer(1, 3 * mm),
        _note(
            "A driver can only go ONLINE when status is ACTIVE. While ONLINE the "
            "last_online_at is recorded; other states are OFFLINE, ON_TRIP, BREAK.",
        ),
        Spacer(1, 4 * mm),
        _section_heading("5. Quick Checklist Before Adding a Driver"),
        *_bullets(
            [
                "Confirm the driver's mobile is new/unique.",
                "Collect the driving license number + expiry date.",
                "Have clear scans of the driving license, Aadhaar, and a passport-style photo ready (PDF/JPG/PNG, max 10 MB each).",
                "Collect optional docs (PAN, police verification, medical certificate) if available.",
                "Ensure the license is valid (not expired).",
            ]
        ),
    ]
    return s


def _vehicle_guide_story(content_width: float) -> list[Any]:
    s: list[Any] = [
        _note(
            "Who this is for: WayTero partners who need to add a vehicle (cab) to "
            "their fleet. What this covers: the exact information you must enter, "
            "the documents you must upload, and the vehicle photos required for approval."
        ),
        Spacer(1, 4 * mm),
        _section_heading("1. Before You Start"),
        *_bullets(
            [
                "You must already be a registered partner.",
                "The registration number must be unique across the whole platform.",
                "A vehicle only becomes bookable after admin approves it and sets status to ACTIVE.",
                "A vehicle is tied to a vehicle category (e.g. HATCHBACK, SEDAN, SUV) and an operating city.",
            ]
        ),
        Spacer(1, 4 * mm),
        _section_heading("2. Information You Must Provide (Vehicle Registration)"),
        _data_table(
            [
                ["Field", "Required?", "Rules / Format"],
                [
                    "Registration Number",
                    "Yes",
                    "Vehicle number plate, e.g. MH12AB1234 — stored uppercase",
                ],
                [
                    "Vehicle Category",
                    "Yes",
                    "Select from active categories (category name + seats shown)",
                ],
                ["Operating City", "Yes", "City where this vehicle will operate"],
                ["Brand", "No (recommended)", "e.g. Maruti, Toyota"],
                ["Model", "No (recommended)", "e.g. Dzire, Innova"],
                ["Manufacturing Year", "No", "Between 1990 and 2030"],
                ["Seating Capacity", "No", "Between 1 and 60"],
                ["Fuel Type", "No", "PETROL, DIESEL, CNG, ELECTRIC, or HYBRID"],
            ],
            [content_width * 0.28, content_width * 0.18, content_width * 0.54],
        ),
        Spacer(1, 3 * mm),
        _note("Every new vehicle starts with status PENDING.", WARN),
        Spacer(1, 4 * mm),
        _section_heading("3. Documents You Must Upload (All 5 Required)"),
        _note(
            "These 5 documents are all mandatory for a vehicle to be verified and "
            "activated. The dashboard shows progress as uploaded / 5."
        ),
        _data_table(
            [
                ["Code", "Document", "Expiry date needed?"],
                ["RC", "Registration Certificate", "No (registration is permanent)"],
                ["INSURANCE", "Insurance Certificate", "Yes"],
                ["FITNESS_CERTIFICATE", "Fitness Certificate", "Yes"],
                ["PERMIT", "Permit", "Yes"],
                ["PUC", "Pollution Under Control", "Yes"],
            ],
            [content_width * 0.26, content_width * 0.50, content_width * 0.24],
        ),
        Spacer(1, 3 * mm),
        _note(
            "Important: All 5 documents must be uploaded. Expiry date is required for "
            "Insurance, Fitness, Permit and PUC. You may re-upload a document to "
            "replace the old one (e.g. after renewal).",
            WARN,
        ),
        Spacer(1, 3 * mm),
        _note(
            "File rules — Accepted formats: PDF, JPG, PNG. Maximum size: 10 MB per file."
        ),
        Spacer(1, 4 * mm),
        _section_heading("4. Vehicle Photos (Required for Full Verification)"),
        _note(
            "Photos are optional at registration but are required for full "
            "verification. The admin reviews and approves each photo individually."
        ),
        _data_table(
            [
                ["Photo Type", "Photo"],
                ["FRONT", "Front View"],
                ["BACK", "Rear View"],
                ["LEFT", "Left Side"],
                ["RIGHT", "Right Side"],
                ["INTERIOR", "Interior"],
                ["ODOMETER", "Odometer"],
                ["ENGINE", "Engine Bay"],
                ["OTHER", "Other (optional)"],
            ],
            [content_width * 0.26, content_width * 0.74],
        ),
        Spacer(1, 3 * mm),
        _note(
            "Required for full verification: Front, Rear, Left, Right, Interior, "
            "Odometer, Engine. Photo file rules: JPEG, PNG, WEBP; clear well-lit photos speed up approval.",
        ),
        Spacer(1, 4 * mm),
        _section_heading("5. Vehicle Status Flow"),
        _data_table(
            [
                ["Status", "Meaning"],
                ["PENDING", "Vehicle registered, waiting for review"],
                ["UNDER_REVIEW", "Admin is verifying documents + photos"],
                ["APPROVED", "KYC cleared"],
                ["ACTIVE", "Available for booking assignments"],
                ["ON_TRIP", "Currently assigned to a trip"],
                ["MAINTENANCE", "Under service (out of rotation)"],
                ["INACTIVE", "Not operating"],
                ["SUSPENDED", "Disabled by admin — contact support"],
            ],
            [content_width * 0.30, content_width * 0.70],
        ),
        Spacer(1, 3 * mm),
        _note(
            "When a vehicle becomes ACTIVE, its availability is set to AVAILABLE, "
            "making it eligible for booking assignments.",
        ),
        Spacer(1, 4 * mm),
        _section_heading("6. Quick Checklist Before Adding a Vehicle"),
        *_bullets(
            [
                "Confirm the registration number is unique (not already on WayTero).",
                "Choose the correct category and operating city.",
                "Have the brand/model/year/seating/fuel details handy.",
                "Prepare scans of all 5 documents: RC, Insurance, Fitness, Permit, PUC with valid expiry dates.",
                "Take 7 clear photos: Front, Rear, Left, Right, Interior, Odometer, Engine.",
                "Upload everything before/at registration to get the vehicle active faster.",
            ]
        ),
    ]
    return s


# ── FORM CONTENT ─────────────────────────────────────────────────────────────
def _partner_form_story(content_width: float) -> list[Any]:
    s: list[Any] = [
        _note(
            "Fill in this form by hand, then an admin uses it to register the "
            "partner in the admin portal. Attach clear copies of the required documents.",
        ),
        Spacer(1, 4 * mm),
        _section_heading("1. Partner Type"),
        _checkbox_row("INDIVIDUAL — solo operator (GST / PAN optional)"),
        _checkbox_row("COMPANY — registered business entity (GST + PAN mandatory)"),
        Spacer(1, 4 * mm),
        _section_heading("2. Identity & Contact"),
        _field_row("Owner / Contact Name (full legal name)"),
        _field_row("Business / Trade Name"),
        _field_row("Mobile Number (10-digit, starts with 6–9)"),
        _field_row("Email Address"),
        Spacer(1, 4 * mm),
        _section_heading("3. Location & Business"),
        _field_row("Operating City"),
        _field_row("Office Address Line 1"),
        _field_row("Office Address Line 2"),
        _field_row("Office City (must match Operating City)"),
        _field_row("Office Postal Code (6-digit PIN)"),
        Spacer(1, 4 * mm),
        _section_heading("4. Tax Information"),
        _field_row("GST Number (15-char GSTIN)"),
        _field_row("PAN Number (10-char)"),
        _field_row("GST Legal Name (as on GST certificate)"),
        Spacer(1, 4 * mm),
        _section_heading("5. Services"),
        _checkbox_row("CAB — Cab rentals, airport transfers, outstation trips"),
        _checkbox_row("HOTEL — Hotel bookings and accommodation"),
        _checkbox_row("TOUR — Tour packages, sightseeing, itineraries"),
        Spacer(1, 4 * mm),
        _section_heading("6. Bank Account (For Settlements)"),
        _field_row("Account Holder Name"),
        _field_row("Account Number"),
        _field_row("IFSC Code"),
        _field_row("Bank Name"),
        _field_row("Branch Name"),
        _checkbox_row("Account Type: SAVINGS (default)"),
        _checkbox_row("Account Type: CURRENT"),
        _field_row("Set as Primary (Yes / No)"),
        Spacer(1, 4 * mm),
        _section_heading("7. Documents Attached"),
        _checkbox_row("Aadhaar Card (individuals)"),
        _checkbox_row("PAN Card (all)"),
        _checkbox_row("GST Certificate (companies)"),
        _checkbox_row("Company / Business Registration (companies)"),
        _checkbox_row("Bank Proof / Cancelled Cheque"),
        _checkbox_row("Signed Agreement (as advised)"),
        *_signature_block(),
    ]
    return s


def _vehicle_form_story(content_width: float) -> list[Any]:
    s: list[Any] = [
        _note(
            "Fill in this form by hand, then an admin uses it to add the vehicle "
            "in the admin portal. The partner must already be registered — quote "
            "the Partner ID / Partner mobile as reference. Attach copies of all 5 documents.",
        ),
        Spacer(1, 4 * mm),
        _section_heading("1. Partner Reference (required)"),
        _field_row("Partner ID (reference — must already be registered)"),
        _field_row("Partner Name"),
        _field_row("Partner Mobile"),
        Spacer(1, 4 * mm),
        _section_heading("2. Vehicle Details"),
        _field_row("Registration Number (e.g. MH12AB1234)"),
        _field_row("Vehicle Category (e.g. HATCHBACK, SEDAN, SUV)"),
        _field_row("Operating City"),
        _field_row("Brand (e.g. Maruti, Toyota)"),
        _field_row("Model (e.g. Dzire, Innova)"),
        _field_row("Manufacturing Year (1990–2030)"),
        _field_row("Seating Capacity (1–60)"),
        _checkbox_row("Fuel Type: PETROL"),
        _checkbox_row("Fuel Type: DIESEL"),
        _checkbox_row("Fuel Type: CNG"),
        _checkbox_row("Fuel Type: ELECTRIC"),
        _checkbox_row("Fuel Type: HYBRID"),
        Spacer(1, 4 * mm),
        _section_heading("3. Documents Attached (All 5 Required)"),
        _checkbox_row("RC — Registration Certificate"),
        _checkbox_row("INSURANCE — Insurance Certificate (expiry: ______)"),
        _checkbox_row("FITNESS_CERTIFICATE — Fitness Certificate (expiry: ______)"),
        _checkbox_row("PERMIT — Permit (expiry: ______)"),
        _checkbox_row("PUC — Pollution Under Control (expiry: ______)"),
        Spacer(1, 3 * mm),
        _section_heading("4. Vehicle Photos (for full verification)"),
        _checkbox_row("Front View"),
        _checkbox_row("Rear View"),
        _checkbox_row("Left Side"),
        _checkbox_row("Right Side"),
        _checkbox_row("Interior"),
        _checkbox_row("Odometer"),
        _checkbox_row("Engine Bay"),
        *_signature_block(),
    ]
    return s


def _driver_form_story(content_width: float) -> list[Any]:
    s: list[Any] = [
        _note(
            "Fill in this form by hand, then an admin uses it to add the driver "
            "in the admin portal. The partner must already be registered — quote "
            "the Partner ID / Partner mobile as reference. Attach copies of the required documents.",
        ),
        Spacer(1, 4 * mm),
        _section_heading("1. Partner Reference (required)"),
        _field_row("Partner ID (reference — must already be registered)"),
        _field_row("Partner Name"),
        Spacer(1, 4 * mm),
        _section_heading("2. Driver Details"),
        _field_row("Full Name"),
        _field_row("Mobile Number (10-digit, unique in system)"),
        _field_row("Email Address"),
        _field_row("Driving License Number"),
        _field_row("License Expiry Date"),
        _field_row("Date of Birth"),
        _field_row("Joining Date"),
        Spacer(1, 4 * mm),
        _section_heading("3. Documents Attached"),
        _checkbox_row("DRIVING_LICENSE — Driving License (mandatory)"),
        _checkbox_row("AADHAAR — Aadhaar Card (mandatory)"),
        _checkbox_row("PHOTO — Driver Photo (mandatory)"),
        _checkbox_row("PAN — PAN Card (recommended)"),
        _checkbox_row("POLICE_VERIFICATION — Police Verification (optional)"),
        _checkbox_row("MEDICAL_CERTIFICATE — Medical Certificate (recommended)"),
        *_signature_block(),
    ]
    return s


# ── PUBLIC API ───────────────────────────────────────────────────────────────
_META: dict[str, dict[str, Any]] = {
    "guide_partner": {
        "doc_title": "Partner Registration Guide",
        "header_title": "PARTNER REGISTRATION GUIDE",
        "header_subtitle": "Requirements & Documents",
        "filename": "WayTero_Partner_Registration_Guide.pdf",
        "story": _partner_guide_story,
    },
    "guide_driver": {
        "doc_title": "Driver Onboarding Guide",
        "header_title": "DRIVER ONBOARDING GUIDE",
        "header_subtitle": "Requirements & Documents",
        "filename": "WayTero_Driver_Onboarding_Guide.pdf",
        "story": _driver_guide_story,
    },
    "guide_vehicle": {
        "doc_title": "Vehicle Onboarding Guide",
        "header_title": "VEHICLE ONBOARDING GUIDE",
        "header_subtitle": "Requirements & Documents",
        "filename": "WayTero_Vehicle_Onboarding_Guide.pdf",
        "story": _vehicle_guide_story,
    },
    "form_partner": {
        "doc_title": "Partner Registration Form",
        "header_title": "PARTNER REGISTRATION FORM",
        "header_subtitle": "To be filled by hand and used to register the partner",
        "filename": "WayTero_Partner_Registration_Form.pdf",
        "story": _partner_form_story,
    },
    "form_vehicle": {
        "doc_title": "Vehicle Registration Form",
        "header_title": "VEHICLE REGISTRATION FORM",
        "header_subtitle": "To be filled by hand — Partner ID required as reference",
        "filename": "WayTero_Vehicle_Registration_Form.pdf",
        "story": _vehicle_form_story,
    },
    "form_driver": {
        "doc_title": "Driver Registration Form",
        "header_title": "DRIVER REGISTRATION FORM",
        "header_subtitle": "To be filled by hand — Partner ID required as reference",
        "filename": "WayTero_Driver_Registration_Form.pdf",
        "story": _driver_form_story,
    },
}

GUIDE_KEYS = ("guide_partner", "guide_driver", "guide_vehicle")
FORM_KEYS = ("form_partner", "form_vehicle", "form_driver")


def generate_onboarding_pdf(doc_key: str, cfg: dict[str, str]) -> dict[str, Any]:
    """Generate an onboarding guide or form PDF.

    Returns {"pdf": bytes, "filename": str}.
    """
    meta = _META.get(doc_key)
    if meta is None:
        raise ValueError(f"Unknown onboarding document: {doc_key}")
    pdf = _build_document(
        cfg,
        doc_title=meta["doc_title"],
        header_title=meta["header_title"],
        header_subtitle=meta["header_subtitle"],
        build_story=meta["story"],
    )
    return {"pdf": pdf, "filename": meta["filename"]}
