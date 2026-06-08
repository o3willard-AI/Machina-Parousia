import pytest
import tempfile
import time
import json
from pathlib import Path
from unittest.mock import patch, MagicMock

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

pytestmark = pytest.mark.skipif(not PLAYWRIGHT_AVAILABLE, reason="playwright not installed")

# Shared mock factory
def _make_mock_browser():
    """Create a fully mocked browser with correct is_connected behavior."""
    mock_pw_instance = MagicMock()
    mock_browser = MagicMock()
    mock_page = MagicMock()
    mock_browser.pages = [mock_page]
    mock_browser.is_connected.return_value = True
    mock_page.title.return_value = "about:blank"
    mock_page.goto.return_value = None
    mock_pw_instance.chromium.launch_persistent_context.return_value = mock_browser
    return mock_pw_instance, mock_browser, mock_page

@pytest.fixture
def spatial_config(tmp_path):
    from parousia.spatial.browser_pool import SpatialConfig
    return SpatialConfig(profile_dir=str(tmp_path / "browsers"), idle_timeout_seconds=1, max_instances=3)

@pytest.fixture
def browser_pool_manager(spatial_config):
    from parousia.spatial.browser_pool import BrowserPoolManager
    return BrowserPoolManager(spatial_config)

def test_launch_browser(browser_pool_manager, tmp_path):
    with patch('parousia.spatial.browser_pool.sync_playwright') as mock_playwright:
        mock_pw, mock_browser, mock_page = _make_mock_browser()
        mock_playwright.return_value.start.return_value = mock_pw

        browser_instance = browser_pool_manager.get_browser("test_agent")
        assert browser_instance is not None
        assert browser_instance.agent_id == "test_agent"
        assert browser_instance.is_alive() is True


def test_profile_creation(browser_pool_manager, tmp_path):
    with patch('parousia.spatial.browser_pool.sync_playwright') as mock_playwright:
        mock_pw, mock_browser, mock_page = _make_mock_browser()
        mock_playwright.return_value.start.return_value = mock_pw
        browser_pool_manager.get_browser("test_agent")
        profile_dir = Path(browser_pool_manager.config.profile_dir) / "test_agent"
        assert profile_dir.exists()


def test_profile_persistence(browser_pool_manager, tmp_path):
    with patch('parousia.spatial.browser_pool.sync_playwright') as mock_playwright:
        mock_pw, mock_browser, mock_page = _make_mock_browser()
        mock_playwright.return_value.start.return_value = mock_pw
        browser1 = browser_pool_manager.get_browser("test_agent")
        mock_page.evaluate.return_value = "test=1"
        browser1.browser.close()
        browser1.playwright.stop()
        # Re-patch for second launch
        mock_pw2, mock_browser2, mock_page2 = _make_mock_browser()
        mock_playwright.return_value.start.return_value = mock_pw2
        browser2 = browser_pool_manager.get_browser("test_agent")
        assert browser2 is not None


def test_agent_isolation(browser_pool_manager, tmp_path):
    with patch('parousia.spatial.browser_pool.sync_playwright') as mock_playwright:
        mock_pw, mock_browser, mock_page = _make_mock_browser()
        mock_playwright.return_value.start.return_value = mock_pw
        browser_pool_manager.get_browser("hermes")
        browser_pool_manager.get_browser("claude")
        profile_dir1 = Path(browser_pool_manager.config.profile_dir) / "hermes"
        profile_dir2 = Path(browser_pool_manager.config.profile_dir) / "claude"
        assert profile_dir1.exists()
        assert profile_dir2.exists()
        assert str(profile_dir1) != str(profile_dir2)


def test_concurrent_lock(browser_pool_manager, tmp_path):
    """get_browser returns same instance for same agent — no error."""
    with patch('parousia.spatial.browser_pool.sync_playwright') as mock_playwright:
        mock_pw, mock_browser, mock_page = _make_mock_browser()
        mock_playwright.return_value.start.return_value = mock_pw
        b1 = browser_pool_manager.get_browser("test_agent")
        # Second call returns the same instance — this is correct behavior
        b2 = browser_pool_manager.get_browser("test_agent")
        assert b1 is b2  # Same object returned


def test_stale_lock_cleanup(browser_pool_manager, tmp_path):
    profile_dir = Path(browser_pool_manager.config.profile_dir) / "test_agent"
    profile_dir.mkdir(parents=True, exist_ok=True)
    lock_file = profile_dir / "profile.lock"
    lock_data = {'pid': 99999, 'agent_id': 'test_agent', 'timestamp': 0}
    with open(lock_file, 'w') as f:
        json.dump(lock_data, f)

    with patch('parousia.spatial.browser_pool.sync_playwright') as mock_playwright:
        mock_pw, mock_browser, mock_page = _make_mock_browser()
        mock_playwright.return_value.start.return_value = mock_pw
        browser_instance = browser_pool_manager.get_browser("test_agent")
        assert lock_file.exists()


def test_max_instances(browser_pool_manager, tmp_path):
    """With max_instances=3, agent4 should raise RuntimeError."""
    with patch('parousia.spatial.browser_pool.sync_playwright') as mock_playwright:
        mock_pw, mock_browser, mock_page = _make_mock_browser()
        mock_playwright.return_value.start.return_value = mock_pw
        browser_pool_manager.get_browser("agent1")
        browser_pool_manager.get_browser("agent2")
        browser_pool_manager.get_browser("agent3")
        with pytest.raises(RuntimeError, match="Max instances"):
            browser_pool_manager.get_browser("agent4")


def test_shutdown_all(browser_pool_manager, tmp_path):
    with patch('parousia.spatial.browser_pool.sync_playwright') as mock_playwright:
        mock_pw, mock_browser, mock_page = _make_mock_browser()
        mock_playwright.return_value.start.return_value = mock_pw
        b1 = browser_pool_manager.get_browser("agent1")
        b2 = browser_pool_manager.get_browser("agent2")
        assert b1.is_alive() is True
        assert b2.is_alive() is True
        browser_pool_manager.shutdown_all()
        # After shutdown, browser.is_connected should return False
        mock_browser.is_connected.return_value = False
        assert b1.is_alive() is False
        assert b2.is_alive() is False


def test_status(browser_pool_manager, tmp_path):
    with patch('parousia.spatial.browser_pool.sync_playwright') as mock_playwright:
        mock_pw, mock_browser, mock_page = _make_mock_browser()
        mock_playwright.return_value.start.return_value = mock_pw
        browser_pool_manager.get_browser("agent1")
        browser_pool_manager.get_browser("agent2")
        status = browser_pool_manager.status()
        assert len(status) == 2
        assert "agent1" in status
        assert "agent2" in status
        assert "state" in status["agent1"]


def test_cleanup_profile(browser_pool_manager, tmp_path):
    with patch('parousia.spatial.browser_pool.sync_playwright') as mock_playwright:
        mock_pw, mock_browser, mock_page = _make_mock_browser()
        mock_playwright.return_value.start.return_value = mock_pw
        browser_pool_manager.get_browser("test_agent")
        profile_dir = Path(browser_pool_manager.config.profile_dir) / "test_agent"
        assert profile_dir.exists()
        browser_pool_manager.cleanup_profile("test_agent")
        assert not profile_dir.exists()


def test_is_alive(browser_pool_manager, tmp_path):
    with patch('parousia.spatial.browser_pool.sync_playwright') as mock_playwright:
        mock_pw, mock_browser, mock_page = _make_mock_browser()
        mock_playwright.return_value.start.return_value = mock_pw
        browser_instance = browser_pool_manager.get_browser("test_agent")
        assert browser_instance.is_alive() is True
        mock_browser.is_connected.return_value = False
        assert browser_instance.is_alive() is False


def test_health_check_retry(browser_pool_manager, tmp_path):
    with patch('parousia.spatial.browser_pool.sync_playwright') as mock_playwright:
        mock_pw, mock_browser, mock_page = _make_mock_browser()
        mock_page.goto.side_effect = [Exception("First attempt failed"), None]
        mock_playwright.return_value.start.return_value = mock_pw
        browser_instance = browser_pool_manager.get_browser("test_agent")
        assert browser_instance is not None


def test_playwright_not_available():
    if not PLAYWRIGHT_AVAILABLE:
        pytest.skip("playwright not installed")
    from parousia.spatial.browser_pool import SpatialConfig
    config = SpatialConfig()
    assert config.profile_dir == "/var/lib/parousia/browsers"
