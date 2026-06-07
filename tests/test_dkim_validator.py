"""Tests for DKIM inbound validation."""

import pytest
from parousia.guard.dkim_validator import verify_dkim


def test_verify_dkim_no_dkim_header():
    """Email without DKIM-Signature header returns not-verified."""
    raw = (
        b"From: test@example.com\r\n"
        b"To: agent@test.com\r\n"
        b"Subject: Hi\r\n"
        b"\r\n"
        b"Hello.\r\n"
    )
    ok, details = verify_dkim(raw)
    assert not ok
    assert "No valid DKIM signature" in details or "No DKIM signature" in details or "dkimpy" in details


def test_verify_dkim_empty_email():
    """Empty email body returns not-verified gracefully."""
    ok, details = verify_dkim(b"")
    assert not ok
    # Should not crash — returns a descriptive string
    assert isinstance(details, str)
    assert len(details) > 0


def test_verify_dkim_returns_tuple():
    """verify_dkim always returns a (bool, str) tuple."""
    result = verify_dkim(b"From: x\r\n\r\nbody\r\n")
    assert isinstance(result, tuple)
    assert len(result) == 2
    assert isinstance(result[0], bool)
    assert isinstance(result[1], str)


def test_verify_dkim_with_broken_dns():
    """When DNS resolution fails, returns not-verified gracefully."""
    raw = (
        b"DKIM-Signature: v=1; a=rsa-sha256; d=example.com; s=default;\r\n"
        b"  h=from:to:subject; bh=invalid;\r\n"
        b"  b=invalid;\r\n"
        b"From: test@example.com\r\n"
        b"To: agent@test.com\r\n"
        b"Subject: Hi\r\n"
        b"\r\n"
        b"Body text.\r\n"
    )
    # This has a DKIM header but invalid signature — should return not-verified
    # Note: dkimpy may fail to verify due to bad signature or missing DNS
    ok, details = verify_dkim(raw)
    assert not ok  # Either "no valid signature" or "dkimpy not available"
    assert isinstance(details, str)
