"""Tests for Story 6: DKIM key generation and DNS records."""

from unittest import mock
from click.testing import CliRunner
from parousia.cli.main import cli


# ═══════════════════════════════════════════════════════════════
# setup --dkim
# ═══════════════════════════════════════════════════════════════

@mock.patch("parousia.config.load_config")
@mock.patch("os.makedirs")
@mock.patch("os.chmod")
@mock.patch("builtins.open", new_callable=mock.mock_open)
def test_dkim_generates_keypair(mock_file, mock_chmod, mock_makedirs, mock_config):
    """setup --dkim generates keys and prints DNS records."""
    from parousia.config import ParousiaConfig

    mock_config.return_value = ParousiaConfig(domain="example.com")

    runner = CliRunner()
    result = runner.invoke(cli, ["setup", "--dkim"])
    assert result.exit_code == 0
    assert "DKIM keypair generated" in result.output
    mock_makedirs.assert_called_once_with("/etc/parousia/dkim", exist_ok=True)
    mock_chmod.assert_called_once()


@mock.patch("parousia.config.load_config")
@mock.patch("os.makedirs")
@mock.patch("os.chmod")
@mock.patch("builtins.open", new_callable=mock.mock_open)
def test_dkim_prints_dns_records(mock_file, mock_chmod, mock_makedirs, mock_config):
    """Output contains DKIM, SPF, and DMARC records."""
    from parousia.config import ParousiaConfig

    mock_config.return_value = ParousiaConfig(domain="example.com")

    runner = CliRunner()
    result = runner.invoke(cli, ["setup", "--dkim"])
    assert result.exit_code == 0
    assert "v=DKIM1" in result.output
    assert "v=spf1 mx -all" in result.output
    assert "v=DMARC1" in result.output


@mock.patch("parousia.config.load_config")
@mock.patch("os.makedirs")
@mock.patch("os.path.exists", return_value=True)
def test_dkim_existing_key_warns(mock_exists, mock_makedirs, mock_config):
    """Second run warns about existing key."""
    from parousia.config import ParousiaConfig

    mock_config.return_value = ParousiaConfig(domain="example.com")

    runner = CliRunner()
    result = runner.invoke(cli, ["setup", "--dkim"])
    assert result.exit_code == 0
    assert "Key already exists" in result.output


@mock.patch("parousia.config.load_config")
@mock.patch("os.makedirs")
@mock.patch("os.chmod")
@mock.patch("builtins.open", new_callable=mock.mock_open)
def test_dkim_key_permissions(mock_file, mock_chmod, mock_makedirs, mock_config):
    """Key file has mode 0o600."""
    from parousia.config import ParousiaConfig

    mock_config.return_value = ParousiaConfig(domain="example.com")

    runner = CliRunner()
    result = runner.invoke(cli, ["setup", "--dkim"])
    assert result.exit_code == 0
    # os.chmod should be called with 0o600
    mock_chmod.assert_called_once()
    _, mode = mock_chmod.call_args[0]
    assert mode == 0o600
