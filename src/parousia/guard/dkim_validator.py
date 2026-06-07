"""DKIM signature verification for inbound email.

Uses the dkimpy library to verify DKIM signatures on raw RFC 822 messages.
DNS resolution is delegated to the system resolver via the `dig` command.
"""

import logging
import subprocess
from typing import Tuple

logger = logging.getLogger("parousia.dkim")


def verify_dkim(raw_email: bytes, dns_timeout: float = 5.0) -> Tuple[bool, str]:
    """Verify DKIM signatures in a raw RFC 822 email.

    Args:
        raw_email: Raw email bytes (RFC 822 format).
        dns_timeout: DNS lookup timeout in seconds (default 5s).

    Returns:
        (verified, details) tuple. verified=True if at least one
        valid DKIM signature was found. details is a human-readable
        string describing the result (for logging / payload).

    Graceful degradation:
    - If dkimpy is not installed: returns (False, "dkimpy not available")
    - If DNS resolution fails: returns (False, "DNS lookup failed: ...")
    - If the email has no DKIM header: returns (False, "No DKIM signature header")
    """
    try:
        import dkim
    except ImportError:
        logger.warning("dkimpy not installed — DKIM verification skipped")
        return (False, "dkimpy not available")

    try:
        # DNS resolver for dkimpy — uses system `dig` for TXT records.
        def _dns_resolver(name: bytes, timeout: float = dns_timeout) -> bytes:
            name_str = name.decode("utf-8", errors="replace") if isinstance(name, bytes) else name
            try:
                result = subprocess.run(
                    ["dig", "+short", "TXT", name_str],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                if result.returncode == 0 and result.stdout.strip():
                    # dig returns TXT records quoted; strip outer quotes
                    txt = result.stdout.strip().replace('"', "")
                    return txt.encode("utf-8")
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                pass
            return b""

        verified = dkim.verify(raw_email, dnsfunc=_dns_resolver)

        if verified:
            return (True, "DKIM signature valid")
        else:
            return (False, "No valid DKIM signature found")

    except dkim.ValidationError as e:
        logger.info("DKIM validation failed", extra={"error": str(e)})
        return (False, f"DKIM validation error: {e}")
    except Exception as e:
        logger.warning("DKIM verification error", extra={"error": str(e)})
        return (False, f"DKIM verification error: {e}")
