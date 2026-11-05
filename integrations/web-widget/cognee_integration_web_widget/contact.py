"""Hand a conversation over to the team: the contact form the assistant can offer.

When the answering model cannot answer reliably it ends its reply with
``[[contact_form]]`` (the system prompt says when). The backend strips that
marker from the answer and tells the widget to show a form under it; the
visitor's email, note and the chat transcript then come back to
``POST /api/contact`` and go to the team as one email.

Sending uses the standard library's SMTP client, so the widget gains no
dependency. Nothing is sent, and no form is offered, until both a recipient
and an SMTP host are configured:

    WIDGET_CONTACT_TO        address the requests go to (e.g. info@example.com)
    WIDGET_SMTP_HOST         SMTP server
    WIDGET_SMTP_PORT         default 587
    WIDGET_SMTP_USER         login, if the server needs one
    WIDGET_SMTP_PASSWORD
    WIDGET_SMTP_FROM         sender address; defaults to WIDGET_SMTP_USER
    WIDGET_SMTP_SECURITY     "starttls" (default), "ssl" or "none"
"""

from __future__ import annotations

import os
import re
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import List, Optional, Tuple

CONTACT_MARKER = "[[contact_form]]"
# Tolerant of the spacing and case a model may produce around the marker.
_MARKER = re.compile(r"\[\[\s*contact[_ ]form\s*\]\]", re.I)

# Bounds on what a visitor can send. The transcript comes from their browser,
# so it is capped rather than trusted.
MAX_MESSAGE_CHARS = 5000
MAX_TRANSCRIPT_MESSAGES = 60
MAX_TRANSCRIPT_CHARS = 60000

# One address, no display name, no whitespace: anything else could smuggle
# extra headers into Reply-To.
_EMAIL = re.compile(r"^[^@\s<>,;:\"]+@[^@\s<>,;:\"]+\.[^@\s<>,;:\"]+$")


def split_contact_marker(text: str) -> Tuple[str, bool]:
    """The answer without the marker, and whether the marker was there."""
    if not _MARKER.search(text or ""):
        return text, False
    return _MARKER.sub("", text).rstrip(), True


def valid_email(address: str) -> bool:
    return len(address) <= 254 and bool(_EMAIL.match(address))


@dataclass(frozen=True)
class SmtpSettings:
    to: str
    host: str
    port: int
    user: Optional[str]
    password: Optional[str]
    sender: str
    security: str

    @classmethod
    def from_env(cls) -> Optional["SmtpSettings"]:
        """The configured settings, or ``None`` when the contact form is off."""
        to = os.getenv("WIDGET_CONTACT_TO", "").strip()
        host = os.getenv("WIDGET_SMTP_HOST", "").strip()
        if not to or not host:
            return None
        user = os.getenv("WIDGET_SMTP_USER", "").strip() or None
        sender = os.getenv("WIDGET_SMTP_FROM", "").strip() or user or to
        security = os.getenv("WIDGET_SMTP_SECURITY", "starttls").strip().lower()
        if security not in ("starttls", "ssl", "none"):
            raise ValueError(
                f"WIDGET_SMTP_SECURITY must be starttls, ssl or none, not {security!r}"
            )
        return cls(
            to=to,
            host=host,
            port=int(os.getenv("WIDGET_SMTP_PORT", "587")),
            user=user,
            password=os.getenv("WIDGET_SMTP_PASSWORD") or None,
            sender=sender,
            security=security,
        )


def trim_transcript(transcript: List[dict]) -> List[dict]:
    """The last messages of the transcript, within the size caps."""
    kept, total = [], 0
    for message in reversed(transcript[-MAX_TRANSCRIPT_MESSAGES:]):
        text = str(message.get("text", ""))
        if total + len(text) > MAX_TRANSCRIPT_CHARS:
            break
        kept.append({"role": str(message.get("role", "")), "text": text})
        total += len(text)
    return list(reversed(kept))


def build_email(
    settings: SmtpSettings,
    *,
    email: str,
    message: str,
    transcript: List[dict],
    page_url: str,
    session_id: str,
) -> EmailMessage:
    """The email the team receives. Reply-To is the visitor, so answering it reaches them."""
    lines = [
        f"From: {email}",
        f"Page: {page_url or '(not sent)'}",
        f"Session: {session_id}",
        "",
        "Message:",
        message.strip() or "(no message)",
        "",
        "Conversation (as shown in the visitor's browser):",
    ]
    for entry in transcript:
        who = "Visitor" if entry["role"] == "user" else "Assistant"
        lines += ["", f"--- {who} ---", entry["text"]]

    msg = EmailMessage()
    msg["Subject"] = f"Docs assistant: contact request from {email}"
    msg["From"] = settings.sender
    msg["To"] = settings.to
    msg["Reply-To"] = email
    msg.set_content("\n".join(lines))
    return msg


def send_email(settings: SmtpSettings, msg: EmailMessage) -> None:
    """Send ``msg`` over SMTP. Blocking: run it in a thread from async code."""
    context = ssl.create_default_context()
    if settings.security == "ssl":
        smtp = smtplib.SMTP_SSL(settings.host, settings.port, timeout=30, context=context)
    else:
        smtp = smtplib.SMTP(settings.host, settings.port, timeout=30)
    with smtp:
        if settings.security == "starttls":
            smtp.starttls(context=context)
        if settings.user and settings.password:
            smtp.login(settings.user, settings.password)
        smtp.send_message(msg)
