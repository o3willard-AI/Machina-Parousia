"""Tests for config generation and logging setup (Story 9)."""

from unittest import mock
from click.testing import CliRunner
from parousia.cli.main import cli


# ═══════════════════════════════════════════════════════════════
# setup --config
# ═══════════════════════════════════════════════════════════════

@mock.patch("os.makedirs")
@mock.patch("os.access", return_value=True)
@mock.patch("os.path.exists", return_value=False)
@mock.patch("builtins.open", new_callable=mock.mock_open)
def test_setup_config_generates_yaml(mock_file, mock_exists, mock_access, mock_makedirs):
    """setup --config writes a valid YAML config file."""
    runner = CliRunner()
    result = runner.invoke(cli, ["setup", "--config"])
    assert result.exit_code == 0
    assert "Config written" in result.output
    mock_file.assert_called_once()
    # Verify YAML was written
    write_call = mock_file.return_value.write
    assert write_call.call_count > 0


@mock.patch("os.makedirs")
@mock.patch("os.access", return_value=True)
@mock.patch("os.path.exists", return_value=True)
@mock.patch("builtins.open", new_callable=mock.mock_open)
def test_setup_config_existing_warns(mock_file, mock_exists, mock_access, mock_makedirs):
    """setup --config warns when config already exists."""
    runner = CliRunner()
    # User says no to overwrite
    result = runner.invoke(cli, ["setup", "--config"], input="n\n")
    assert result.exit_code == 0
    assert "already exists" in result.output


@mock.patch("os.makedirs")
@mock.patch("os.access", return_value=False)  # /etc not writable
@mock.patch("os.path.exists", return_value=False)
@mock.patch("builtins.open", new_callable=mock.mock_open)
def test_setup_config_falls_back_to_home(mock_file, mock_exists, mock_access, mock_makedirs):
    """setup --config falls back to ~/.parousia when /etc not writable."""
    runner = CliRunner()
    result = runner.invoke(cli, ["setup", "--config"])
    assert result.exit_code == 0
    assert "Config written" in result.output


# ═══════════════════════════════════════════════════════════════
# Logging setup
# ═══════════════════════════════════════════════════════════════

def test_json_formatter_outputs_valid_json():
    """JsonFormatter produces valid JSON."""
    from parousia.logging_setup import JsonFormatter
    import logging

    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="parousia.test", level=logging.INFO, pathname="test.py",
        lineno=1, msg="test message", args=(), exc_info=None
    )
    output = formatter.format(record)
    import json
    data = json.loads(output)
    assert data["level"] == "info"
    assert data["message"] == "test message"
    assert "timestamp" in data


def test_setup_logging_returns_logger():
    """setup_logging returns a configured logger."""
    from parousia.logging_setup import setup_logging

    logger = setup_logging(level="debug", output="stdout", log_format="text")
    assert logger.level == 10  # DEBUG
    assert len(logger.handlers) == 1


def test_get_logger_returns_named_logger():
    """get_logger returns namespaced loggers."""
    from parousia.logging_setup import get_logger

    ingest = get_logger("guard.ingest")
    assert ingest.name == "parousia.guard.ingest"

    root = get_logger()
    assert root.name == "parousia"
