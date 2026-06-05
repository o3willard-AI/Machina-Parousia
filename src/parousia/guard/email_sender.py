"""SMTP email sender for Parousia Guard.

Sends via localhost:25 (Postfix, no auth needed for trusted relay).
"""

import smtplib
import time
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid


def send_email(
    to: str,
    subject: str,
    body: str,
    from_addr: str,
    reply_to: str | None = None,
) -> str:
    """Send email via localhost SMTP.

    Args:
        to: Recipient email address.
        subject: Email subject line.
        body: Plain-text email body.
        from_addr: Sender address (agent's email).
        reply_to: Optional Reply-To address.

    Returns:
        The generated Message-ID string.
    """
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
