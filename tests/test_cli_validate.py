"""Edge-case tests for the validate command."""

from unittest import mock
from click.testing import CliRunner
from parousia.cli.main import cli


@mock.patch("parousia.config.load_config")
def test_validate_catches_config_loading_errors(mock_config):
    """validate exits non-zero when config fails to load."""
    mock_config.side_effect = RuntimeError("YAML parse error at line 3")
    runner = CliRunner()
    result = runner.invoke(cli, ["validate"])
    assert result.exit_code == 1
    assert "Config error" in result.output


@mock.patch("parousia.config.load_config")
@mock.patch("subprocess.run")
@mock.patch("os.path.exists", return_value=True)
@mock.patch("redis.Redis")
def test_validate_catches_redis_connection_error(mock_redis, mock_exists, mock_run, mock_config):
    """validate detects Redis connection failure."""
    from parousia.config import ParousiaConfig
    mock_config.return_value = ParousiaConfig(domain="mx.example.com")

    mock_run.return_value.returncode = 0
    mock_run.return_value.stdout = "active\n"

    # Simulate Redis connection failure
    mock_redis_instance = mock_redis.return_value
    mock_redis_instance.ping.side_effect = ConnectionError("Connection refused")

    runner = CliRunner()
    result = runner.invoke(cli, ["validate"])
    assert result.exit_code == 1
    assert "Redis unreachable" in result.output


@mock.patch("parousia.config.load_config")
@mock.patch("subprocess.run")
@mock.patch("os.path.exists", return_value=False)
@mock.patch("redis.Redis")
def test_validate_catches_missing_alias_file(mock_redis, mock_exists, mock_run, mock_config):
    """validate warns on missing aliases file but still checks Redis."""
    from parousia.config import ParousiaConfig
    mock_config.return_value = ParousiaConfig(domain="mx.example.com")

    mock_run.return_value.returncode = 0
    mock_run.return_value.stdout = "active\n"

    # Simulate Redis connection failure
    mock_redis_instance = mock_redis.return_value
    mock_redis_instance.ping.side_effect = ConnectionError("Connection refused")

    runner = CliRunner()
    result = runner.invoke(cli, ["validate"])
    assert result.exit_code == 1  # Redis unreachable causes error
    assert "not found" in result.output
    assert "Redis unreachable" in result.output


@mock.patch("parousia.config.load_config")
@mock.patch("subprocess.run")
@mock.patch("os.path.exists", return_value=True)
def test_validate_default_domain_warning(mock_exists, mock_run, mock_config):
    """validate warns when domain is the default value."""
    from parousia.config import ParousiaConfig
    mock_config.return_value = ParousiaConfig()  # default domain = agents.yourdomain.com

    mock_run.return_value.returncode = 0
    mock_run.return_value.stdout = "active\n"

    runner = CliRunner()
    result = runner.invoke(cli, ["validate"])
    assert "default" in result.output
    assert "agents.yourdomain.com" in result.output


@mock.patch("parousia.config.load_config")
@mock.patch("os.path.exists", return_value=True)
def test_validate_systemctl_not_found(mock_exists, mock_config):
    """validate handles missing systemctl gracefully."""
    from parousia.config import ParousiaConfig
    mock_config.return_value = ParousiaConfig(domain="mx.example.com")

    import subprocess
    # We need subprocess.run to raise FileNotFoundError for systemctl
    # but that requires a real subprocess call. Instead, patch it.
    with mock.patch("subprocess.run") as mock_run:
        mock_run.side_effect = FileNotFoundError("systemctl not found")
        runner = CliRunner()
        result = runner.invoke(cli, ["validate"])
        assert result.exit_code == 1
        assert "systemctl not found" in result.output


@mock.patch("parousia.config.load_config")
@mock.patch("os.path.exists", return_value=True)
def test_validate_systemctl_timeout(mock_exists, mock_config):
    """validate handles systemctl timeout."""
    from parousia.config import ParousiaConfig
    mock_config.return_value = ParousiaConfig(domain="mx.example.com")

    import subprocess
    with mock.patch("subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="systemctl", timeout=5)
        runner = CliRunner()
        result = runner.invoke(cli, ["validate"])
        assert result.exit_code == 1
        assert "timed out" in result.output
