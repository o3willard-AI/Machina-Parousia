"""Tests for spatial CLI commands."""

import json
import subprocess
import sys
from unittest.mock import patch, MagicMock

import pytest
from click.testing import CliRunner

from parousia.cli.spatial import spatial_group


def test_setup():
    """Test spatial setup command."""
    runner = CliRunner()
    result = runner.invoke(spatial_group, ["setup"])
    
    assert result.exit_code == 0
    assert "pip install playwright crawl4ai && playwright install chromium" in result.output


def test_status():
    """Test spatial status command."""
    runner = CliRunner()
    result = runner.invoke(spatial_group, ["status"])
    
    assert result.exit_code == 0
    # Parse the JSON output to ensure it's valid
    status_data = json.loads(result.output.strip())
    assert "enabled" in status_data
    assert "chromium_path" in status_data
    assert "active_instances" in status_data
    assert "profile_count" in status_data


@patch("parousia.cli.spatial.os.path.exists")
@patch("parousia.cli.spatial.load_config")
def test_validate(mock_load_config, mock_exists):
    """Test spatial validate command with valid setup."""
    # Mock the config to return a valid chromium path
    mock_config = MagicMock()
    mock_config.spatial.chromium_path = "/usr/bin/chromium-browser"
    mock_load_config.return_value = mock_config
    mock_exists.return_value = True
    
    # Mock imports to succeed
    with patch.dict(sys.modules, {"crawl4ai": MagicMock(), "playwright": MagicMock()}):
        runner = CliRunner()
        result = runner.invoke(spatial_group, ["validate"])
        
        assert result.exit_code == 0
        assert "✓ crawl4ai and Playwright imported successfully" in result.output


@patch("parousia.cli.spatial.os.path.exists")
@patch("parousia.cli.spatial.load_config")
def test_validate_no_chromium(mock_load_config, mock_exists):
    """Test spatial validate command when chromium is not found."""
    # Mock the config to return a non-existent chromium path
    mock_config = MagicMock()
    mock_config.spatial.chromium_path = "/non/existent/chromium"
    mock_load_config.return_value = mock_config
    mock_exists.return_value = False
    
    # Mock imports to succeed
    with patch.dict(sys.modules, {"crawl4ai": MagicMock(), "playwright": MagicMock()}):
        runner = CliRunner()
        result = runner.invoke(spatial_group, ["validate"])
        
        assert result.exit_code == 1
        assert "✗ Chromium not found at:" in result.output


def test_cleanup():
    """Test spatial cleanup command."""
    runner = CliRunner()
    result = runner.invoke(spatial_group, ["cleanup", "--days", "7"])
    
    assert result.exit_code == 0
    assert "Removing idle profiles older than 7 days" in result.output


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
