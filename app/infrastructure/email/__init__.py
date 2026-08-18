# ============================================================
# WAYTERO — EMAIL ENGINE
# File: app/infrastructure/email/__init__.py
#
# A production-grade email sender with:
#   * SMTP configuration stored in `system_configurations`
#     (admin edits it under Settings → Email; nothing is sent
#     until EMAIL_ENABLED=true and SMTP credentials exist).
#   * A premium branded HTML shell — logo + platform name from
#     the platform-profile keys (PLATFORM_LOGO_URL etc.), gradient
#     hero, details card, CTA button, support footer.
#   * Every attempt written to `email_logs` (SENT / FAILED with
#     the error and the full render payload) so the admin portal
#     can list emails and resend failures.
#   * stdlib `smtplib` run via asyncio.to_thread — no new
#     dependency; failures are logged, never raised to callers
#     (an email must never break a booking).
#
# Usage (from any endpoint/service):
#     from app.infrastructure.email import send_event_email
#     await send_event_email(db, event_type="booking_confirmed",
#         to_email="x@y.com", to_name="Anita", context={...},
#         related_type="TOUR_BOOKING", related_id=tour.id)
# ============================================================

from __future__ import annotations

import asyncio
import html
import json
import logging
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from typing import Any, Dict, Optional, Tuple

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("waytero.email")

# ── Config keys in system_configurations ──────────────────────────
CONFIG_KEYS = (
    "EMAIL_ENABLED",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USER",
    "SMTP_PASSWORD",
    "SMTP_FROM_EMAIL",
    "SMTP_FROM_NAME",
    "SMTP_USE_TLS",
)
BRAND_KEYS = (
    "PLATFORM_NAME",
    "PLATFORM_LOGO_URL",
    "SUPPORT_EMAIL",
    "SUPPORT_PHONE",
    "COMPANY_ADDRESS",
    "PLATFORM_WEB_URL",
)


# ── Event registry ────────────────────────────────────────────────
# Each event has: label (UI), subject template, tone (accent hex) and
# an optional CTA. The body is assembled from generic blocks fed by the
# caller's context — no per-event HTML code needed.
class EventSpec:
    def __init__(
        self,
        label: str,
        subject: str,
        tone: str = "#1A56DB",
        cta_label: str = "",
        cta_url: str = "",
    ):
        self.label = label
        self.subject = subject
        self.tone = tone
        self.cta_label = cta_label
        self.cta_url = cta_url


EVENT_TEMPLATES: Dict[str, EventSpec] = {
    "booking_requested": EventSpec(
        "Booking Requested",
        "Your {service} booking {booking_number} has been received",
        tone="#1A56DB",
        cta_label="View my booking",
        cta_url="{web_url}/bookings",
    ),
    "booking_confirmed": EventSpec(
        "Booking Confirmed",
        "Your {service} booking {booking_number} is confirmed",
        tone="#0F9D58",
        cta_label="View my booking",
        cta_url="{web_url}/bookings",
    ),
    "booking_completed": EventSpec(
        "Trip Completed",
        "Your {service} trip {booking_number} is complete — thank you!",
        tone="#1A56DB",
        cta_label="Download invoice",
        cta_url="{web_url}/bookings",
    ),
    "booking_cancelled": EventSpec(
        "Booking Cancelled",
        "Your {service} booking {booking_number} has been cancelled",
        tone="#D64545",
    ),
    "refund_processed": EventSpec(
        "Refund Processed",
        "Refund of ₹{amount} credited to your WayTero Wallet",
        tone="#0F9D58",
        cta_label="View wallet",
        cta_url="{web_url}/wallet",
    ),
    "invoice_issued": EventSpec(
        "Invoice Issued",
        "Your tax invoice for {booking_number} is ready",
        tone="#1A56DB",
        cta_label="View invoice",
        cta_url="{web_url}/bookings",
    ),
    "payment_received": EventSpec(
        "Payment Received",
        "Payment of ₹{amount} received for {booking_number}",
        tone="#0F9D58",
        cta_label="View booking",
        cta_url="{web_url}/bookings",
    ),
    "customer_registered": EventSpec(
        "Welcome",
        "Welcome to WayTero, {name}!",
        tone="#1A56DB",
        cta_label="Explore trips",
        cta_url="{web_url}/cabs",
    ),
    "partner_registered": EventSpec(
        "Partner Registration",
        "Your partner application has been received",
        tone="#F05A22",
    ),
    "driver_registered": EventSpec(
        "Driver Registration",
        "Your driver application has been received",
        tone="#F05A22",
    ),
    "wallet_recharged": EventSpec(
        "Wallet Recharged",
        "₹{amount} added to your {wallet_type} wallet",
        tone="#0F9D58",
        cta_label="View wallet",
        cta_url="{web_url}/wallet",
    ),
    "partner_settlement": EventSpec(
        "Settlement Completed",
        "Settlement of ₹{amount} completed for {period}",
        tone="#0F9D58",
    ),
    "password_reset_otp": EventSpec(
        "Password Reset",
        "Your WayTero password reset OTP",
        tone="#D64545",
    ),
    "test_email": EventSpec(
        "Test Email",
        "WayTero test email — SMTP configuration works",
        tone="#1A56DB",
    ),
}

EVENT_LABELS: Dict[str, str] = {k: v.label for k, v in EVENT_TEMPLATES.items()}


# ── Config loading / saving ───────────────────────────────────────


async def _read_keys(db: AsyncSession, keys: Tuple[str, ...]) -> Dict[str, str]:
    stmt = text(
        "SELECT config_key, config_value FROM system_configurations "
        "WHERE config_key IN :keys"
    ).bindparams(bindparam("keys", expanding=True))
    rows = (await db.execute(stmt, {"keys": list(keys)})).mappings().all()
    return {r["config_key"]: (r["config_value"] or "") for r in rows}


async def load_config(db: AsyncSession) -> Dict[str, Any]:
    """Merge SMTP + branding keys into one dict (raw, password unmasked).

    SMTP credentials are read from the active SMTP row in `api_integrations`
    (Settings → API Integrations → SMTP in the admin portal) when present —
    that's where admins configure Gmail etc. Falls back to the legacy
    `system_configurations` keys (Settings → Email tab) otherwise.
    Branding (logo, support info) always comes from `system_configurations`.
    """
    cfg = await _read_keys(db, CONFIG_KEYS + BRAND_KEYS)

    enabled = cfg.get("EMAIL_ENABLED", "").strip().lower() == "true"
    host = cfg.get("SMTP_HOST", "").strip() or "smtp.gmail.com"
    port = int(str(cfg.get("SMTP_PORT", "587")).strip() or 587)
    user = cfg.get("SMTP_USER", "").strip()
    password = cfg.get("SMTP_PASSWORD", "")
    from_email = cfg.get("SMTP_FROM_EMAIL", "").strip() or "noreply@waytero.com"
    from_name = cfg.get("SMTP_FROM_NAME", "").strip() or "WayTero"
    use_tls = cfg.get("SMTP_USE_TLS", "").strip().lower() != "false"

    # An active SMTP integration row is the source of truth: it also flips
    # emailing ON (the integrations card's "Active" badge is the admin's
    # intent to send).
    try:
        row = (
            (
                await db.execute(
                    text(
                        "SELECT configuration FROM api_integrations "
                        "WHERE service_type = 'SMTP' AND is_active = TRUE "
                        "ORDER BY id LIMIT 1"
                    )
                )
            )
            .mappings()
            .first()
        )
        if row:
            smtp = row["configuration"] or {}
            enabled = True
            host = str(smtp.get("host") or host).strip() or "smtp.gmail.com"
            port = int(str(smtp.get("port") or port).strip() or 587)
            user = str(smtp.get("username") or user).strip()
            password = str(smtp.get("password") or password)
            from_email = str(smtp.get("from_email") or from_email).strip() or from_email
    except Exception:  # pragma: no cover — config load must never break emailing
        logger.warning(
            "email.load_config SMTP integration lookup failed", exc_info=True
        )

    return {
        "enabled": enabled,
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "from_email": from_email,
        "from_name": from_name,
        "use_tls": use_tls,
        # Branding
        "platform_name": cfg.get("PLATFORM_NAME", "").strip() or "WayTero",
        "logo_url": cfg.get("PLATFORM_LOGO_URL", "").strip(),
        "support_email": cfg.get("SUPPORT_EMAIL", "").strip() or "support@waytero.com",
        "support_phone": cfg.get("SUPPORT_PHONE", "").strip() or "",
        "company_address": cfg.get("COMPANY_ADDRESS", "").strip() or "",
        "web_url": (
            cfg.get("PLATFORM_WEB_URL", "").strip().rstrip("/")
            or "https://www.waytero.com"
        ),
    }


async def save_config(db: AsyncSession, values: Dict[str, Any]) -> Dict[str, str]:
    """Upsert the SMTP settings. Passwords are stored as-is; an empty
    password field on save is ignored (keeps the existing secret)."""
    key_map = {
        "enabled": "EMAIL_ENABLED",
        "host": "SMTP_HOST",
        "port": "SMTP_PORT",
        "user": "SMTP_USER",
        "password": "SMTP_PASSWORD",
        "from_email": "SMTP_FROM_EMAIL",
        "from_name": "SMTP_FROM_NAME",
        "use_tls": "SMTP_USE_TLS",
    }
    for field, key in key_map.items():
        if field not in values:
            continue
        if field == "password":
            val = str(values[field] or "").strip()
            # Blank, or the masked placeholder returned by public_config
            # (the admin UI echoes it back on every save) → keep the secret.
            if val == "" or val == "********":
                continue
        await db.execute(
            text(
                """
                INSERT INTO system_configurations (config_key, config_value, updated_at)
                VALUES (:k, :v, NOW())
                ON CONFLICT (config_key) DO UPDATE SET config_value = :v, updated_at = NOW()
                """
            ),
            {"k": key, "v": str(values[field])},
        )
    await db.commit()
    return await public_config(db)


async def public_config(db: AsyncSession) -> Dict[str, Any]:
    """Config safe to return to the admin UI — password masked."""
    cfg = await load_config(db)
    return {
        "enabled": cfg["enabled"],
        "host": cfg["host"],
        "port": cfg["port"],
        "user": cfg["user"],
        "password_set": bool(cfg["password"]),
        "password": "********" if cfg["password"] else "",
        "from_email": cfg["from_email"],
        "from_name": cfg["from_name"],
        "use_tls": cfg["use_tls"],
        "configured": bool(cfg["host"] and cfg["user"] and cfg["password"]),
        "platform_name": cfg["platform_name"],
        "logo_url": cfg["logo_url"],
        "support_email": cfg["support_email"],
        "support_phone": cfg["support_phone"],
        "web_url": cfg["web_url"],
    }


# ── Rendering ─────────────────────────────────────────────────────


def _esc(value: Any) -> str:
    return html.escape(str(value) if value is not None else "")


def _inr(value: Any) -> str:
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return _esc(value)


def _fill(template: str, ctx: Dict[str, Any], cfg: Dict[str, str]) -> str:
    merged = {**ctx, "name": ctx.get("name") or ctx.get("to_name") or "there"}
    merged.setdefault("service", "WayTero")
    merged.setdefault("web_url", cfg["web_url"])
    out = template
    for key, value in merged.items():
        out = out.replace("{" + key + "}", str(value if value is not None else ""))
    return out


def _details_rows(ctx: Dict[str, Any]) -> str:
    """Render ctx['details'] — a list of (label, value) tuples or dicts."""
    rows = ctx.get("details") or []
    if not rows:
        return ""
    cells = []
    for row in rows:
        if isinstance(row, dict):
            label, value = row.get("label", ""), row.get("value", "")
        else:
            label, value = row
        if value in (None, ""):
            continue
        cells.append(
            f'<tr><td style="padding:7px 0;font-size:13px;color:#64748B;">'
            f"{_esc(label)}</td>"
            f'<td style="padding:7px 0;font-size:13px;font-weight:600;color:#0F172A;'
            f'text-align:right;">{_esc(value)}</td></tr>'
        )
    if not cells:
        return ""
    return (
        '<table width="100%" cellpadding="0" cellspacing="0" '
        'style="margin:18px 0;border-top:1px solid #E2E8F0;">'
        + "".join(cells)
        + "</table>"
    )


def _summary_card(ctx: Dict[str, Any], cfg: Dict[str, str]) -> str:
    """Optional big amount card for financial emails."""
    amount = ctx.get("amount")
    if amount is None:
        return ""
    sub = _esc(ctx.get("amount_label") or "Amount")
    return (
        '<div style="background:linear-gradient(135deg,#0B1B3B,#12326B);'
        'border-radius:14px;padding:18px 22px;margin:18px 0;color:#fff;">'
        f'<div style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;'
        f'color:#93C5FD;">{sub}</div>'
        f'<div style="font-size:30px;font-weight:800;margin-top:4px;">'
        f"₹{_inr(amount)}</div>"
        f'<div style="font-size:12px;color:#BFDBFE;margin-top:6px;">'
        f"{_esc(ctx.get('amount_note') or '')}</div></div>"
    )


def _cta_button(spec: EventSpec, ctx: Dict[str, Any], cfg: Dict[str, str]) -> str:
    label = _fill(spec.cta_label, ctx, cfg)
    url = _fill(spec.cta_url, ctx, cfg)
    if not label or not url:
        return ""
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" '
        'style="margin:22px 0;"><tr><td>'
        f'<a href="{_esc(url)}" '
        'style="display:inline-block;background:linear-gradient(135deg,#1A56DB,#0B1B3B);'
        "color:#fff;text-decoration:none;font-weight:700;font-size:14px;"
        'padding:13px 30px;border-radius:10px;">'
        f"{_esc(label)}</a></td></tr></table>"
    )


def _hero(spec: EventSpec, ctx: Dict[str, Any], cfg: Dict[str, str]) -> str:
    title = _fill(spec.subject, ctx, cfg).split("!")[0] + "!"
    subtitle = _esc(ctx.get("message") or "")
    return (
        '<div style="background:linear-gradient(135deg,#0B1B3B 0%,#12326B 60%,#1A56DB 130%);'
        'border-radius:16px;padding:28px 30px;color:#fff;margin-bottom:22px;">'
        f'<div style="font-size:11px;letter-spacing:.14em;text-transform:uppercase;'
        f'color:#93C5FD;margin-bottom:8px;">{_esc(spec.label)}</div>'
        f'<h1 style="margin:0;font-size:22px;line-height:1.35;font-weight:800;">'
        f"{_esc(title)}</h1>"
        f'<p style="margin:10px 0 0;font-size:14px;line-height:1.6;color:#DBEAFE;">'
        f"{subtitle}</p></div>"
    )


def _brand_shell(
    subject: str,
    hero_html: str,
    body_html: str,
    cfg: Dict[str, str],
) -> str:
    logo = cfg["logo_url"]
    logo_html = (
        f'<img src="{_esc(logo)}" alt="{_esc(cfg["platform_name"])}" '
        'style="max-height:38px;max-width:160px;height:auto;border:0;" />'
        if logo
        else (
            f'<div style="font-size:20px;font-weight:800;color:#0B1B3B;">'
            f'{_esc(cfg["platform_name"])}</div>'
        )
    )
    # Plain text labels only — emoji in HTML email is a spam-filter trigger.
    footer_contact = []
    if cfg["support_phone"]:
        footer_contact.append(f"Phone: {_esc(cfg['support_phone'])}")
    if cfg["support_email"]:
        footer_contact.append(f"Email: {_esc(cfg['support_email'])}")
    footer_line = " &nbsp;·&nbsp; ".join(footer_contact)
    address = _esc(cfg["company_address"])

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8" /><meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{_esc(subject)}</title></head>
<body style="margin:0;padding:0;background:#F1F5F9;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#F1F5F9;padding:24px 12px;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0"
        style="max-width:600px;width:100%;background:#ffffff;border-radius:18px;
        overflow:hidden;border:1px solid #E2E8F0;">
        <!-- Header -->
        <tr><td style="padding:22px 30px;border-bottom:1px solid #EEF2F7;">
          <table width="100%" cellpadding="0" cellspacing="0"><tr>
            <td>{logo_html}</td>
            <td align="right" style="font-size:11px;color:#94A3B8;letter-spacing:.06em;">
              {_esc(cfg["platform_name"])} · Travel OS</td>
          </tr></table>
        </td></tr>
        <!-- Body -->
        <tr><td style="padding:28px 30px 8px;">
          {hero_html}
          <div style="font-size:14px;line-height:1.7;color:#334155;">{body_html}</div>
          <p style="font-size:12px;color:#94A3B8;margin-top:24px;line-height:1.6;">
            This is an automated email from {_esc(cfg["platform_name"])}. If you didn't
            expect this, you can safely ignore it. Never share your OTP or password with anyone.</p>
        </td></tr>
        <!-- Footer -->
        <tr><td style="padding:20px 30px;background:#0B1B3B;">
          <div style="font-size:12px;color:#93C5FD;font-weight:700;">{_esc(cfg["platform_name"])}</div>
          <div style="font-size:12px;color:#BFDBFE;margin-top:6px;line-height:1.6;">
            {footer_line}{'<br/>' if footer_line and address else ''}{address}
          </div>
          <div style="font-size:11px;color:#7C9CC9;margin-top:10px;">
            © {cfg["platform_name"]} · All rights reserved</div>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""


def render_email(
    event_type: str,
    context: Dict[str, Any],
    cfg: Dict[str, str],
) -> Tuple[str, str]:
    """Return (subject, html_body). Never raises — falls back to a generic mail."""
    spec = EVENT_TEMPLATES.get(event_type, EVENT_TEMPLATES["test_email"])
    subject = _fill(spec.subject, context, cfg)
    ctx = dict(context)
    ctx.setdefault("name", context.get("to_name") or "there")
    ctx.setdefault("service", "WayTero")
    ctx.setdefault("web_url", cfg["web_url"])
    message = " ".join(
        p for p in [context.get("message"), *context.get("body", [])] if p
    )[:400]
    ctx["message"] = message

    hero = _hero(spec, ctx, cfg)
    paragraphs = []
    for block in context.get("body") or []:
        paragraphs.append(f'<p style="margin:0 0 12px;">{_esc(block)}</p>')
    body_html = (
        "".join(paragraphs)
        + _details_rows(ctx)
        + _summary_card(ctx, cfg)
        + _cta_button(spec, ctx, cfg)
    )
    return subject, _brand_shell(subject, hero, body_html, cfg)


# ── Sending ───────────────────────────────────────────────────────


def _smtp_send(
    cfg: Dict[str, Any],
    to_email: str,
    subject: str,
    html_body: str,
) -> None:
    """Blocking SMTP send (run inside asyncio.to_thread)."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((cfg["from_name"], cfg["from_email"]))
    msg["To"] = to_email
    # ── Deliverability headers — Gmail/spam filters score these heavily ──
    # A valid Message-ID + Date prove the mail is a real transactional send,
    # Reply-To keeps replies out of the unmonitored from-box, and X-Mailer
    # identifies the sender. Missing headers push mail toward spam.
    from_domain = (cfg.get("from_email") or "").split("@")[-1] or "waytero.com"
    msg["Message-ID"] = make_msgid(domain=from_domain)
    msg["Date"] = formatdate(localtime=True)
    reply_to = (cfg.get("support_email") or "").strip()
    if reply_to and reply_to != cfg.get("from_email"):
        msg["Reply-To"] = reply_to
    msg["X-Mailer"] = "WayTero"
    # Plain-text fallback for strict clients
    import re as _re

    plain = _re.sub(r"<[^>]+>", " ", html_body)
    plain = _re.sub(r"\s+", " ", plain).strip()
    msg.set_content(plain[:2000])
    msg.add_alternative(html_body, subtype="html")

    if cfg["port"] == 465:
        server: smtplib.SMTP = smtplib.SMTP_SSL(
            cfg["host"],
            cfg["port"],
            timeout=25,
            context=ssl.create_default_context(),
        )
    else:
        server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=25)
        server.ehlo()
        if cfg["use_tls"]:
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
    try:
        if cfg["user"]:
            server.login(cfg["user"], cfg["password"])
        server.sendmail(cfg["from_email"], [to_email], msg.as_string())
    finally:
        try:
            server.quit()
        except Exception:
            pass


async def _write_log(
    db: AsyncSession,
    *,
    event_type: str,
    to_email: str,
    to_name: Optional[str],
    subject: str,
    status: str,
    error: Optional[str],
    attempt: int,
    payload: Optional[Dict[str, Any]],
    related_type: Optional[str],
    related_id: Optional[Any],
) -> int:
    row = (
        await db.execute(
            text(
                """
                INSERT INTO email_logs
                    (event_type, recipient, recipient_name, subject, status,
                     error_message, attempt_count, payload, related_type,
                     related_id, sent_at, created_at, updated_at)
                VALUES (:evt, :to, :name, :subj, :st, :err, :att, :payload,
                        :rtype, :rid, :sent_at, NOW(), NOW())
                RETURNING id
                """
            ),
            {
                "evt": event_type,
                "to": to_email,
                "name": to_name,
                "subj": subject[:500],
                "st": status,
                "err": error[:2000] if error else None,
                "att": attempt,
                "payload": json.dumps(payload) if payload is not None else None,
                "rtype": related_type,
                "rid": str(related_id) if related_id is not None else None,
                "sent_at": datetime.now(timezone.utc) if status == "SENT" else None,
            },
        )
    ).first()
    await db.commit()
    if row is None:
        return 0
    return int(row[0])


async def send_event_email(
    db: AsyncSession,
    *,
    event_type: str,
    to_email: str,
    to_name: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
    related_type: Optional[str] = None,
    related_id: Optional[Any] = None,
) -> Optional[int]:
    """Fire-and-forget event email. Never raises — failures are logged to
    email_logs so the admin can fix config and resend. Returns the log id
    (or None when emailing is disabled / no recipient)."""
    to_email = (to_email or "").strip()
    if not to_email or "@" not in to_email:
        return None
    ctx = dict(context or {})
    ctx.setdefault("name", to_name)
    try:
        cfg = await load_config(db)
        if not cfg["enabled"]:
            return None  # disabled → no attempt, no log noise
        if not (cfg["host"] and cfg["user"] and cfg["password"]):
            return await _write_log(
                db,
                event_type=event_type,
                to_email=to_email,
                to_name=to_name,
                subject=_fill(
                    EVENT_TEMPLATES.get(
                        event_type, EVENT_TEMPLATES["test_email"]
                    ).subject,
                    ctx,
                    cfg,
                ),
                status="FAILED",
                error="SMTP not configured — save credentials in Settings → Email",
                attempt=1,
                payload=ctx,
                related_type=related_type,
                related_id=related_id,
            )
        subject, html_body = await asyncio.to_thread(render_email, event_type, ctx, cfg)
        await asyncio.to_thread(_smtp_send, cfg, to_email, subject, html_body)
        return await _write_log(
            db,
            event_type=event_type,
            to_email=to_email,
            to_name=to_name,
            subject=subject,
            status="SENT",
            error=None,
            attempt=1,
            payload=ctx,
            related_type=related_type,
            related_id=related_id,
        )
    except Exception as exc:  # never break the caller
        logger.warning(
            "email.send_failed event=%s to=%s err=%s", event_type, to_email, exc
        )
        try:
            subject = _fill(
                EVENT_TEMPLATES.get(event_type, EVENT_TEMPLATES["test_email"]).subject,
                ctx,
                await load_config(db),
            )
        except Exception:
            subject = EVENT_TEMPLATES.get(
                event_type, EVENT_TEMPLATES["test_email"]
            ).subject
        try:
            return await _write_log(
                db,
                event_type=event_type,
                to_email=to_email,
                to_name=to_name,
                subject=subject,
                status="FAILED",
                error=f"{type(exc).__name__}: {exc}",
                attempt=1,
                payload=ctx,
                related_type=related_type,
                related_id=related_id,
            )
        except Exception as log_exc:  # pragma: no cover
            logger.error("email.log_failed err=%s", log_exc)
            return None


async def send_test_email(db: AsyncSession, to_email: str) -> Dict[str, Any]:
    """Admin 'Send test email' — raises HTTPException-friendly errors so the
    settings UI can surface SMTP problems immediately."""
    cfg = await load_config(db)
    if not cfg["enabled"]:
        raise RuntimeError("Emailing is disabled. Enable it in Settings → Email first.")
    if not (cfg["host"] and cfg["user"] and cfg["password"]):
        raise RuntimeError(
            "SMTP is not fully configured. Fill host, username and password first."
        )
    ctx = {
        "name": "WayTero Team",
        "message": "This is a test email to confirm your SMTP settings are working. "
        "If you can read this, every transactional email will deliver.",
        "body": [
            "Your SMTP host, credentials and TLS settings were validated successfully.",
            "Transactional emails — bookings, invoices, refunds, settlements and wallet "
            "updates — are now ready to send to customers and partners.",
        ],
        "details": [("Environment", "Development"), ("Sent at", "Now")],
    }
    subject, html_body = await asyncio.to_thread(render_email, "test_email", ctx, cfg)
    try:
        await asyncio.to_thread(_smtp_send, cfg, to_email, subject, html_body)
    except Exception as exc:
        raise RuntimeError(f"SMTP send failed: {type(exc).__name__}: {exc}")
    log_id = await _write_log(
        db,
        event_type="test_email",
        to_email=to_email,
        to_name="WayTero Team",
        subject=subject,
        status="SENT",
        error=None,
        attempt=1,
        payload=ctx,
        related_type=None,
        related_id=None,
    )
    return {
        "success": True,
        "message": f"Test email sent to {to_email}",
        "log_id": log_id,
    }


async def resend_email(db: AsyncSession, log_id: int) -> Dict[str, Any]:
    """Re-render a logged email from its stored payload and send again.
    Updates the log row (new attempt count / status / error)."""
    row = (
        (
            await db.execute(
                text(
                    "SELECT id, event_type, recipient, recipient_name, subject, "
                    "payload, attempt_count FROM email_logs WHERE id = :id"
                ),
                {"id": log_id},
            )
        )
        .mappings()
        .first()
    )
    if not row:
        raise LookupError(f"Email log #{log_id} not found")

    cfg = await load_config(db)
    if not cfg["enabled"]:
        raise RuntimeError("Emailing is disabled. Enable it in Settings → Email first.")
    if not (cfg["host"] and cfg["user"] and cfg["password"]):
        raise RuntimeError(
            "SMTP is not fully configured. Fill host, username and password first."
        )

    ctx = dict(row["payload"] or {})
    to_email = row["recipient"]
    event_type = row["event_type"] or "test_email"
    attempts = int(row["attempt_count"] or 0) + 1

    try:
        subject, html_body = await asyncio.to_thread(render_email, event_type, ctx, cfg)
        await asyncio.to_thread(_smtp_send, cfg, to_email, subject, html_body)
        await db.execute(
            text(
                """
                UPDATE email_logs
                   SET status = 'SENT', error_message = NULL,
                       attempt_count = :att, sent_at = NOW(), updated_at = NOW()
                 WHERE id = :id
                """
            ),
            {"att": attempts, "id": log_id},
        )
        await db.commit()
        return {
            "success": True,
            "message": f"Email re-sent to {to_email}",
            "log_id": log_id,
        }
    except Exception as exc:
        await db.execute(
            text(
                """
                UPDATE email_logs
                   SET status = 'FAILED', error_message = :err,
                       attempt_count = :att, updated_at = NOW()
                 WHERE id = :id
                """
            ),
            {
                "err": f"{type(exc).__name__}: {exc}"[:2000],
                "att": attempts,
                "id": log_id,
            },
        )
        await db.commit()
        raise RuntimeError(f"Resend failed: {type(exc).__name__}: {exc}")


__all__ = [
    "EVENT_TEMPLATES",
    "EVENT_LABELS",
    "load_config",
    "save_config",
    "public_config",
    "send_event_email",
    "send_test_email",
    "resend_email",
    "render_email",
]
