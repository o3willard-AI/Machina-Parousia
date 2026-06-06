"""Tests for Story 5 CLI commands: setup, validate, test, status."""

from unittest import mock
from click.testing import CliRunner
from parousia.cli.main import cli


# ═══════════════════════════════════════════════════════════════
# setup --postfix
# ═══════════════════════════════════════════════════════════════

def test_setup_no_flags_prints_usage():
    runner = CliRunner()
    result = runner.invoke(cli, ["setup"])
    assert result.exit_code == 0
    assert "Usage:" in result.output


@mock.patch("parousia.config.load_config")
@mock.patch("os.makedirs")
@mock.patch("os.chmod")
@mock.patch("builtins.open", new_callable=mock.mock_open)
def test_setup_dkim_placeholder(mock_file, mock_chmod, mock_makedirs, mock_config):
    """setup --dkim generates DKIM keys and prints DNS records."""
    from parousia.config import ParousiaConfig
    mock_config.return_value = ParousiaConfig(domain="example.com")
    runner = CliRunner()
    result = runner.invoke(cli, ["setup", "--dkim"])
    assert result.exit_code == 0
    assert "v=DKIM1" in result.output


@mock.patch("builtins.open", new_callable=mock.mock_open)
@mock.patch("subprocess.run")
def test_setup_postfix_writes_alias_and_runs_newaliases(mock_run, mock_file):
    mock_run.return_value.returncode = 0
    runner = CliRunner()
    result = runner.invoke(cli, ["setup", "--postfix"])
    assert result.exit_code == 0
    assert "Wrote agent alias" in result.output
    assert "Ran newaliases" in result.output
    mock_file.assert_called_once_with("/etc/aliases", "a")
    mock_run.assert_called_once_with(
        ["newaliases"], check=True, capture_output=True, text=True
    )


@mock.patch("builtins.open")
def test_setup_postfix_permission_denied(mock_open):
    mock_open.side_effect = PermissionError("Permission denied")
    runner = CliRunner()
    result = runner.invoke(cli, ["setup", "--postfix"])
    assert result.exit_code == 1
    assert "Permission denied" in result.output


@mock.patch("builtins.open", new_callable=mock.mock_open)
@mock.patch("subprocess.run")
def test_setup_postfix_newaliases_fails(mock_run, mock_file):
    import subprocess
    mock_run.side_effect = subprocess.CalledProcessError(1, "newaliases", stderr="database error")
    runner = CliRunner()
    result = runner.invoke(cli, ["setup", "--postfix"])
    assert result.exit_code == 1
    assert "Wrote agent alias" in result.output
    assert "newaliases failed" in result.output


@mock.patch("builtins.open", new_callable=mock.mock_open)
@mock.patch("subprocess.run")
def test_setup_postfix_newaliases_not_found(mock_run, mock_file):
    mock_run.side_effect = FileNotFoundError("newaliases not found")
    runner = CliRunner()
    result = runner.invoke(cli, ["setup", "--postfix"])
    assert result.exit_code == 1
    assert "newaliases command not found" in result.output


# ═══════════════════════════════════════════════════════════════
# validate
# ═══════════════════════════════════════════════════════════════

@mock.patch("parousia.config.load_config")
@mock.patch("subprocess.run")
@mock.patch("os.path.exists", return_value=True)
@mock.patch("redis.Redis")
def test_validate_all_healthy(mock_redis, mock_exists, mock_run, mock_config):
    """validate exits 0 when everything is healthy."""
    from parousia.config import ParousiaConfig
    mock_config.return_value = ParousiaConfig(domain="mx.example.com")

    # Postfix running
    mock_run.return_value.returncode = 0
    mock_run.return_value.stdout = "active\n"

    # Redis healthy
    mock_redis_instance = mock_redis.return_value
    mock_redis_instance.ping.return_value = True

    runner = CliRunner()
    result = runner.invoke(cli, ["validate"])
    assert result.exit_code == 0
    assert "All checks passed" in result.output


@mock.patch("parousia.config.load_config")
@mock.patch("subprocess.run")
@mock.patch("os.path.exists", return_value=True)
@mock.patch("redis.Redis")
def test_validate_postfix_not_running(mock_redis, mock_exists, mock_run, mock_config):
    """validate exits non-zero when Postfix is down."""
    from parousia.config import ParousiaConfig
    mock_config.return_value = ParousiaConfig(domain="mx.example.com")

    mock_run.return_value.returncode = 3  # systemctl is-active returns 3 when inactive
    mock_run.return_value.stdout = "inactive\n"

    # Redis healthy
    mock_redis_instance = mock_redis.return_value
    mock_redis_instance.ping.return_value = True

    runner = CliRunner()
    result = runner.invoke(cli, ["validate"])
    assert result.exit_code == 1
    assert "not running" in result.output
    assert "1 error" in result.output


@mock.patch("parousia.config.load_config")
@mock.patch("os.path.exists", return_value=False)
def test_validate_config_error(mock_exists, mock_config):
    """validate catches config loading errors."""
    mock_config.side_effect = ValueError("Invalid YAML")

    runner = CliRunner()
    result = runner.invoke(cli, ["validate"])
    assert result.exit_code == 1
    assert "Config error" in result.output


@mock.patch("parousia.config.load_config")
@mock.patch("subprocess.run")
@mock.patch("os.path.exists", return_value=True)
@mock.patch("redis.Redis")
def test_validate_missing_aliases(mock_redis, mock_exists, mock_run, mock_config):
    """validate warns but doesn't fail on missing aliases file."""
    from parousia.config import ParousiaConfig
    mock_config.return_value = ParousiaConfig(domain="mx.example.com")

    mock_run.return_value.returncode = 0
    mock_run.return_value.stdout = "active\n"
    mock_exists.return_value = False  # aliases missing

    # Redis healthy
    mock_redis_instance = mock_redis.return_value
    mock_redis_instance.ping.return_value = True

    runner = CliRunner()
    result = runner.invoke(cli, ["validate"])
    assert result.exit_code == 0  # missing aliases is a warning, not error
    assert "not found" in result.output


# ═══════════════════════════════════════════════════════════════
# test --to
# ═══════════════════════════════════════════════════════════════

@mock.patch("smtplib.SMTP")
def test_test_command_sends_email(mock_smtp_class):
    """test --to sends email via SMTP."""
    mock_smtp = mock.MagicMock()
    mock_smtp.__enter__.return_value = mock_smtp
    mock_smtp.send_message.return_value = {}
    mock_smtp_class.return_value = mock_smtp

    runner = CliRunner()
    result = runner.invoke(cli, ["test", "--to", "agent@test.local"])
    assert result.exit_code == 0
    assert "Test email sent" in result.output


def test_test_command_missing_recipient():
    """test requires --to flag."""
    runner = CliRunner()
    result = runner.invoke(cli, ["test"])
    assert result.exit_code != 0
    assert "Missing option" in result.output or "Error" in result.output


@mock.patch("smtplib.SMTP")
def test_test_command_connection_refused(mock_smtp_class):
    """test handles connection refused gracefully."""
    mock_smtp_class.side_effect = ConnectionRefusedError("Connection refused")
    runner = CliRunner()
    result = runner.invoke(cli, ["test", "--to", "agent@test.local"])
    assert result.exit_code == 1
    assert "Connection refused" in result.output


# ═══════════════════════════════════════════════════════════════
# status
# ═══════════════════════════════════════════════════════════════

@mock.patch("parousia.config.load_config")
@mock.patch("subprocess.run")
def test_status_shows_redis_unavailable(mock_run, mock_config):
    """status shows 'unavailable' when Redis is down."""
    from parousia.config import ParousiaConfig
    mock_config.return_value = ParousiaConfig(domain="mx.example.com")

    # mailq succeeds, tail no logs
    mock_run.side_effect = [
        mock.MagicMock(returncode=0, stdout="Mail queue is empty\n", stderr=""),   # mailq
        FileNotFoundError("tail"),  # tail (no logs)
    ]

    runner = CliRunner()
    result = runner.invoke(cli, ["status"])
    assert result.exit_code == 0
    assert "Redis: unavailable" in result.output
    assert "Queue is empty" in result.output
