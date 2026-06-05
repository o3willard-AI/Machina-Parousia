"""Configuration loader for Parousia Guard.

Reads /etc/parousia/config.yaml with fallback to ~/.parousia/config.yaml.
"""

import os
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field


class AgentConfig(BaseModel):
    webhook_url: str = "http://localhost:8000/webhook"
    rate_limit_per_hour: int = 100


class RedisConfig(BaseModel):
    host: str = "localhost"
    port: int = 6379
    db: int = 0


class RateLimitConfig(BaseModel):
    per_agent_per_hour: int = 100
    domain_per_day: int = 500


class PostfixConfig(BaseModel):
    aliases_file: str = "/etc/aliases"
    guard_script: str = "/usr/local/bin/parousia-guard"


class DkimConfig(BaseModel):
    key_dir: str = "/etc/parousia/dkim"
    selector: str = "default"


class ServerConfig(BaseModel):
    rest_host: str = "127.0.0.1"
    rest_port: int = 8080
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8081


class LoggingConfig(BaseModel):
    level: str = "info"
    format: str = "json"
    output: str = "syslog"


class ParousiaConfig(BaseModel):
    domain: str = "agents.yourdomain.com"
    hostname: str = "mx.agents.yourdomain.com"
    agents: dict[str, AgentConfig] = Field(default_factory=dict)
    redis: RedisConfig = Field(default_factory=RedisConfig)
    rate_limits: RateLimitConfig = Field(default_factory=RateLimitConfig)
    postfix: PostfixConfig = Field(default_factory=PostfixConfig)
    dkim: DkimConfig = Field(default_factory=DkimConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


def _find_config() -> Optional[Path]:
    """Locate config file: /etc/parousia/config.yaml or ~/.parousia/config.yaml."""
    paths = [
        Path("/etc/parousia/config.yaml"),
        Path.home() / ".parousia" / "config.yaml",
    ]
    for p in paths:
        if p.exists():
            return p
    return None


def load_config(path: Optional[str] = None) -> ParousiaConfig:
    """Load Parousia configuration from YAML file.

    Resolution order:
    1. Explicit path argument
    2. /etc/parousia/config.yaml
    3. ~/.parousia/config.yaml
    4. Defaults (all fields have sensible defaults)
    """
    config_path = None
    if path:
        config_path = Path(path)
    else:
        config_path = _find_config()

    data: dict = {}
    if config_path and config_path.exists():
        with open(config_path) as f:
            data = yaml.safe_load(f) or {}

    return ParousiaConfig(**data)
