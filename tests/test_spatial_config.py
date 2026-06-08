"""Tests for spatial configuration models."""

from parousia.config import ParousiaConfig, SpatialConfig, Crawl4aiConfig


def test_crawl4ai_config_defaults():
    """Test Crawl4aiConfig default values."""
    config = Crawl4aiConfig()
    
    assert config.word_count_threshold == 200
    assert config.exclude_tags == []
    assert config.exclude_selectors == []
    assert config.timeout_ms == 15000


def test_spatial_config_defaults():
    """Test SpatialConfig default values."""
    config = SpatialConfig()
    
    assert config.enabled is True
    assert config.chromium_path == "/usr/bin/chromium-browser"
    assert config.profile_dir == "/var/lib/parousia/browsers"
    assert config.idle_timeout_seconds == 300
    assert config.max_instances == 10
    assert config.launch_args == []
    assert isinstance(config.crawl4ai, Crawl4aiConfig)


def test_parousia_config_spatial_field():
    """Test that ParousiaConfig includes spatial field."""
    config = ParousiaConfig()
    
    assert hasattr(config, 'spatial')
    assert isinstance(config.spatial, SpatialConfig)
    assert config.spatial.enabled is True


def test_spatial_config_with_custom_values():
    """Test SpatialConfig with custom values."""
    config = SpatialConfig(
        enabled=False,
        chromium_path="/custom/chromium",
        profile_dir="/custom/profiles",
        idle_timeout_seconds=600,
        max_instances=5,
        launch_args=["--headless", "--no-sandbox"],
        crawl4ai=Crawl4aiConfig(
            word_count_threshold=100,
            exclude_tags=["script", "style"],
            exclude_selectors=[".ads", ".sidebar"]
        )
    )
    
    assert config.enabled is False
    assert config.chromium_path == "/custom/chromium"
    assert config.profile_dir == "/custom/profiles"
    assert config.idle_timeout_seconds == 600
    assert config.max_instances == 5
    assert config.launch_args == ["--headless", "--no-sandbox"]
    
    assert config.crawl4ai.word_count_threshold == 100
    assert config.crawl4ai.exclude_tags == ["script", "style"]
    assert config.crawl4ai.exclude_selectors == [".ads", ".sidebar"]