"""DNS record formatter for Parousia DKIM setup."""

import base64


def format_dns_records(domain: str, selector: str, public_key_pem: str) -> str:
    """Format DKIM, SPF, and DMARC DNS records for copy-paste into DNS provider.

    Args:
        domain: The email domain (e.g., example.com).
        selector: DKIM selector (e.g., "default").
        public_key_pem: Public key in PEM format.

    Returns:
        Multi-line string with all three DNS records.
    """
    # Extract DER-encoded public key and base64-encode it
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.backends import default_backend

    pub_key = serialization.load_pem_public_key(
        public_key_pem.encode(), backend=default_backend()
    )
    der = pub_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    b64_key = base64.b64encode(der).decode()

    return f"""\
;; DKIM Record — add to your DNS provider
{selector}._domainkey.{domain}.  IN  TXT  "v=DKIM1; k=rsa; p={b64_key}"

;; SPF Record
{domain}.  IN  TXT  "v=spf1 mx -all"

;; DMARC Record
_dmarc.{domain}.  IN  TXT  "v=DMARC1; p=quarantine; rua=mailto:postmaster@{domain}"
"""
