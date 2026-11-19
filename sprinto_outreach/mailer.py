"""Delivery. The only recipient is the demo inbox from config — enforced here, at the integration boundary.

`send()` has no recipient parameter, so nothing the LLM writes can change who receives mail.
"""
from __future__ import annotations

import smtplib
import ssl

import httpx
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from .email_gen import ensure_layout
from .settings import Settings


class DeliveryPolicyViolation(Exception):
    pass


class SendUncertain(Exception):
    pass


class SetupRequired(Exception):
    pass


def enforce_email_recipients(settings: Settings, to: list[str], cc=None, bcc=None) -> None:
    demo = settings.delivery_address
    if not demo:
        raise SetupRequired("DEMO_RECIPIENT_EMAIL (or GMAIL_ADDRESS) is not set")
    if [a.lower() for a in to] != [demo]:
        raise DeliveryPolicyViolation(f"recipient must be exactly the demo inbox, got {to}")
    if cc or bcc:
        raise DeliveryPolicyViolation("cc/bcc are not allowed")


def build_message(settings: Settings, company: str, subject: str, body: str, written_for: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = f"[DEMO — {company}] {subject}"
    sender = settings.resend_from if settings.mail_provider == "resend" else (settings.gmail_address or "demo@localhost")
    msg["From"] = formataddr((settings.sender_name, sender))
    msg["To"] = settings.delivery_address
    msg["Message-ID"] = make_msgid(domain="sprinto-agent.local")
    msg["X-Sprinto-Agent"] = "1"
    footer = f"\n\n—\nDemo send: written for {written_for}. Delivered only to your own inbox."
    msg.set_content(ensure_layout(body) + footer)
    return msg


def send_resend(settings: Settings, msg: EmailMessage) -> dict:
    """Resend REST API. With the test sender (onboarding@resend.dev) Resend only delivers to the account's own email."""
    to = [settings.delivery_address]
    enforce_email_recipients(settings, to)
    payload = {
        "from": formataddr((settings.sender_name, settings.resend_from)),
        "to": to,
        "subject": str(msg["Subject"]),
        "text": msg.get_content(),
        "headers": {"X-Sprinto-Agent": "1"},
    }
    headers = {"Authorization": f"Bearer {settings.resend_key}",
               # Resend dedupes repeats of the same key for 24h, so a retry can't send twice.
               "Idempotency-Key": str(msg["Message-ID"]).strip("<>")}
    try:
        r = httpx.post("https://api.resend.com/emails", json=payload, headers=headers, timeout=30)
    except httpx.TimeoutException as e:
        raise SendUncertain("Resend timed out — check resend.com/emails before retrying") from e
    except httpx.TransportError as e:
        raise ConnectionError(f"could not reach Resend: {type(e).__name__}") from e
    if r.status_code in (401, 403):
        detail = _resend_error(r)
        if "only send testing emails" in detail.lower() or "verify a domain" in detail.lower():
            raise SetupRequired(f"Resend test mode: {detail} Set DEMO_RECIPIENT_EMAIL to the email you signed up to Resend with.")
        raise SetupRequired(f"Resend rejected the API key: {detail}")
    if r.status_code == 422:
        raise SetupRequired(f"Resend refused the message: {_resend_error(r)}")
    if r.status_code == 429:
        raise ConnectionError("Resend rate limit — retry in a minute")
    if r.status_code >= 400:
        raise ConnectionError(f"Resend error {r.status_code}: {_resend_error(r)}")
    return {"status": "sent", "provider": "resend", "provider_id": r.json().get("id", "")}


def _resend_error(r) -> str:
    try:
        return str(r.json().get("message", r.text))[:300]
    except ValueError:
        return r.text[:300]


def send(settings: Settings, company: str, subject: str, body: str, written_for: str) -> dict:
    """Returns {"status": sent|dry_run|simulated, "message_id", "to"}."""
    to = [settings.delivery_address]
    enforce_email_recipients(settings, to)
    msg = build_message(settings, company, subject, body, written_for)
    base = {"message_id": msg["Message-ID"], "to": settings.delivery_address, "subject": msg["Subject"],
            "body": msg.get_content()}
    if settings.mode == "simulation":
        return {**base, "status": "simulated"}
    if settings.mode == "dry_run" or not settings.real_sends_enabled:
        return {**base, "status": "dry_run"}
    if settings.mail_provider == "resend":
        return {**base, **send_resend(settings, msg)}
    if not (settings.gmail_address and settings.gmail_app_password):
        raise SetupRequired("GMAIL_ADDRESS and GMAIL_APP_PASSWORD are required for real sends")
    ctx = ssl.create_default_context()
    try:
        server = smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx, timeout=30)
    except (OSError, smtplib.SMTPException) as e:
        raise ConnectionError(f"could not reach Gmail SMTP: {type(e).__name__}") from e
    try:
        try:
            server.login(settings.gmail_address, settings.gmail_app_password)
        except smtplib.SMTPAuthenticationError as e:
            raise SetupRequired("Gmail rejected the login — check GMAIL_APP_PASSWORD (an App Password, not your normal password)") from e
        try:
            refused = server.send_message(msg, to_addrs=to)
        except smtplib.SMTPRecipientsRefused as e:
            raise ConnectionError("Gmail refused the recipient") from e
        except (smtplib.SMTPServerDisconnected, OSError) as e:
            # The connection broke during transmission: we can't know if Gmail accepted it.
            raise SendUncertain(f"connection dropped during send ({type(e).__name__}) — check your Sent folder") from e
        if refused:
            raise ConnectionError(f"Gmail refused: {refused}")
    finally:
        try:
            server.quit()
        except Exception:  # noqa: BLE001
            pass
    return {**base, "status": "sent"}
