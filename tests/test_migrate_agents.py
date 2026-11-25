"""MP-7: AccountStore becomes the SINGLE agent registry (issue #14).

Covers the DONE gate:
- `ensure_account` idempotency: create → re-run no-op → rate-limit update,
  with api_key_hash / tier / email never clobbered, and NO API key
  generated for legacy config-only agents.
- `migrate-agents` CLI: migrates a config-only agent (reads the legacy
  `agents:` block straight from the YAML), is a no-op on re-run, and
  prints created vs present per agent.
- `list_account_ids` active-only filtering (the registry surface that
  replaced the legacy dict for dashboard counters / available_agents).
"""

import pytest
import yaml
from click.testing import CliRunner

from parousia.auth.accounts import AccountStore
from parousia.cli.main import cli


@pytest.fixture
def store(tmp_path):
    s = AccountStore(str(tmp_path / "acc.db"))
    s.connect()
    yield s
    s.close()


@pytest.fixture
def legacy_config(tmp_path):
    """A config.yaml still carrying a legacy `agents:` block."""
    path = tmp_path / "config.yaml"
    data = {
        "domain": "agents.test.com",
        "redis": {"port": 6380},
        "agents": {
            "hermes": {"rate_limit_per_hour": 55},
            "mr-krabs": {},
            # legacy bare-string form (agent: null) must not explode
            "atlas": None,
        },
    }
    with open(path, "w") as f:
        yaml.dump(data, f)
    return str(path)


# ── ensure_account ───────────────────────────────────────────────


class TestEnsureAccount:
    def test_create_returns_created_true(self, store):
        account_id, created = store.ensure_account("hermes", rate_limit_per_hour=55)
        assert (account_id, created) == ("hermes", True)
        acct = store.get_account("hermes")
        assert acct is not None
        assert acct.rate_limit_per_hour == 55
        assert acct.status == "active"

    def test_legacy_agent_has_no_api_key(self, store):
        """A migrated config-only agent gets an EMPTY api_key_hash — no
        key is generated or hashed (they authenticate via ingest/stdio)."""
        store.ensure_account("hermes")
        acct = store.get_account("hermes")
        assert acct.api_key_hash == ""
        # Empty hash can never authenticate anyone.
        assert store.authenticate("anything") is None

    def test_rerun_is_no_op_created_false(self, store):
        store.ensure_account("hermes", rate_limit_per_hour=55)
        account_id, created = store.ensure_account("hermes", rate_limit_per_hour=55)
        assert (account_id, created) == ("hermes", False)

    def test_rerun_updates_rate_limit(self, store):
        store.ensure_account("hermes", rate_limit_per_hour=55)
        store.ensure_account("hermes", rate_limit_per_hour=77)
        assert store.get_account("hermes").rate_limit_per_hour == 77

    def test_rerun_updates_status(self, store):
        store.ensure_account("hermes")
        store.ensure_account("hermes", status="suspended")
        assert store.get_account("hermes").status == "suspended"

    def test_rerun_never_clobbers_onboarded_fields(self, store):
        """The critical invariant: a re-run must NOT touch an onboarded
        account's api_key_hash / tier / email."""
        account, raw_key = store.create_account(
            "tina", tier="pro", email="tina@example.com"
        )
        original_hash = account.api_key_hash

        store.ensure_account("tina", rate_limit_per_hour=123)

        after = store.get_account("tina")
        assert after.api_key_hash == original_hash  # key untouched
        assert after.tier == "pro"  # tier untouched
        assert after.email == "tina@example.com"  # email untouched
        assert after.rate_limit_per_hour == 123  # …but the rate did update
        # And the real key still authenticates.
        assert store.authenticate(raw_key).account_id == "tina"

    def test_display_name_updated_only_when_passed(self, store):
        store.ensure_account("hermes", display_name="Hermes Agent")
        store.ensure_account("hermes", rate_limit_per_hour=9)
        assert store.get_account("hermes").display_name == "Hermes Agent"
        store.ensure_account("hermes", display_name="Renamed")
        assert store.get_account("hermes").display_name == "Renamed"

    def test_created_account_appears_in_list(self, store):
        store.ensure_account("hermes")
        store.ensure_account("tina")
        assert store.list_account_ids() == ["hermes", "tina"]


class TestListAccountIds:
    def test_active_only_by_default(self, store):
        store.ensure_account("active-one")
        store.ensure_account("suspended-one")
        store.set_status("suspended-one", "suspended")
        assert store.list_account_ids() == ["active-one"]

    def test_all_statuses_when_asked(self, store):
        store.ensure_account("active-one")
        store.ensure_account("suspended-one")
        store.set_status("suspended-one", "suspended")
        assert store.list_account_ids(active_only=False) == [
            "active-one",
            "suspended-one",
        ]


# ── migrate-agents CLI ───────────────────────────────────────────


class TestMigrateAgentsCli:
    def _run(self, db_path, config_path):
        return CliRunner().invoke(
            cli,
            ["migrate-agents", "--db", str(db_path), "--config", str(config_path)],
        )

    def test_migrates_config_only_agents(self, tmp_path, legacy_config):
        db = tmp_path / "migrated.db"
        result = self._run(db, legacy_config)
        assert result.exit_code == 0, result.output

        store = AccountStore(str(db))
        store.connect()
        try:
            # All three legacy entries landed, no API keys.
            for agent_id in ("hermes", "mr-krabs", "atlas"):
                acct = store.get_account(agent_id)
                assert acct is not None, agent_id
                assert acct.api_key_hash == ""
            # The legacy rate limit came across.
            assert store.get_account("hermes").rate_limit_per_hour == 55
            # The output reports created.
            assert "created  hermes" in result.output
        finally:
            store.close()

    def test_rerun_is_a_no_op(self, tmp_path, legacy_config):
        db = tmp_path / "migrated.db"
        first = self._run(db, legacy_config)
        assert first.exit_code == 0, first.output
        second = self._run(db, legacy_config)
        assert second.exit_code == 0, second.output

        store = AccountStore(str(db))
        store.connect()
        try:
            # Re-run: present, not created — and the rate limit from the
            # YAML is re-applied (idempotent refresh, same value).
            assert "present  hermes" in second.output
            assert "created  hermes" not in second.output
            assert store.get_account("hermes").rate_limit_per_hour == 55
        finally:
            store.close()

    def test_rerun_preserves_onboarded_key(self, tmp_path, legacy_config):
        """A re-run must not clobber an account that was onboarded (with a
        real API key) after the first migration."""
        db = tmp_path / "migrated.db"
        assert self._run(db, legacy_config).exit_code == 0

        store = AccountStore(str(db))
        store.connect()
        try:
            # Someone onboards tina properly (real key) after migration.
            _, raw_key = store.create_account("tina", tier="pro")
            original_hash = store.get_account("tina").api_key_hash
            store.close()

            # Re-run the migration: tina has no legacy entry, but hermes
            # etc. are refreshed — tina's key must survive any path.
            result = self._run(db, legacy_config)
            assert result.exit_code == 0, result.output

            store = AccountStore(str(db))
            store.connect()
            after = store.get_account("tina")
            assert after.api_key_hash == original_hash
            assert store.authenticate(raw_key).account_id == "tina"
        finally:
            store.close()

    def test_no_legacy_block_reports_nothing_to_migrate(self, tmp_path):
        config = tmp_path / "clean.yaml"
        with open(config, "w") as f:
            yaml.dump({"domain": "agents.test.com"}, f)
        db = tmp_path / "clean.db"
        result = self._run(db, config)
        assert result.exit_code == 0, result.output
        assert "nothing to migrate" in result.output

    def test_missing_config_file_reports_nothing(self, tmp_path):
        db = tmp_path / "none.db"
        result = self._run(db, tmp_path / "no-such-config.yaml")
        assert result.exit_code == 0, result.output
        assert "nothing to migrate" in result.output