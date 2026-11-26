"""Configuration loader for Parousia Guard.

Reads /etc/parousia/config.yaml with fallback to ~/.parousia/config.yaml.
"""

import os
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field


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


class ApprovalConfig(BaseModel):
    enabled: bool = False
    queue_ttl_hours: int = 72
    require_approval_for: list[str] = Field(default_factory=list)


class Crawl4aiConfig(BaseModel):
    word_count_threshold: int = 200
    exclude_tags: list[str] = Field(default_factory=list)
    exclude_selectors: list[str] = Field(default_factory=list)
    timeout_ms: int = 15000


# MP-3 (part A): the default browser identity — a current desktop Chrome UA.
# The previous default presented Playwright's headless signature
# (UA "HeadlessChrome", navigator.webdriver set) and was refused by
# bot-signature WAFs (CloudFront) before any tool could help.
DEFAULT_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)


class SpatialConfig(BaseModel):
    enabled: bool = True
    chromium_path: str = "/usr/bin/chromium-browser"
    profile_dir: str = "/var/lib/parousia/browsers"
    idle_timeout_seconds: int = 300
    max_instances: int = 10
    launch_args: list[str] = Field(default_factory=list)
    # ── MP-3 (part A): identity & egress control (GitHub issue #16) ──
    # Per-agent browser identity. Defaults present a realistic desktop
    # Chrome; all fields are config-overridable.
    user_agent: str = DEFAULT_CHROME_UA
    viewport_width: int = 1280
    viewport_height: int = 800
    locale: str = "en-US"
    timezone_id: str = "America/Los_Angeles"
    # Egress proxy selection (config-driven, default OFF = direct egress):
    # agent_id -> proxy URL (any scheme Playwright supports, e.g.
    # "http://user:pass@proxy:8080" or "socks5://proxy:1080").
    proxy_servers: dict[str, str] = Field(default_factory=dict)
    # Default egress proxy for agents with no explicit mapping ("" = direct).
    proxy_default: str = ""
    # ── MP-10a (issue #11 ws1, chunk 1 of 3): named proxy registry — a
    # MENU of available transports (name -> proxy URL), never a routing
    # rule. Agents explicitly call one BY NAME via the `proxy` argument on
    # browse_to/interact; omitted or "" means DIRECT egress, always.
    # `proxy_servers`/`proxy_default` above remain the operator-side
    # per-agent mapping (unchanged, MP-3). "tor" is a reserved name —
    # chunk 2 will register it; until then it errors as unknown. ──
    proxies: dict[str, str] = Field(default_factory=dict)
    # ── MP-9: full fingerprint hardening ("stealth") is OPT-IN and OFF by
    # default (issue #15) — ordinary browsing stays exactly as today. When
    # enabled, the pool injects the stealth init script (webdriver →
    # undefined, window.chrome, plugins/languages/platform realism, WebGL
    # vendor/renderer, history.length) INSTEAD of the plain webdriver-
    # suppression script. ──
    stealth_enabled: bool = False
    crawl4ai: Crawl4aiConfig = Field(default_factory=Crawl4aiConfig)


class CredentialStoreConfig(BaseModel):
    """MP-3 (part B): server-side credential store for the browser.

    SQLite + Fernet encryption at rest. Admin/human-populated only (the
    `parousia-guard vault add` CLI); agents never add or read secrets —
    they can only request an origin-bound fill via `fill_credentials`,
    which returns a fill summary, never the secret.
    """

    db_path: str = "/var/lib/parousia/credentials.db"
    key_path: str = "/var/lib/parousia/credential_store.key"


class TOTPStoreConfig(BaseModel):
    """MP-8: server-side TOTP vault (RFC 6238) for app-based 2FA.

    Same Fernet-at-rest approach as the credential store. Secrets are
    account-scoped and only ever decrypted in memory to compute a code —
    no tool response ever carries them.
    """

    db_path: str = "/var/lib/parousia/totp.db"
    key_path: str = "/var/lib/parousia/totp_store.key"


class AccountStoreConfig(BaseModel):
    db_path: str = "/var/lib/parousia/accounts.db"


class AdminConfig(BaseModel):
    api_key: str = ""  # Set via PAROUSIA_ADMIN_KEY env var


class InvitesConfig(BaseModel):
    require_invite: bool = True  # Require a valid invite code for onboarding


class ParousiaConfig(BaseModel):
    # MP-7: the legacy `agents` dict is GONE — AccountStore (accounts.db)
    # is the single agent registry. A leftover `agents:` block in an old
    # config.yaml is ignored (pydantic ignores extra keys on load); migrate
    # it with `parousia-guard migrate-agents` and then delete the block.
    domain: str = "agents.yourdomain.com"
    hostname: str = "mx.agents.yourdomain.com"
    redis: RedisConfig = Field(default_factory=RedisConfig)
    rate_limits: RateLimitConfig = Field(default_factory=RateLimitConfig)
    postfix: PostfixConfig = Field(default_factory=PostfixConfig)
    dkim: DkimConfig = Field(default_factory=DkimConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    approval: ApprovalConfig = Field(default_factory=ApprovalConfig)
    spatial: SpatialConfig = Field(default_factory=SpatialConfig)
    credential_store: CredentialStoreConfig = Field(default_factory=CredentialStoreConfig)
    totp_store: TOTPStoreConfig = Field(default_factory=TOTPStoreConfig)
    account_store: AccountStoreConfig = Field(default_factory=AccountStoreConfig)
    admin: AdminConfig = Field(default_factory=AdminConfig)
    invites: InvitesConfig = Field(default_factory=InvitesConfig)


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