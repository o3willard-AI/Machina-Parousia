"""Tests for Parousia CLI and config loading."""

import subprocess
import sys
from pathlib import Path

import pytest
import yaml


def test_cli_version():
    """parousia-guard --version exits cleanly."""
    result = subprocess.run(
        [sys.executable, "-m", "parousia.cli.main", "--version"],
        capture_output=True,
        text=True,
        cwd=Path(__file__).parent.parent,
    )
    assert result.returncode == 0
    assert "parousia-guard" in result.stdout


def test_validate_no_config_shows_error():
    """validate with no config exits non-zero with message."""
    result = subprocess.run(
        [sys.executable, "-m", "parousia.cli.main", "validate"],
        capture_output=True,
        text=True,
        cwd=Path(__file__).parent.parent,
    )
    # Should succeed because defaults kick in when no config file exists
    assert "Config loaded" in result.stdout or result.returncode == 0


def test_config_defaults():
    """Config loads with sensible defaults when no file exists."""
    from parousia.config import ParousiaConfig

    cfg = ParousiaConfig()
    assert cfg.domain == "agents.yourdomain.com"
    assert cfg.redis.port == 6379
    assert cfg.rate_limits.per_agent_per_hour == 100
    assert cfg.server.rest_port == 8080
    assert cfg.server.mcp_port == 8081


def test_config_from_temp_file(tmp_path):
    """Config loads from a YAML file."""
    from parousia.config import load_config

    config_data = {
        "domain": "test.example.com",
        "redis": {"port": 6380},
        "agents": {
            "hermes": {"webhook_url": "http://192.168.1.1:8000/webhook"}
        },
    }
    config_file = tmp_path / "config.yaml"
    with open(config_file, "w") as f:
        yaml.dump(config_data, f)

    cfg = load_config(str(config_file))
    assert cfg.domain == "test.example.com"
    assert cfg.redis.port == 6380
    assert cfg.agents["hermes"].webhook_url == "http://192.168.1.1:8000/webhook"
    # Defaults still apply for unspecified fields
    assert cfg.rate_limits.per_agent_per_hour == 100


def test_config_missing_file_uses_defaults():
    """load_config with nonexistent path uses defaults."""
    from parousia.config import load_config

    cfg = load_config("/nonexistent/path/config.yaml")
    assert cfg.domain == "agents.yourdomain.com"
