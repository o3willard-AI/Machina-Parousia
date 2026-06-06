"""Shared fixtures for Parousia integration tests."""

import pytest
from unittest import mock


@pytest.fixture
def mock_redis_client():
    """Mock Redis client that responds to ping and basic operations."""
    with mock.patch("redis.Redis") as mock_redis:
        instance = mock_redis.return_value
        instance.ping.return_value = True
        instance.get.return_value = b"0"
        instance.incr.return_value = 1
        instance.ttl.return_value = 3600
        instance.expire.return_value = True
        yield mock_redis


@pytest.fixture
def mock_subprocess():
    """Mock subprocess.run with configurable results."""
    with mock.patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = ""
        yield mock_run


@pytest.fixture
def mock_smtp():
    """Mock SMTP connection."""
    with mock.patch("smtplib.SMTP") as mock_smtp_class:
        mock_smtp = mock.MagicMock()
        mock_smtp.__enter__.return_value = mock_smtp
        mock_smtp.send_message.return_value = {}
        mock_smtp_class.return_value = mock_smtp
        yield mock_smtp_class


@pytest.fixture
def cli_runner():
    """Click CLI test runner."""
    from click.testing import CliRunner

    return CliRunner()


@pytest.fixture
def sample_config_dict():
    """Minimal valid config dict."""
    return {
        "domain": "mx.example.com",
        "hostname": "mx.example.com",
    }
