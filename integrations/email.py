"""Email integration for Max: send fix summaries, task completion alerts, and digests."""

from __future__ import annotations

import os
import re
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from authz import owner_access

NAME = "email"
DESCRIPTION = "send email notifications, fix summaries, and daily digests"
USAGE = [
    "max email summary to user@example.com",
    "max send email to user@example.com subject Hello body World",
]

EMAIL_RE = re.compile(
    r"^\\s*(?:send\\s+)?email\\s+(?:to\\s+)?([^\\s@]+@[^\\s@]+\\.[^\\s@]+)(?:\\s+(.*))?\\s*$",
    re.IGNORECASE | re.DOTALL,
)
ADDRESS_RE = re.compile(r"^[^\\s@<>]+@[^\\s@<>]+\\.[^\\s@<>]+$")

SMTP_HOST = os.getenv("MAX_SMTP_HOST", "smtp.resend.com")
SMTP_PORT = int(os.getenv("MAX_SMTP_PORT", "587"))
SMTP_USER = os.getenv("MAX_SMTP_USER", "resend")
SMTP_PASS = os.getenv("MAX_SMTP_PASS", os.getenv("RESEND_API_KEY", ""))
SENDER_EMAIL = os.getenv("MAX_SENDER_EMAIL", "max@resend.dev")


def match(text: str) -> dict | None:
    m = EMAIL_RE.match(text or "")
    if not m:
        return None
    recipient = m.group(1).strip()
    tail = (m.group(2) or "").strip()
    subject = "Notification from Max"
    body = ""

    # Parse explicit markers in order, without letting optional lazy groups
    # silently swallow the subject/body text.
    if tail:
        subject_match = re.match(r"^subject:?\\s*(.*)$", tail, re.IGNORECASE | re.DOTALL)
        about_match = re.match(r"^about\\s+(.*)$", tail, re.IGNORECASE | re.DOTALL)
        if subject_match:
            subject_and_body = subject_match.group(1).strip()
            body_match = re.search(r"\\s+body:?\\s+", subject_and_body, re.IGNORECASE)
            if body_match:
                subject = subject_and_body[:body_match.start()].strip() or subject
                body = subject_and_body[body_match.end():].strip()
            else:
                subject = subject_and_body or subject
        elif about_match:
            subject = about_match.group(1).strip() or subject
        else:
            return None

    return {"recipient": recipient, "subject": subject, "body": body}


def send_email(recipient: str, subject: str, body: str, html_body: str | None = None) -> bool:
    """Send one bounded email using configured SMTP parameters."""
    if not SMTP_PASS:
        return False
    if not ADDRESS_RE.fullmatch(recipient or ""):
        return False
    # Header values must never contain line breaks (prevent header injection).
    if not subject or len(subject) > 200 or "\\r" in subject or "\\n" in subject:
        return False
    if len(body or "") > 20_000 or (html_body is not None and len(html_body) > 20_000):
        return False
    if not ADDRESS_RE.fullmatch(SENDER_EMAIL or ""):
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SENDER_EMAIL
    msg["To"] = recipient
    msg.attach(MIMEText(body or "", "plain"))
    if html_body:
        msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SENDER_EMAIL, [recipient], msg.as_string())
        return True
    except Exception:
        # Do not log SMTP exceptions; some providers include connection details.
        return False


async def handle(event, payload: dict) -> str:
    """Only configured owners may initiate outbound email."""
    access = owner_access(getattr(event, "author_id", None))
    if not access.allowed:
        return "Email sending is restricted to authorized Max owners."

    recipient = str(payload.get("recipient") or "").strip()
    subject = str(payload.get("subject") or "Notification from Max").strip()
    body = str(payload.get("body") or "No body content provided.")

    if not ADDRESS_RE.fullmatch(recipient):
        return "Invalid recipient email address."
    if not subject or len(subject) > 200 or "\\r" in subject or "\\n" in subject:
        return "Invalid email subject."
    if len(body) > 20_000:
        return "Email body is too long (maximum 20,000 characters)."

    success = send_email(recipient, subject, body)
    if success:
        return f"Email sent successfully to `{recipient}`.\\nSubject: *{subject}*"
    return (
        "Could not send email. Check the SMTP configuration and sender-domain "
        "verification; provider details are intentionally omitted from logs."
    )
