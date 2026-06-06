"""DKIM key generation for Parousia Guard."""

import os
from datetime import datetime
from typing import Optional


def generate_dkim_keys(
    domain: str,
    key_dir: str = "/etc/parousia/dkim",
    selector: str = "default",
    force: bool = False,
    rotate: bool = False,
) -> Optional[str]:
    """Generate RSA-2048 DKIM keypair and return the public key PEM.

    Args:
        domain: The email domain (e.g., example.com).
        key_dir: Directory to store the private key.
        selector: DKIM selector (default: "default").
        force: Overwrite existing key if True.
        rotate: If True, save with timestamp suffix, keep old key.

    Returns:
        Public key in PEM format, or None if key exists and force=False.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.backends import default_backend

    os.makedirs(key_dir, exist_ok=True)

    key_path = os.path.join(key_dir, f"{domain}.key")

    if os.path.exists(key_path) and not force and not rotate:
        return None  # caller should warn

    if rotate:
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        key_path = os.path.join(key_dir, f"{domain}.{timestamp}.key")

    # Generate RSA-2048 keypair
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend(),
    )

    # Write private key with restrictive permissions
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    with open(key_path, "wb") as f:
        f.write(private_pem)
    os.chmod(key_path, 0o600)

    # Return public key PEM
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )

    return public_pem
