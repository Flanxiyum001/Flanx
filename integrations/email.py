"""Email integration for Max: send fix summaries, task completion alerts, and digests."""

from __future__ import annotations

import os
import re
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

NAME = "email"
DESCRIPTION = "send email notifications, fix summaries, and daily digests"
USAGE = [
    "max email summary to user@example.com",
    "max send email to user@example.com subject Hello body World",
]

EMAIL_RE = re.compile(
    r"^\s*(?:send\s+)?email\s+(?:to\s+)?([^\s@]+@[^\s@]+\.[^\s@]+)\s*(?:about|subject:?\s*(.*?))?\s*(?:body:?\s*(.*))?$",
    re.IGNORECASE | re.DOTALL,
)

SMTP_HOST = os.getenv("MAX_SMTP_HOST", "smtp.resend.com")
SMTP_PORT = int(os.getenv("MAX_SMTP_PORT", "587"))
SMTP_USER = os.getenv("MAX_SMTP_USER", "resend")
SMTP_PASS = os.getenv("MAX_SMTP_PASS", os.getenv("RESEND_API_KEY", ""))
SENDER_EMAIL = os.getenv("MAX_SENDER_EMAIL", "max@resend.dev")


def match(text: str) -> dict | None:
    m = EMAIL_RE.match(text or "")
    if m:
        return {
            "recipient": m.group(1).strip(),
            "subject": (m.group(2) or "Notification from Max").strip(),
            "body": (m.group(3) or "").strip(),
        }
    return None


def send_email(recipient: str, subject: str, body: str, html_body: str | None = None) -> bool:
    """Send an email using configured SMTP parameters (e.g. Resend, SendGrid)."""
    if not SMTP_PASS:
        print("⚠️ Email dispatch skipped: MAX_SMTP_PASS / RESEND_API_KEY not configured.")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SENDER_EMAIL
    msg["To"] = recipient

    msg.attach(MIMEText(body, "plain"))
    if html_body:
        msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SENDER_EMAIL, [recipient], msg.as_string())
        return True
    except Exception as exc:
        print(f"⚠️ Email send error: {exc}")
        return False


async def handle(event, payload: dict) -> str:
    recipient = payload["recipient"]
    subject = payload["subject"] or "Notification from Max"
    body = payload["body"] or "No body content provided."

    success = send_email(recipient, subject, body)
    if success:
        return f"📧 **Email sent successfully** to `{recipient}`!\nSubject: *{subject}*"
    return (
        f"⚠️ **Could not send email** to `{recipient}`.\n"
        "Ensure `RESEND_API_KEY` or `MAX_SMTP_PASS` is set in environment variables."
    )
