# ============================================================
# WAYTERO — BOOKING CONFIRMATION & PAYMENT RECEIPT PDFS
# File: app/modules/admin/booking_pdf_service.py
#
# Purpose:
#   Customer-facing booking vouchers and payment receipts for cab and
#   hotel bookings. The visual family (palette, header/footer bands,
#   logo, section layout) mirrors invoice_pdf_service.py and
#   tour/services/pdf.py so every document the platform issues carries
#   one print identity.
#
#   Platform branding (logo, name, GST, address, support) is pulled from
#   system_configurations at render time, so Settings changes are
#   reflected in the next PDF without a deploy.
#
# Doc Ref:
#   BRD Part 3 §45 — Trip completion, invoice & customer documents
#   BRD Part 4 §57-92 — Hotel booking lifecycle
#   DB Schema Part 1 §12 — system_configurations
# ============================================================

from __future__ import annotations

import io
import logging
from datetime import datetime
from typing import Any
from xml.sax.saxutils import escape

import httpx
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    HRFlowable,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

log = logging.getLogger(__name__)

# ── Palette (matches invoice_pdf_service.py) ────────────────────────────────
BRAND_DARK = colors.HexColor("#0d1b2a")  # deep navy
BRAND_MAIN = colors.HexColor("#1565c0")  # WayTero blue
BRAND_ACCENT = colors.HexColor("#00b4d8")  # cyan accent
BRAND_GOLD = colors.HexColor("#f59e0b")  # amber for balances
GREY_100 = colors.HexColor("#f8fafc")
GREY_200 = colors.HexColor("#e2e8f0")
GREY_600 = colors.HexColor("#475569")
GREY_900 = colors.HexColor("#0f172a")
SUCCESS = colors.HexColor("#16a34a")
WHITE = colors.white

MARGIN = 18 * mm
CONTENT_WIDTH = A4[0] - (MARGIN * 2)


def _fetch_logo_bytes(url: str) -> bytes | None:
    """Fetch the platform logo with a short timeout; never fail the PDF."""
    if not url or not url.startswith("http"):
        return None
    try:
        resp = httpx.get(url, timeout=5)
        if resp.status_code == 200 and resp.content:
            return resp.content
    except Exception:  # pragma: no cover — branding must not block a document
        log.warning("Could not fetch platform logo for PDF", exc_info=True)
    return None


def _ps(
    name: str,
    size: float,
    color=GREY_900,
    bold: bool = False,
    align: int = TA_LEFT,
    line_height: float | None = None,
    space_after: int = 0,
) -> ParagraphStyle:
    return ParagraphStyle(
        name,
        fontSize=size,
        fontName="Helvetica-Bold" if bold else "Helvetica",
        textColor=color,
        alignment=align,
        leading=line_height or size * 1.45,
        spaceAfter=space_after,
    )


def _p(text: str, style: ParagraphStyle) -> Paragraph:
    return Paragraph(text, style)


def _e(value: Any) -> str:
    """Escape user-controlled text before handing it to ReportLab."""
    return escape(str(value)) if value not in (None, "") else "—"


def _fmt_dt(value: datetime | None) -> str:
    if not value:
        return "—"
    try:
        return value.strftime("%d %b %Y, %I:%M %p")
    except Exception:
        return str(value)


def _fmt_date(value: Any) -> str:
    if not value:
        return "—"
    if isinstance(value, datetime):
        return value.strftime("%d %b %Y")
    if hasattr(value, "strftime"):
        return value.strftime("%d %b %Y")
    return str(value)


def _money(value: float | None) -> str:
    if value is None:
        return "—"
    return f"Rs. {float(value):,.2f}"


def _amount_in_words(amount: float) -> str:
    """Indian-numbering amount in words, used on payment receipts."""
    from app.modules.admin.invoice_pdf_service import _amount_in_words as _words

    return _words(amount)


class _BrandedDoc:
    """Shared A4 shell: navy header band, logo, document identity, footer."""

    def __init__(
        self,
        *,
        title: str,
        doc_title: str,
        doc_number: str,
        platform_name: str,
        platform_logo_url: str,
        business_legal_name: str,
        business_gst_number: str,
        business_registered_address: str,
        support_email: str,
        support_phone: str,
    ) -> None:
        self.buf = io.BytesIO()
        self.platform_name = platform_name or "WayTero"
        self.support_email = support_email
        self.support_phone = support_phone
        self.doc_title = doc_title
        self.doc_number = doc_number
        self.logo = _fetch_logo_bytes(platform_logo_url)
        self.business_legal_name = business_legal_name
        self.business_gst_number = business_gst_number
        self.business_registered_address = business_registered_address
        self.story: list[Any] = []
        self.doc = BaseDocTemplate(
            self.buf,
            pagesize=A4,
            leftMargin=MARGIN,
            rightMargin=MARGIN,
            topMargin=40 * mm,
            bottomMargin=20 * mm,
            title=title,
            author=self.platform_name,
            creator=self.platform_name,
        )
        frame = Frame(
            MARGIN,
            20 * mm,
            CONTENT_WIDTH,
            A4[1] - (40 * mm) - (20 * mm),
            id="body",
            leftPadding=0,
            rightPadding=0,
            topPadding=0,
            bottomPadding=0,
        )
        self.doc.addPageTemplates(
            [PageTemplate(id="main", frames=[frame], onPage=self._draw_page)]
        )

    def _draw_page(self, canvas, doc) -> None:  # noqa: ANN001
        width, height = A4
        canvas.saveState()

        # Header band + accent stripe
        canvas.setFillColor(BRAND_DARK)
        canvas.rect(0, height - 32 * mm, width, 32 * mm, fill=1, stroke=0)
        canvas.setFillColor(BRAND_ACCENT)
        canvas.rect(0, height - 33.5 * mm, width, 1.5 * mm, fill=1, stroke=0)

        text_left = MARGIN
        if self.logo:
            try:
                canvas.drawImage(
                    ImageReader(io.BytesIO(self.logo)),
                    MARGIN,
                    height - 25 * mm,
                    width=18 * mm,
                    height=18 * mm,
                    preserveAspectRatio=True,
                    mask="auto",
                )
                text_left = MARGIN + 22 * mm
            except Exception:  # pragma: no cover — bad logo must not break PDF
                text_left = MARGIN

        canvas.setFillColor(WHITE)
        canvas.setFont("Helvetica-Bold", 14)
        canvas.drawString(text_left, height - 13.5 * mm, self.platform_name)

        canvas.setFont("Helvetica", 7.2)
        canvas.setFillColor(colors.HexColor("#cbd5e1"))
        identity = " | ".join(
            filter(
                None,
                [
                    self.business_legal_name,
                    (
                        f"GSTIN {self.business_gst_number}"
                        if self.business_gst_number
                        else ""
                    ),
                ],
            )
        )
        if identity:
            canvas.drawString(text_left, height - 18.5 * mm, identity[:120])
        if self.business_registered_address:
            canvas.drawString(
                text_left, height - 22.5 * mm, self.business_registered_address[:130]
            )

        # Right side document identity
        canvas.setFillColor(WHITE)
        canvas.setFont("Helvetica-Bold", 9)
        canvas.drawRightString(
            width - MARGIN, height - 13.5 * mm, self.doc_title.upper()
        )
        canvas.setFont("Helvetica-Bold", 8)
        canvas.setFillColor(BRAND_ACCENT)
        canvas.drawRightString(width - MARGIN, height - 19 * mm, self.doc_number)
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.HexColor("#cbd5e1"))
        canvas.drawRightString(
            width - MARGIN,
            height - 23.5 * mm,
            f"{self.platform_name} customer document",
        )

        # Footer
        canvas.setStrokeColor(GREY_200)
        canvas.setLineWidth(0.6)
        canvas.line(MARGIN, 16 * mm, width - MARGIN, 16 * mm)
        canvas.setFont("Helvetica", 6.8)
        canvas.setFillColor(GREY_600)
        contact = " | ".join(filter(None, [self.support_email, self.support_phone]))
        footer_left = self.platform_name
        if contact:
            footer_left = f"{footer_left}  |  {contact}"
        canvas.drawString(MARGIN, 11 * mm, footer_left[:135])
        canvas.drawRightString(
            width - MARGIN, 11 * mm, f"Page {canvas.getPageNumber()}"
        )
        canvas.restoreState()

    def heading(self, title: str, subtitle: str | None = None) -> None:
        self.story.append(Spacer(1, 1 * mm))
        self.story.append(
            _p(
                escape(title.upper()),
                _ps("doc_h", 14, BRAND_DARK, bold=True, space_after=1 * mm),
            )
        )
        if subtitle:
            self.story.append(
                _p(escape(subtitle), _ps("doc_sub", 8.5, GREY_600, space_after=3 * mm))
            )

    def section(self, title: str) -> None:
        self.story.append(Spacer(1, 3 * mm))
        self.story.append(HRFlowable(width="100%", thickness=0.6, color=GREY_200))
        self.story.append(Spacer(1, 1.2 * mm))
        self.story.append(
            _p(
                escape(title.upper()),
                _ps("section", 8, BRAND_MAIN, bold=True, space_after=1.5 * mm),
            )
        )

    def kv_table(self, rows: list[tuple[str, Any]], *, columns: int = 1) -> None:
        """Label/value rows. Pass columns=2 to halve the vertical space for
        long service-detail lists without shrinking the text.
        """
        if columns == 2:
            half = (len(rows) + 1) // 2
            left, right = rows[:half], rows[half:]
            body: list[list[Any]] = []
            for i in range(half):
                lrow = left[i] if i < len(left) else None
                rrow = right[i] if i < len(right) else None
                body.append(
                    [
                        (
                            _p(escape(str(lrow[0])), _ps("kl", 8, GREY_600))
                            if lrow
                            else _p("", _ps("kl", 8, GREY_600))
                        ),
                        (
                            _p(_e(lrow[1]), _ps("kv", 8.2, GREY_900))
                            if lrow
                            else _p("", _ps("kv", 8.2, GREY_900))
                        ),
                        (
                            _p(escape(str(rrow[0])), _ps("kl", 8, GREY_600))
                            if rrow
                            else _p("", _ps("kl", 8, GREY_600))
                        ),
                        (
                            _p(_e(rrow[1]), _ps("kv", 8.2, GREY_900))
                            if rrow
                            else _p("", _ps("kv", 8.2, GREY_900))
                        ),
                    ]
                )
            table = Table(
                body,
                colWidths=[
                    CONTENT_WIDTH * 0.16,
                    CONTENT_WIDTH * 0.34,
                    CONTENT_WIDTH * 0.16,
                    CONTENT_WIDTH * 0.34,
                ],
                hAlign="LEFT",
            )
            style: list[Any] = [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LINEBELOW", (0, 0), (-1, -2), 0.35, GREY_200),
            ]
            for i in range(len(body)):
                if i % 2 == 1:
                    style.append(("BACKGROUND", (0, i), (-1, i), GREY_100))
            table.setStyle(TableStyle(style))
            self.story.append(table)
            return

        body = []
        for label, value in rows:
            body.append(
                [
                    _p(escape(str(label)), _ps("kl", 8, GREY_600)),
                    _p(_e(value), _ps("kv", 8.4, GREY_900)),
                ]
            )
        table = Table(
            body,
            colWidths=[CONTENT_WIDTH * 0.28, CONTENT_WIDTH * 0.72],
            hAlign="LEFT",
        )
        style = [
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LINEBELOW", (0, 0), (-1, -2), 0.35, GREY_200),
        ]
        for i in range(len(body)):
            if i % 2 == 1:
                style.append(("BACKGROUND", (0, i), (-1, i), GREY_100))
        table.setStyle(TableStyle(style))
        self.story.append(table)

    def status_strip(
        self, status: str, booked_on: str | None = None, reference: str | None = None
    ) -> None:
        left = _p(
            f"<b>{escape(status.upper())}</b>",
            _ps("status", 8, BRAND_DARK, bold=True),
        )
        right_bits = []
        if reference:
            right_bits.append(f"Ref: <b>{escape(reference)}</b>")
        if booked_on:
            right_bits.append(f"Booked on <b>{escape(booked_on)}</b>")
        right = _p(
            "  |  ".join(right_bits) or "",
            _ps("status_r", 7.5, GREY_600, align=TA_RIGHT),
        )
        table = Table(
            [[left, right]], colWidths=[CONTENT_WIDTH * 0.35, CONTENT_WIDTH * 0.65]
        )
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), GREY_100),
                    ("BOX", (0, 0), (-1, -1), 0.5, GREY_200),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 7),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )
        self.story.append(table)

    def payment_table(
        self,
        rows: list[tuple[str, float | str]],
        *,
        total_label: str,
        total_amount: float,
        balance_due: float,
    ) -> None:
        body: list[list[Any]] = [
            [
                _p("Description", _ps("ph1", 7.5, WHITE, bold=True)),
                _p("Amount", _ps("ph2", 7.5, WHITE, bold=True, align=TA_RIGHT)),
            ]
        ]
        for label, amount in rows:
            body.append(
                [
                    _p(escape(str(label)), _ps("pl", 8.4, GREY_900)),
                    _p(
                        (
                            _money(float(amount))
                            if isinstance(amount, (int, float))
                            else escape(str(amount))
                        ),
                        _ps("pv", 8.4, GREY_900, align=TA_RIGHT),
                    ),
                ]
            )
        body.append(
            [
                _p(escape(total_label.upper()), _ps("tl", 9, WHITE, bold=True)),
                _p(
                    _money(total_amount),
                    _ps("tv", 10, WHITE, bold=True, align=TA_RIGHT),
                ),
            ]
        )
        body.append(
            [
                _p("Balance due", _ps("bl", 8.5, BRAND_GOLD, bold=True)),
                _p(
                    _money(max(balance_due, 0)),
                    _ps("bv", 9, BRAND_GOLD, bold=True, align=TA_RIGHT),
                ),
            ]
        )
        table = Table(body, colWidths=[CONTENT_WIDTH * 0.68, CONTENT_WIDTH * 0.32])
        style = [
            ("BACKGROUND", (0, 0), (-1, 0), BRAND_DARK),
            ("BACKGROUND", (0, len(body) - 2), (-1, len(body) - 2), BRAND_MAIN),
            (
                "BACKGROUND",
                (0, len(body) - 1),
                (-1, len(body) - 1),
                colors.HexColor("#fefce8"),
            ),
            ("BOX", (0, 0), (-1, -1), 0.5, GREY_200),
            ("INNERGRID", (0, 0), (-1, -1), 0.35, GREY_200),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 7),
            ("RIGHTPADDING", (0, 0), (-1, -1), 7),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]
        for i in range(1, len(body) - 2):
            if i % 2 == 0:
                style.append(("BACKGROUND", (0, i), (-1, i), GREY_100))
        table.setStyle(TableStyle(style))
        self.story.append(table)

    def notes(self, title: str, items: list[str]) -> None:
        self.section(title)
        for item in items:
            self.story.append(
                _p(f"-  {escape(item)}", _ps("note", 8, GREY_600, space_after=0.9 * mm))
            )

    def closing(self, message: str) -> None:
        self.story.append(Spacer(1, 4 * mm))
        self.story.append(HRFlowable(width="100%", thickness=1, color=BRAND_ACCENT))
        self.story.append(Spacer(1, 2 * mm))
        self.story.append(
            _p(escape(message), _ps("close", 7.5, GREY_600, align=TA_CENTER))
        )
        contact = "  |  ".join(filter(None, [self.support_email, self.support_phone]))
        if contact:
            self.story.append(Spacer(1, 1.5 * mm))
            self.story.append(
                _p(
                    escape(contact),
                    _ps("contact", 7.5, BRAND_MAIN, bold=True, align=TA_CENTER),
                )
            )

    def finish(self) -> bytes:
        self.doc.build(self.story)
        return self.buf.getvalue()


def generate_booking_confirmation_pdf(
    *,
    service_label: str,
    booking_number: str,
    service_booking_number: str | None,
    booking_status: str | None,
    booked_at: datetime | None,
    customer_name: str | None,
    customer_mobile: str | None,
    customer_email: str | None,
    details: list[tuple[str, Any]],
    payment_lines: list[tuple[str, float]],
    total_label: str,
    total_amount: float,
    balance_due: float,
    notes: list[str],
    platform_name: str = "WayTero",
    platform_logo_url: str = "",
    business_legal_name: str = "",
    business_gst_number: str = "",
    business_registered_address: str = "",
    support_email: str = "",
    support_phone: str = "",
) -> bytes:
    """Build a premium A4 booking confirmation/voucher as raw PDF bytes."""
    doc = _BrandedDoc(
        title=f"{service_label} Booking Confirmation",
        doc_title="BOOKING CONFIRMATION",
        doc_number=service_booking_number or booking_number,
        platform_name=platform_name,
        platform_logo_url=platform_logo_url,
        business_legal_name=business_legal_name,
        business_gst_number=business_gst_number,
        business_registered_address=business_registered_address,
        support_email=support_email,
        support_phone=support_phone,
    )
    doc.heading(
        f"{service_label} booking confirmation",
        f"Booking {booking_number}"
        + (f"  |  {service_booking_number}" if service_booking_number else ""),
    )
    doc.status_strip(
        (booking_status or "CONFIRMED").replace("_", " "),
        booked_on=booked_at.strftime("%d %b %Y") if booked_at else None,
        reference=service_booking_number,
    )

    doc.section("Customer")
    doc.kv_table(
        [
            ("Name", customer_name or "—"),
            ("Mobile", customer_mobile or "—"),
            ("Email", customer_email or "—"),
        ]
    )

    doc.section("Service details")
    doc.kv_table(details, columns=2)

    doc.section("Payment summary")
    doc.payment_table(
        payment_lines,
        total_label=total_label,
        total_amount=total_amount,
        balance_due=balance_due,
    )

    doc.notes(
        "Important information",
        notes
        + [
            "This is a booking confirmation, not a tax invoice. A GST invoice is "
            "issued after the service is completed and paid.",
            f"Keep booking reference {service_booking_number or booking_number} "
            "handy for check-in, pickup, or support.",
        ],
    )
    doc.closing(
        f"Thank you for choosing {platform_name}. We look forward to serving you."
    )
    return doc.finish()


def generate_payment_receipt_pdf(
    *,
    receipt_number: str,
    booking_number: str,
    service_booking_number: str | None,
    payment_date: datetime | None,
    customer_name: str | None,
    customer_mobile: str | None,
    service_label: str,
    amount: float,
    payment_mode: str | None,
    received_by: str | None,
    reference_number: str | None,
    payment_notes: str | None,
    total_amount: float,
    total_paid: float,
    balance_due: float,
    platform_name: str = "WayTero",
    platform_logo_url: str = "",
    business_legal_name: str = "",
    business_gst_number: str = "",
    business_registered_address: str = "",
    support_email: str = "",
    support_phone: str = "",
) -> bytes:
    """Build a premium A4 payment receipt as raw PDF bytes."""
    doc = _BrandedDoc(
        title=f"Payment Receipt {receipt_number}",
        doc_title="PAYMENT RECEIPT",
        doc_number=receipt_number,
        platform_name=platform_name,
        platform_logo_url=platform_logo_url,
        business_legal_name=business_legal_name,
        business_gst_number=business_gst_number,
        business_registered_address=business_registered_address,
        support_email=support_email,
        support_phone=support_phone,
    )
    doc.heading(
        "Payment received",
        f"{service_label}  |  {service_booking_number or booking_number}",
    )
    doc.status_strip(
        "PAYMENT RECEIVED",
        booked_on=payment_date.strftime("%d %b %Y") if payment_date else None,
        reference=receipt_number,
    )

    amount_table = Table(
        [
            [
                _p(
                    "AMOUNT RECEIVED",
                    _ps("al", 8, GREY_600, bold=True, align=TA_CENTER),
                )
            ],
            [
                _p(
                    _money(amount),
                    _ps("av", 18, BRAND_DARK, bold=True, align=TA_CENTER),
                )
            ],
            [
                _p(
                    _amount_in_words(amount),
                    _ps("aw", 7.5, GREY_600, align=TA_CENTER),
                )
            ],
        ],
        colWidths=[CONTENT_WIDTH],
    )
    amount_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), GREY_100),
                ("BOX", (0, 0), (-1, -1), 0.6, BRAND_ACCENT),
                ("TOPPADDING", (0, 0), (-1, 0), 8),
                ("BOTTOMPADDING", (0, -1), (-1, -1), 8),
                ("TOPPADDING", (0, 1), (-1, 1), 1),
                ("BOTTOMPADDING", (0, 1), (-1, 1), 1),
            ]
        )
    )
    doc.story.append(amount_table)

    doc.section("Customer")
    doc.kv_table(
        [
            ("Name", customer_name or "—"),
            ("Mobile", customer_mobile or "—"),
        ]
    )

    doc.section("Payment details")
    doc.kv_table(
        [
            ("Receipt number", receipt_number),
            ("Payment date", _fmt_dt(payment_date)),
            ("Payment mode", (payment_mode or "—").replace("_", " ")),
            ("Received by", (received_by or "—").replace("_", " ")),
            ("Reference / UTR", reference_number or "—"),
            ("Notes", payment_notes or "—"),
        ],
        columns=2,
    )

    doc.section("Booking position")
    doc.kv_table(
        [
            ("Master booking", booking_number),
            ("Service booking", service_booking_number or "—"),
            ("Service", service_label),
        ]
    )
    doc.payment_table(
        [("Total service amount", total_amount), ("Total paid", total_paid)],
        total_label="Amount received",
        total_amount=amount,
        balance_due=balance_due,
    )
    doc.notes(
        "Receipt notes",
        [
            "This receipt acknowledges money received against the booking above.",
            "It is a payment acknowledgement, not a tax invoice. The GST invoice "
            "is issued separately after service completion.",
        ],
    )
    doc.closing(
        f"Thank you for choosing {platform_name}. Please retain this receipt for your records."
    )
    return doc.finish()
