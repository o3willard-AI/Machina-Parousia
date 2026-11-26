"""SMTP email sender for Parousia Guard.

Sends via localhost:25 (Postfix, no auth needed for trusted relay).
MP-17: supports calendar invitations — with ``ics`` set, the message is a
``multipart/alternative`` carrying the plain body, a
``text/calendar; method=REQUEST`` part, and an ``application/ics``
attachment. ORGANIZER/ATTENDEE are derived inside this module (never
agent-supplied — see ``_enrich_ics``).
"""

import re
import smtplib
import time
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid

# Untrusted fields (to/subject/reply_to) are single-line header values —
# any CR/LF in them is a header-injection attempt and is stripped.
_CRLF_RE = re.compile(r"[\r\n]")


def _clean_header(value: str) -> str:
    """Strip CR/LF from a single-line header value (anti-injection)."""
    return _CRLF_RE.sub(" ", value or "")


def _enrich_ics(ics_text: str, from_addr: str, to_addr: str) -> str:
    """Normalize an agent-supplied .ics into a METHOD:REQUEST invitation body.

    MP-17 security: ORGANIZER is DERIVED from the authenticated From
    identity and ATTENDEE from the recipient — any ORGANIZER/ATTENDEE lines
    already in the payload are agent-supplied forgeries and are STRIPPED.
    METHOD:REQUEST is injected (an existing METHOD:PUBLISH is never
    silently honored). The VEVENT UID is kept when present, else a stable
    one is generated from the sending domain. All derived values are
    CR/LF-stripped (an embedded line break could forge .ics property
    lines). Lines are RFC 5545 CRLF on output.
    """
    from_addr = _clean_header(from_addr)
    to_addr = _clean_header(to_addr)
    from_cn = from_addr.split("@")[0]
    to_cn = to_addr.split("@")[0]
    domain = from_addr.split("@")[-1] or "localhost"

    lines = ics_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    uid_present = any(line.upper().startswith("UID:") for line in lines)

    method_done = False
    out: list[str] = []
    for line in lines:
        upper = line.upper()
        if upper.startswith("METHOD:"):
            # Re-derived below; never honor an agent-supplied METHOD.
            continue
        if upper.startswith("ORGANIZER") or upper.startswith("ATTENDEE"):
            # Organizer/attendee identity is derived, not caller-supplied.
            continue
        out.append(line)
        if upper == "BEGIN:VCALENDAR" and not method_done:
            out.append("METHOD:REQUEST")
            method_done = True
        if upper == "BEGIN:VEVENT":
            out.append(f"ORGANIZER;CN={from_cn}:mailto:{from_addr}")
            out.append(f"ATTENDEE;CN={to_cn};ROLE=REQ-PARTICIPANT:mailto:{to_addr}")
            if not uid_present:
                out.append(f"UID:{make_msgid(domain=domain).strip('<>')}")
    if not method_done:
        # No VCALENDAR wrapper — provide one (METHOD is VCALENDAR-level).
        out = ["BEGIN:VCALENDAR", "METHOD:REQUEST", "VERSION:2.0"] + out + ["END:VCALENDAR"]
    return "\r\n".join(out)


def send_email(
    to: str,
    subject: str,
    body: str,
    from_addr: str,
    reply_to: str | None = None,
    ics: str | None = None,
) -> str:
    """Send email via localhost SMTP.

    Args:
        to: Recipient email address.
        subject: Email subject line.
        body: Plain-text email body (part 1 of the invite when ``ics`` set).
        from_addr: Sender address (agent's email).
        reply_to: Optional Reply-To address.
        ics: Optional iCalendar (RFC 5545) VEVENT payload. When set, the
            email is sent as a calendar invitation
            (``multipart/alternative`` with ``text/calendar;
            method=REQUEST`` + ``application/ics``); ORGANIZER/ATTENDEE are
            derived from ``from_addr``/``to`` inside this module.

    Returns:
        The generated Message-ID string.
    """
    # Header values are built via the email library's __setitem__; any
    # embedded CR/LF (header injection) is stripped from these single-line
    # values before the header is created.
    to = _clean_header(to)
    subject = _clean_header(subject)
    reply_to = _clean_header(reply_to) if reply_to else reply_to

    if ics:
        # Calendar invitation: multipart/alternative with the plain body,
        # the text/calendar part (METHOD:REQUEST in BOTH the part header
        # and the .ics body — a mismatch makes Outlook render a bare
        # attachment), and an application/ics duplicate attachment.
        enriched = _enrich_ics(ics, from_addr, to)

        msg = MIMEMultipart("alternative")
        msg["From"] = from_addr
        msg["To"] = to
        msg["Subject"] = subject
        msg["Date"] = formatdate(timeval=time.time(), localtime=True)
        msg["Message-ID"] = make_msgid(domain=from_addr.split("@")[-1])
        if reply_to:
            msg["Reply-To"] = reply_to

        msg.attach(MIMEText(body, "plain", "utf-8"))

        cal_part = MIMEText(enriched, "calendar", "utf-8")
        cal_part.set_param("method", "REQUEST")
        msg.attach(cal_part)

        att_part = MIMEApplication(enriched.encode("utf-8"), "ics")
        att_part.add_header("Content-Disposition", "attachment", filename="invite.ics")
        msg.attach(att_part)
    else:
        msg = MIMEText(body, "plain", "utf-8")
        msg["From"] = from_addr
        msg["To"] = to
        msg["Subject"] = subject
        msg["Date"] = formatdate(timeval=time.time(), localtime=True)
        msg["Message-ID"] = make_msgid(domain=from_addr.split("@")[-1])

        if reply_to:
            msg["Reply-To"] = reply_to

    with smtplib.SMTP("localhost", 25, timeout=10) as server:
        server.sendmail(from_addr, [to], msg.as_string())

    return str(msg["Message-ID"])