# ============================================================
# WAYTERO — TOUR PDF DOCUMENTS (advance receipt + invoice + itinerary)
# File: app/modules/tour/services/pdf.py
# Doc Ref: BRD Part 5 §6, BRD Part 3 §45
#
# Streamed, not stored — the documents are fully reproducible from the
# booking + advance rows. The visual family (palette, header/footer bands,
# logo, section layout) mirrors the cab/hotel documents in
# app/modules/admin/invoice_pdf_service.py so all three services share one
# print identity.
#
# Platform branding (logo, name, GST, address, support) is pulled from
# system_configurations at render time.
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

# ── Palette (matches admin/invoice_pdf_service.py) ──────────────────────────
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
    """Render a rupee amount in Indian numbering, e.g.
    1234.50 -> 'Rupees One Thousand Two Hundred Thirty Four and Fifty Paise Only'."""
    rupees = int(amount)
    paise = int(round((amount - rupees) * 100))
    if paise == 100:
        rupees += 1
        paise = 0
    if rupees == 0:
        words = "Zero"
    else:
        parts: list[str] = []
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


def _fmt_date_iso(value: str | None) -> str:
    """'2026-08-14' -> '14 Aug 2026' (or '—')."""
    if not value:
        return "—"
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%d %b %Y")
    except ValueError:
        return value


def _fmt_dt(dt: datetime | None) -> str:
    if not dt:
        return "—"
    return dt.strftime("%d %b %Y, %I:%M %p")


# ── Document scaffolding ──────────────────────────────────────────────────────


class _TourDoc:
    """Builds an A4 BaseDocTemplate with the shared navy header band, accent
    stripe and footer band used by every WayTero document."""

    def __init__(
        self,
        *,
        filename: str,
        footer_left: str,
        footer_right: str,
        platform_name: str = "WayTero",
        support_email: str = "",
        support_phone: str = "",
        generated_label: str = "Generated on",
    ):
        self.buf = io.BytesIO()
        self.W, self.H = A4
        self.MARGIN = 18 * mm
        self.doc = BaseDocTemplate(
            self.buf,
            pagesize=A4,
            leftMargin=self.MARGIN,
            rightMargin=self.MARGIN,
            topMargin=12 * mm,
            bottomMargin=14 * mm,
            title=filename.replace(".pdf", ""),
            author=platform_name,
        )
        self.content_width = self.W - 2 * self.MARGIN
        self.filename = filename
        self._footer_left = footer_left
        self._footer_right = footer_right
        self._platform = platform_name
        self._email = support_email
        self._phone = support_phone
        self._generated_label = generated_label
        self._init_page_template()

    def _init_page_template(self) -> None:
        W, H = self.W, self.H
        platform, email, phone = self._platform, self._email, self._phone
        footer_left = self._footer_left

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
            canvas.setFillColor(colors.HexColor("#94a3b8"))
            canvas.setFont("Helvetica", 7)
            footer_y = 3.5 * mm
            canvas.drawString(self.MARGIN, footer_y, footer_left)
            canvas.drawRightString(
                W - self.MARGIN,
                footer_y,
                f"{platform}  |  {email}  |  {phone}",
            )
            canvas.setFillColor(colors.HexColor("#64748b"))
            canvas.setFont("Helvetica", 6.5)
            canvas.drawCentredString(W / 2, footer_y, f"Page {doc.page}")
            canvas.restoreState()

        frame = Frame(
            self.MARGIN,
            14 * mm,
            self.content_width,
            H - 22 * mm - 14 * mm - 4 * mm,
            showBoundary=0,
        )
        self.doc.addPageTemplates(
            [PageTemplate(id="main", frames=[frame], onPage=_draw_page)]
        )

    # ── Common blocks ─────────────────────────────────────────────────────────

    def header_block(
        self,
        *,
        doc_title: str,
        doc_number: str,
        date_label: str,
        date_value: str,
        platform_name: str,
        platform_logo_url: str,
        business_legal_name: str,
        business_gst_number: str,
        business_registered_address: str,
    ) -> None:
        """Logo + platform identity on the left, document title/number/date on
        the right — sits inside the navy header band."""
        story = self.story
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

        left: list[Any] = []
        if logo_img:
            left.append(logo_img)
            left.append(Spacer(1, 2 * mm))
        left.append(_p(f"<b>{platform_name}</b>", _ps("hn", 14, BRAND_MAIN, bold=True)))
        if business_legal_name:
            left.append(_p(business_legal_name, _ps("bln", 7.5, GREY_600)))
        if business_gst_number:
            left.append(
                _p(f"GSTIN: <b>{business_gst_number}</b>", _ps("gst2", 7.5, GREY_600))
            )
        if business_registered_address:
            left.append(_p(business_registered_address, _ps("adr", 7, GREY_600)))

        right = [
            _p(doc_title, _ps("TITLE", 20, BRAND_DARK, bold=True, align=TA_RIGHT)),
            Spacer(1, 1 * mm),
            _p(doc_number, _ps("num", 10, BRAND_MAIN, bold=True, align=TA_RIGHT)),
            Spacer(1, 1 * mm),
            _p(f"{date_label}: {date_value}", _ps("dt", 8, GREY_600, align=TA_RIGHT)),
        ]

        tbl = Table(
            [[left, right]],
            colWidths=[self.content_width * 0.55, self.content_width * 0.45],
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

    def section_h(self, text: str) -> None:
        self.story.append(
            _p(
                text,
                _ps(f"sh_{len(self.story)}", 9, BRAND_MAIN, bold=True, space_after=4),
            )
        )

    def two_col(
        self,
        left_items: list[Any],
        right_items: list[Any],
    ) -> None:
        tbl = Table(
            [[left_items, right_items]],
            colWidths=[self.content_width * 0.5, self.content_width * 0.5],
        )
        tbl.setStyle(
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
        self.story.append(tbl)
        self.story.append(Spacer(1, 5 * mm))

    def hr(self) -> None:
        self.story.append(HRFlowable(width="100%", thickness=0.5, color=GREY_200))
        self.story.append(Spacer(1, 3 * mm))

    def finish(self) -> dict:
        self.doc.build(self.story)
        return {"pdf": self.buf.getvalue(), "filename": self.filename}


def _money_table(
    doc: _TourDoc,
    rows: list[list[Any]],
    *,
    total_row: int | None = None,
    last_highlight: tuple[str, str] | None = None,
    last_bg: Any = None,
    last_line: Any = None,
    header_bg: Any = BRAND_MAIN,
) -> None:
    """Render a two-column money/description table with header row, optional
    highlighted total row and optional final status row (same visual language
    as the cab fare breakdown)."""
    n = len(rows)
    style: list[Any] = [
        ("BACKGROUND", (0, 0), (-1, 0), header_bg),
        ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 8),
        ("GRID", (0, 0), (-1, -1), 0.3, GREY_200),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]
    for i in range(1, n):
        if i % 2 == 0 and i != total_row:
            style.append(("BACKGROUND", (0, i), (-1, i), GREY_100))
    if total_row is not None and 0 <= total_row < n:
        style.append(
            ("BACKGROUND", (0, total_row), (-1, total_row), colors.HexColor("#e0f2fe"))
        )
        style.append(("LINEABOVE", (0, total_row), (-1, total_row), 1.2, BRAND_MAIN))
    if last_highlight is not None:
        style.append(
            (
                "BACKGROUND",
                (0, n - 1),
                (-1, n - 1),
                last_bg or colors.HexColor("#dcfce7"),
            )
        )
        style.append(("LINEBELOW", (0, n - 1), (-1, n - 1), 1.5, last_line or SUCCESS))
    tbl = Table(rows, colWidths=[doc.content_width * 0.65, doc.content_width * 0.35])
    tbl.setStyle(TableStyle(style))
    doc.story.append(tbl)
    doc.story.append(Spacer(1, 6 * mm))


def _billed_summary(
    doc: _TourDoc,
    *,
    billed_lines: list[tuple[str, str]],
    summary_lines: list[tuple[str, str]],
    payment_label: str,
    payment_color: Any,
    payment_note: str,
) -> None:
    billed = [_p("BILLED TO", _ps("bt_h", 9, BRAND_MAIN, bold=True, space_after=4))]
    for label, value in billed_lines:
        billed.append(
            _p(
                f"<b>{value}</b>" if label == "Name" else value,
                _ps("bt_v", 9, GREY_900, bold=(label == "Name")),
            )
        )
    billed.append(Spacer(1, 2 * mm))

    summary = [
        _p("BOOKING SUMMARY", _ps("bs_h", 9, BRAND_MAIN, bold=True, space_after=4))
    ]
    for label, value in summary_lines:
        summary.append(_p(f"{label}: <b>{value}</b>", _ps("bs_v", 8, GREY_900)))
    summary.append(Spacer(1, 2 * mm))
    summary.append(
        _p("PAYMENT STATUS", _ps("ps_h", 9, BRAND_MAIN, bold=True, space_after=2))
    )
    summary.append(
        _p(f"<b>{payment_label}</b>", _ps("ps_l", 9, payment_color, bold=True))
    )
    summary.append(_p(payment_note, _ps("ps_n", 7.5, GREY_600)))

    doc.two_col(billed, summary)


# ═════════════════════════════════════════════════════════════════════════════
# TOUR ADVANCE RECEIPT
# ═════════════════════════════════════════════════════════════════════════════


def build_tour_advance_receipt_pdf(
    *,
    receipt_number: str,
    booking_number: str,
    package_name: str,
    destination: str,
    travel_start_date: str | None,
    travel_end_date: str | None,
    persons_count: int,
    customer_name: str | None,
    customer_mobile: str | None,
    partner_name: str | None,
    amount: float,
    payment_mode: str,
    received_by: str,
    collected_at: datetime | None,
    reference_number: str | None = None,
    # Platform branding (from system_configurations)
    platform_name: str = "WayTero",
    platform_logo_url: str = "",
    business_legal_name: str = "",
    business_gst_number: str = "",
    business_registered_address: str = "",
    support_email: str = "",
    support_phone: str = "",
) -> dict:
    """Premium A4 advance receipt — mirrors generate_advance_receipt_pdf
    (cab) with tour facts: package, destination, travel dates, travellers."""
    rec_dt = collected_at or datetime.now(timezone.utc)
    doc = _TourDoc(
        filename=f"tour-advance-{receipt_number}.pdf",
        footer_left=f"Issued on {_fmt_dt(rec_dt)}  |  {receipt_number}",
        footer_right="",
        platform_name=platform_name,
        support_email=support_email,
        support_phone=support_phone,
    )
    doc.story = []
    doc.header_block(
        doc_title="ADVANCE RECEIPT",
        doc_number=receipt_number,
        date_label="Date",
        date_value=rec_dt.strftime("%d %B %Y"),
        platform_name=platform_name,
        platform_logo_url=platform_logo_url,
        business_legal_name=business_legal_name,
        business_gst_number=business_gst_number,
        business_registered_address=business_registered_address,
    )

    _billed_summary(
        doc,
        billed_lines=[
            ("Name", customer_name or "—"),
            ("Mobile", customer_mobile or "—"),
        ],
        summary_lines=[
            ("Tour Booking", booking_number),
            ("Package", package_name or "—"),
            ("Destination", destination or "—"),
            (
                "Travel dates",
                f"{_fmt_date_iso(travel_start_date)} → {_fmt_date_iso(travel_end_date)}",
            ),
            ("Travellers", str(persons_count)),
        ],
        payment_label="ADVANCE RECEIVED",
        payment_color=SUCCESS,
        payment_note=f"Mode: {payment_mode or '—'}  |  Received by: {received_by or '—'}",
    )

    # ── Advance received banner ──────────────────────────────────────────────
    doc.section_h("ADVANCE RECEIVED")
    amount_block = [
        _p("ADVANCE RECEIVED", _ps("arh", 8, GREY_600, bold=True)),
        Spacer(1, 1 * mm),
        _p(f"Rs. {amount:,.2f}", _ps("arv", 22, SUCCESS, bold=True)),
        Spacer(1, 1 * mm),
        _p(_amount_in_words(amount), _ps("arw", 8, GREY_600)),
    ]
    meta_rows = [
        ["Payment Mode", (payment_mode or "—").upper()],
        ["Received By", (received_by or "—").upper()],
        ["Received On", _fmt_dt(rec_dt)],
        ["Reference", reference_number or "—"],
    ]
    meta_tbl = Table(
        meta_rows, colWidths=[doc.content_width * 0.16, doc.content_width * 0.28]
    )
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
    banner = Table(
        [[amount_block, meta_tbl]],
        colWidths=[doc.content_width * 0.52, doc.content_width * 0.48],
    )
    banner.setStyle(
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
    doc.story.append(banner)
    doc.story.append(Spacer(1, 5 * mm))

    # ── Indicative balance ───────────────────────────────────────────────────
    doc.section_h("INDICATIVE BALANCE")
    doc.story.append(Spacer(1, 2 * mm))
    rows: list[list[Any]] = [
        [
            Paragraph("Description", _ps("bh1", 8, WHITE, bold=True)),
            Paragraph("Amount", _ps("bh2", 8, WHITE, bold=True, align=TA_RIGHT)),
        ],
        [
            Paragraph("Advance Received (this receipt)", _ps("bi", 8.5, SUCCESS)),
            Paragraph(f"− Rs. {amount:,.2f}", _ps("bv", 8.5, SUCCESS, align=TA_RIGHT)),
        ],
    ]
    _money_table(doc, rows, total_row=None, last_highlight=None)
    doc.story.append(Spacer(1, 1 * mm))
    doc.story.append(
        _p(
            "This receipt acknowledges the advance collected; the final invoice is "
            "generated on completion. The balance is payable at trip end.",
            _ps("note", 7, GREY_600),
        )
    )
    doc.story.append(Spacer(1, 5 * mm))

    # ── Disclaimer + signatures ──────────────────────────────────────────────
    doc.story.append(HRFlowable(width="100%", thickness=1, color=BRAND_GOLD))
    doc.story.append(Spacer(1, 3 * mm))
    doc.story.append(
        _p(
            "<b>This is an advance receipt, not a tax invoice.</b> The balance is "
            "payable at trip end, when the tour invoice will be issued.",
            _ps("disc", 8, BRAND_DARK, align=TA_CENTER),
        )
    )
    doc.story.append(Spacer(1, 4 * mm))
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
        colWidths=[doc.content_width * 0.5, doc.content_width * 0.5],
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
    doc.story.append(sign_tbl)
    doc.story.append(Spacer(1, 4 * mm))
    doc.story.append(
        _p(
            f"Thank you for choosing <b>{platform_name}</b>. This is a computer-generated "
            "receipt; retain it until the trip is invoiced.",
            _ps("ty", 7.5, GREY_600, align=TA_CENTER),
        )
    )
    return doc.finish()


# ═════════════════════════════════════════════════════════════════════════════
# TOUR BOOKING INVOICE
# ═════════════════════════════════════════════════════════════════════════════


def build_tour_invoice_pdf(
    *,
    invoice_number: str,
    booking_number: str,
    package_name: str,
    destination: str,
    travel_start_date: str | None,
    travel_end_date: str | None,
    persons_count: int,
    customer_name: str | None,
    customer_mobile: str | None,
    partner_name: str | None,
    invoice_date: datetime | None,
    total_amount: float,
    platform_commission: float,
    partner_payout: float,
    additional_amount: float,
    advance_total: float,
    balance_due: float,
    charges: list[dict],
    # Platform branding (from system_configurations)
    platform_name: str = "WayTero",
    platform_logo_url: str = "",
    business_legal_name: str = "",
    business_gst_number: str = "",
    business_registered_address: str = "",
    support_email: str = "",
    support_phone: str = "",
) -> dict:
    """Premium A4 tour invoice — mirrors generate_invoice_pdf (cab): fare
    breakdown with total + balance/paid rows, commission/payout, thank-you."""
    inv_dt = invoice_date or datetime.now(timezone.utc)
    doc = _TourDoc(
        filename=f"tour-invoice-{invoice_number}.pdf",
        footer_left=f"Generated on {_fmt_dt(inv_dt)}  |  {invoice_number}",
        footer_right="",
        platform_name=platform_name,
        support_email=support_email,
        support_phone=support_phone,
    )
    doc.story = []
    doc.header_block(
        doc_title="TOUR INVOICE",
        doc_number=invoice_number,
        date_label="Date",
        date_value=inv_dt.strftime("%d %B %Y"),
        platform_name=platform_name,
        platform_logo_url=platform_logo_url,
        business_legal_name=business_legal_name,
        business_gst_number=business_gst_number,
        business_registered_address=business_registered_address,
    )

    paid = balance_due <= 0
    _billed_summary(
        doc,
        billed_lines=[
            ("Name", customer_name or "—"),
            ("Mobile", customer_mobile or "—"),
        ],
        summary_lines=[
            ("Tour Booking", booking_number),
            ("Package", package_name or "—"),
            ("Destination", destination or "—"),
            (
                "Travel dates",
                f"{_fmt_date_iso(travel_start_date)} → {_fmt_date_iso(travel_end_date)}",
            ),
            ("Travellers", str(persons_count)),
            ("Partner", partner_name or "—"),
        ],
        payment_label="PAID" if paid else "PARTIALLY PAID",
        payment_color=SUCCESS if paid else BRAND_GOLD,
        payment_note=f"Advance collected: Rs. {advance_total:,.2f}  |  Balance due: Rs. {max(balance_due, 0):,.2f}",
    )

    # ── Charges / fare breakdown ─────────────────────────────────────────────
    doc.hr()
    doc.section_h("CHARGE BREAKDOWN")
    doc.story.append(Spacer(1, 2 * mm))
    rows: list[list[Any]] = [
        [
            Paragraph("Description", _ps("fh1", 8, WHITE, bold=True)),
            Paragraph("Amount", _ps("fh2", 8, WHITE, bold=True, align=TA_RIGHT)),
        ],
    ]
    package_total = max(total_amount - additional_amount, 0)
    rows.append(
        [
            Paragraph("Package Total", _ps("fi", 8.5, GREY_900)),
            Paragraph(
                f"Rs. {package_total:,.2f}", _ps("fv", 8.5, GREY_900, align=TA_RIGHT)
            ),
        ]
    )
    for c in charges:
        rows.append(
            [
                Paragraph(
                    c.get("label") or "Additional charge", _ps("fi", 8.5, GREY_900)
                ),
                Paragraph(
                    f"Rs. {float(c.get('amount') or 0):,.2f}",
                    _ps("fv", 8.5, GREY_900, align=TA_RIGHT),
                ),
            ]
        )
    if advance_total > 0:
        rows.append(
            [
                Paragraph("Advance Paid", _ps("fi", 8.5, GREY_600)),
                Paragraph(
                    f"− Rs. {advance_total:,.2f}",
                    _ps("fv", 8.5, GREY_600, align=TA_RIGHT),
                ),
            ]
        )
    total_row_idx = len(rows)
    rows.append(
        [
            Paragraph("TOTAL PAYABLE", _ps("gtl", 10, BRAND_DARK, bold=True)),
            Paragraph(
                f"Rs. {total_amount:,.2f}",
                _ps("gtv", 11, BRAND_DARK, bold=True, align=TA_RIGHT),
            ),
        ]
    )
    if balance_due > 0:
        last = (
            Paragraph("Balance Due", _ps("bdl", 9, BRAND_GOLD, bold=True)),
            Paragraph(
                f"Rs. {max(balance_due, 0):,.2f}",
                _ps("bdv", 9, BRAND_GOLD, bold=True, align=TA_RIGHT),
            ),
        )
    else:
        last = (
            Paragraph("✔ Fully Paid", _ps("fpl", 9, SUCCESS, bold=True)),
            Paragraph("Rs. 0.00", _ps("fpv", 9, SUCCESS, bold=True, align=TA_RIGHT)),
        )
    _money_table(
        doc,
        rows,
        total_row=total_row_idx,
        last_highlight=last,
        last_bg=(
            colors.HexColor("#dcfce7")
            if balance_due <= 0
            else colors.HexColor("#fefce8")
        ),
        last_line=SUCCESS if balance_due <= 0 else BRAND_GOLD,
    )

    # ── Commission / payout summary ──────────────────────────────────────────
    doc.section_h("SETTLEMENT SUMMARY")
    doc.story.append(Spacer(1, 2 * mm))
    _kv = Table(
        [
            [
                Paragraph("Platform commission", _ps("kl", 8, GREY_600)),
                Paragraph(
                    f"Rs. {platform_commission:,.2f}",
                    _ps("kv", 8.5, GREY_900, align=TA_RIGHT),
                ),
            ],
            [
                Paragraph("Partner payout", _ps("kl", 8, GREY_600)),
                Paragraph(
                    f"Rs. {partner_payout:,.2f}",
                    _ps("kv", 8.5, GREY_900, align=TA_RIGHT),
                ),
            ],
            [
                Paragraph("Advance collected", _ps("kl", 8, GREY_600)),
                Paragraph(
                    f"Rs. {advance_total:,.2f}",
                    _ps("kv", 8.5, GREY_900, align=TA_RIGHT),
                ),
            ],
        ],
        colWidths=[doc.content_width * 0.65, doc.content_width * 0.35],
    )
    _kv.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F7F9FC")),
                ("BOX", (0, 0), (-1, -1), 0.5, GREY_200),
                ("INNERGRID", (0, 0), (-1, -1), 0.25, GREY_200),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    doc.story.append(_kv)
    doc.story.append(Spacer(1, 4 * mm))
    doc.story.append(
        _p(
            f"Rupees in words: {_amount_in_words(total_amount)}",
            _ps("words", 8, GREY_600),
        )
    )

    # ── Thank you ────────────────────────────────────────────────────────────
    doc.story.append(Spacer(1, 4 * mm))
    doc.story.append(HRFlowable(width="100%", thickness=1, color=BRAND_ACCENT))
    doc.story.append(Spacer(1, 4 * mm))
    doc.story.append(
        _p(
            f"Thank you for choosing <b>{platform_name}</b>!",
            _ps("ty", 10, BRAND_DARK, bold=True, align=TA_CENTER),
        )
    )
    doc.story.append(Spacer(1, 1.5 * mm))
    doc.story.append(
        _p(
            "This is a computer-generated invoice and does not require a physical "
            "signature. For queries, contact our support team.",
            _ps("tyb", 7.5, GREY_600, align=TA_CENTER),
        )
    )
    if support_email or support_phone:
        contact = "  |  ".join(filter(None, [support_email, support_phone]))
        doc.story.append(Spacer(1, 1.5 * mm))
        doc.story.append(
            _p(f"✉ {contact}", _ps("ct", 7.5, BRAND_MAIN, bold=True, align=TA_CENTER))
        )
    if business_gst_number:
        doc.story.append(Spacer(1, 1.5 * mm))
        doc.story.append(
            _p(
                f"Platform GSTIN: <b>{business_gst_number}</b>",
                _ps("gstf", 7, GREY_600, align=TA_CENTER),
            )
        )
    return doc.finish()


# ═════════════════════════════════════════════════════════════════════════════
# TOUR ITINERARY (booking document handed to the customer)
# ═════════════════════════════════════════════════════════════════════════════


def build_tour_itinerary_pdf(
    *,
    booking_number: str,
    master_booking_number: str | None,
    package_name: str,
    package_code: str | None,
    destination: str,
    city_name: str | None,
    duration_days: int | None,
    duration_nights: int | None,
    travel_start_date: str | None,
    travel_end_date: str | None,
    persons_count: int,
    pickup_location: str | None,
    pickup_datetime: datetime | None,
    customer_name: str | None,
    customer_mobile: str | None,
    partner_name: str | None,
    booking_status: str | None,
    payment_status: str | None,
    total_amount: float,
    additional_amount: float,
    advance_total: float,
    balance_due: float,
    invoice_number: str | None,
    itinerary: list[dict],
    vehicle: dict | None,
    driver: dict | None,
    generated_at: datetime | None = None,
    # Platform branding (from system_configurations)
    platform_name: str = "WayTero",
    platform_logo_url: str = "",
    business_legal_name: str = "",
    business_gst_number: str = "",
    business_registered_address: str = "",
    support_email: str = "",
    support_phone: str = "",
) -> dict:
    """Premium A4 tour itinerary — the customer-facing document for the trip:
    booking + customer details, day-by-day package itinerary, current payment
    position and (when assigned) the vehicle & driver."""
    gen_at = generated_at or datetime.now(timezone.utc)
    doc = _TourDoc(
        filename=f"tour-itinerary-{booking_number}.pdf",
        footer_left=f"Generated on {_fmt_dt(gen_at)}  |  {booking_number}",
        footer_right="",
        platform_name=platform_name,
        support_email=support_email,
        support_phone=support_phone,
    )
    doc.story = []
    doc.header_block(
        doc_title="TOUR ITINERARY",
        doc_number=booking_number,
        date_label="Generated",
        date_value=gen_at.strftime("%d %B %Y"),
        platform_name=platform_name,
        platform_logo_url=platform_logo_url,
        business_legal_name=business_legal_name,
        business_gst_number=business_gst_number,
        business_registered_address=business_registered_address,
    )

    # ── Customer + booking summary ───────────────────────────────────────────
    paid = (payment_status or "").upper() == "PAID"
    _billed_summary(
        doc,
        billed_lines=[
            ("Name", customer_name or "—"),
            ("Mobile", customer_mobile or "—"),
        ],
        summary_lines=[
            ("Tour Booking", booking_number),
            ("Master Booking", master_booking_number or "—"),
            ("Package", package_name or "—"),
            ("Destination", destination or "—"),
            (
                "Travel dates",
                f"{_fmt_date_iso(travel_start_date)} → {_fmt_date_iso(travel_end_date)}",
            ),
            (
                "Duration",
                f"{duration_days or '—'} days / {duration_nights or '—'} nights",
            ),
            ("Travellers", str(persons_count)),
            ("Partner", partner_name or "—"),
        ],
        payment_label=(
            "PAID" if paid else (payment_status or "—").replace("_", " ").title()
        ),
        payment_color=SUCCESS if paid else BRAND_GOLD,
        payment_note=f"Advance collected: Rs. {advance_total:,.2f}  |  Balance due: Rs. {max(balance_due, 0):,.2f}",
    )

    # ── Pickup details ───────────────────────────────────────────────────────
    if pickup_location or pickup_datetime:
        doc.hr()
        doc.section_h("PICKUP DETAILS")
        doc.story.append(Spacer(1, 2 * mm))
        _pickup = Table(
            [
                [
                    Paragraph("Pickup location", _ps("kl", 8, GREY_600)),
                    Paragraph(pickup_location or "—", _ps("kv", 8.5, GREY_900)),
                ],
                [
                    Paragraph("Pickup time", _ps("kl", 8, GREY_600)),
                    Paragraph(_fmt_dt(pickup_datetime), _ps("kv", 8.5, GREY_900)),
                ],
            ],
            colWidths=[doc.content_width * 0.30, doc.content_width * 0.70],
        )
        _pickup.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ]
            )
        )
        doc.story.append(_pickup)
        doc.story.append(Spacer(1, 4 * mm))

    # ── Day-by-day itinerary ─────────────────────────────────────────────────
    doc.hr()
    doc.section_h("TOUR ITINERARY")
    doc.story.append(Spacer(1, 2 * mm))
    if not itinerary:
        doc.story.append(
            _p(
                "No day-by-day itinerary has been published for this package yet.",
                _ps("no_it", 8.5, GREY_600),
            )
        )
        doc.story.append(Spacer(1, 4 * mm))
    else:
        header = [
            Paragraph("Day", _ps("ih1", 8, WHITE, bold=True)),
            Paragraph("Plan", _ps("ih2", 8, WHITE, bold=True)),
            Paragraph("Activities", _ps("ih3", 8, WHITE, bold=True)),
            Paragraph("Overnight", _ps("ih4", 8, WHITE, bold=True)),
        ]
        it_rows: list[list[Any]] = [header]
        for day in itinerary:
            activities = day.get("activities") or []
            act_text = "<br/>".join(f"• {a}" for a in activities) if activities else "—"
            overnight = day.get("overnight_city") or "—"
            it_rows.append(
                [
                    Paragraph(
                        f"<b>Day {day.get('day_number', '—')}</b>",
                        _ps("id", 8, BRAND_MAIN, bold=True),
                    ),
                    Paragraph(
                        f"<b>{day.get('title') or '—'}</b>"
                        + (
                            f"<br/><font color='#475569'>{day.get('description') or ''}</font>"
                            if day.get("description")
                            else ""
                        ),
                        _ps("it", 8, GREY_900),
                    ),
                    Paragraph(act_text, _ps("ia", 8, GREY_900)),
                    Paragraph(overnight, _ps("io", 8, GREY_600)),
                ]
            )
        it_tbl = Table(
            it_rows,
            colWidths=[
                doc.content_width * 0.10,
                doc.content_width * 0.42,
                doc.content_width * 0.33,
                doc.content_width * 0.15,
            ],
        )
        it_style: list[Any] = [
            ("BACKGROUND", (0, 0), (-1, 0), BRAND_DARK),
            ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 8),
            ("GRID", (0, 0), (-1, -1), 0.3, GREY_200),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]
        for i in range(1, len(it_rows)):
            if i % 2 == 0:
                it_style.append(("BACKGROUND", (0, i), (-1, i), GREY_100))
        it_tbl.setStyle(TableStyle(it_style))
        doc.story.append(it_tbl)
        doc.story.append(Spacer(1, 6 * mm))

    # ── Vehicle & driver ─────────────────────────────────────────────────────
    doc.hr()
    doc.section_h("VEHICLE & DRIVER")
    doc.story.append(Spacer(1, 2 * mm))
    if vehicle or driver:
        v_rows: list[list[Any]] = []
        if vehicle:
            v_rows.append(
                [
                    Paragraph("Vehicle", _ps("kl", 8, GREY_600)),
                    Paragraph(
                        f"{vehicle.get('vehicle_brand') or ''} {vehicle.get('vehicle_model') or ''} — {vehicle.get('registration_number') or '—'}",
                        _ps("kv", 8.5, GREY_900),
                    ),
                ]
            )
            if vehicle.get("seating_capacity"):
                v_rows.append(
                    [
                        Paragraph("Seating", _ps("kl", 8, GREY_600)),
                        Paragraph(
                            f"{vehicle.get('seating_capacity')} seats",
                            _ps("kv", 8.5, GREY_900),
                        ),
                    ]
                )
        if driver:
            v_rows.append(
                [
                    Paragraph("Driver", _ps("kl", 8, GREY_600)),
                    Paragraph(
                        f"{driver.get('full_name') or '—'}"
                        + (
                            f" ({driver.get('mobile')})" if driver.get("mobile") else ""
                        ),
                        _ps("kv", 8.5, GREY_900),
                    ),
                ]
            )
        _vt = Table(
            v_rows, colWidths=[doc.content_width * 0.30, doc.content_width * 0.70]
        )
        _vt.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ]
            )
        )
        doc.story.append(_vt)
    else:
        doc.story.append(
            _p(
                "No vehicle or driver assigned yet. This will be updated once the "
                "fleet is confirmed for the trip.",
                _ps("no_v", 8.5, GREY_600),
            )
        )
    doc.story.append(Spacer(1, 5 * mm))

    # ── Current payment position ─────────────────────────────────────────────
    doc.hr()
    doc.section_h("PAYMENT DETAILS")
    doc.story.append(Spacer(1, 2 * mm))
    rows: list[list[Any]] = [
        [
            Paragraph("Description", _ps("ph1", 8, WHITE, bold=True)),
            Paragraph("Amount", _ps("ph2", 8, WHITE, bold=True, align=TA_RIGHT)),
        ],
        [
            Paragraph("Package Total", _ps("fi", 8.5, GREY_900)),
            Paragraph(
                f"Rs. {total_amount:,.2f}", _ps("fv", 8.5, GREY_900, align=TA_RIGHT)
            ),
        ],
    ]
    if additional_amount > 0:
        rows.append(
            [
                Paragraph(
                    "Additional charges (modifications)", _ps("fi", 8.5, GREY_900)
                ),
                Paragraph(
                    f"Rs. {additional_amount:,.2f}",
                    _ps("fv", 8.5, GREY_900, align=TA_RIGHT),
                ),
            ]
        )
    if advance_total > 0:
        rows.append(
            [
                Paragraph("Advance collected", _ps("fi", 8.5, SUCCESS)),
                Paragraph(
                    f"− Rs. {advance_total:,.2f}",
                    _ps("fv", 8.5, SUCCESS, align=TA_RIGHT),
                ),
            ]
        )
    total_row_idx = len(rows)
    rows.append(
        [
            Paragraph("TOTAL PAYABLE", _ps("gtl", 10, BRAND_DARK, bold=True)),
            Paragraph(
                f"Rs. {total_amount:,.2f}",
                _ps("gtv", 11, BRAND_DARK, bold=True, align=TA_RIGHT),
            ),
        ]
    )
    last = (
        Paragraph("Balance Due", _ps("bdl", 9, BRAND_GOLD, bold=True)),
        Paragraph(
            f"Rs. {max(balance_due, 0):,.2f}",
            _ps("bdv", 9, BRAND_GOLD, bold=True, align=TA_RIGHT),
        ),
    )
    _money_table(
        doc,
        rows,
        total_row=total_row_idx,
        last_highlight=last,
        last_bg=colors.HexColor("#fefce8"),
        last_line=BRAND_GOLD,
    )
    if invoice_number:
        doc.story.append(
            _p(
                f"Invoice: <b>{invoice_number}</b>  |  Payment status: <b>{(payment_status or '—').replace('_', ' ').title()}</b>",
                _ps("invn", 8, GREY_600),
            )
        )
        doc.story.append(Spacer(1, 3 * mm))

    # ── Thank you ────────────────────────────────────────────────────────────
    doc.story.append(Spacer(1, 3 * mm))
    doc.story.append(HRFlowable(width="100%", thickness=1, color=BRAND_ACCENT))
    doc.story.append(Spacer(1, 4 * mm))
    doc.story.append(
        _p(
            f"Thank you for choosing <b>{platform_name}</b>! This itinerary is your "
            "travel plan for the trip. Keep it handy, along with the booking "
            "reference, for check-in and pickup.",
            _ps("ty", 8, GREY_600, align=TA_CENTER),
        )
    )
    if support_email or support_phone:
        contact = "  |  ".join(filter(None, [support_email, support_phone]))
        doc.story.append(Spacer(1, 1.5 * mm))
        doc.story.append(
            _p(f"✉ {contact}", _ps("ct", 7.5, BRAND_MAIN, bold=True, align=TA_CENTER))
        )
    return doc.finish()
