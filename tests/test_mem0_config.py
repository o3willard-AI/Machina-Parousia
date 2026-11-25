"""Unit tests for the Parousia Mem0Config loader (MP-2).

Topology pins (the brief's CURRENT values — the plan doc is stale):
- Qdrant on localhost:6333 (same VPS as Parousia)
- fastembed local-ONNX embedder (cache via FASTEMBED_CACHE_PATH)
- LLM only for infer=True, OpenRouter via OPENROUTER_API_KEY,
  infer OFF by default
"""
import os
from pathlib import Path

import pytest
import yaml

from parousia.memory.config import DEFAULT_CONFIG_PATH, Mem0Config


# ── Defaults ─────────────────────────────────────────────────────


class TestDefaults:
    def test_vector_store_is_local_qdrant(self):
        cfg = Mem0Config()
        assert cfg.vector_store_host == "127.0.0.1"
        assert cfg.vector_store_port == 6333
        assert cfg.vector_store_provider == "qdrant"

    def test_embedder_is_fastembed(self):
        cfg = Mem0Config()
        assert cfg.embedder_provider == "fastembed"
        assert cfg.embedder_model == "BAAI/bge-small-en-v1.5"

    def test_embedding_dims_match_default_model(self):
        # bge-small-en-v1.5 produces 384-dim vectors; the Qdrant
        # collection is created with embedding_model_dims.
        cfg = Mem0Config()
        assert cfg.embedding_model_dims == 384

    def test_infer_off_by_default(self):
        cfg = Mem0Config()
        assert cfg.infer is False

    def test_no_stale_lm_studio_references(self):
        """The old LM Studio host (.23) is shut down — nothing may
        reference it, in defaults or in the generated mem0 config."""
        cfg = Mem0Config()
        blob = repr(cfg.__dict__) + repr(cfg.to_mem0_dict())
        assert "192.168.101.23" not in blob
        src = Path(
            Mem0Config.__module__.replace("parousia.memory", "")
        )
        # Belt and braces: the module source too.
        import parousia.memory.config as cfg_mod

        source = Path(cfg_mod.__file__).read_text()
        assert "192.168.101.23" not in source

    def test_user_id_prefix(self):
        assert Mem0Config().user_id_prefix == "parousia-"


# ── from_file ────────────────────────────────────────────────────


class TestFromFile:
    def test_missing_file_gives_defaults(self, tmp_path):
        cfg = Mem0Config.from_file(str(tmp_path / "nonexistent.yaml"))
        assert cfg == Mem0Config()

    def test_yaml_overrides_known_fields(self, tmp_path):
        f = tmp_path / "mem0.yaml"
        f.write_text(
            yaml.safe_dump(
                {
                    "vector_store_host": "10.0.0.5",
                    "vector_store_port": 6334,
                    "infer": True,
                    "llm_provider": "openai",
                    "llm_model": "openrouter/qwen/qwen3-coder",
                }
            )
        )
        cfg = Mem0Config.from_file(str(f))
        assert cfg.vector_store_host == "10.0.0.5"
        assert cfg.vector_store_port == 6334
        assert cfg.infer is True
        assert cfg.llm_provider == "openai"
        # Untouched fields keep their defaults
        assert cfg.user_id_prefix == "parousia-"
        assert cfg.embedder_provider == "fastembed"

    def test_unknown_keys_are_ignored(self, tmp_path):
        f = tmp_path / "mem0.yaml"
        f.write_text("bogus_key: 1\nllm_temperature: 0.5\n")
        cfg = Mem0Config.from_file(str(f))
        assert cfg.llm_temperature == 0.5
        # Unknown key did not blow up the load
        assert cfg.mode == "local"


# ── to_mem0_dict ──────────────────────────────────────────────────


class TestToMem0Dict:
    def test_vector_store_block(self):
        d = Mem0Config().to_mem0_dict()
        assert d["vector_store"]["provider"] == "qdrant"
        assert d["vector_store"]["config"]["host"] == "127.0.0.1"
        assert d["vector_store"]["config"]["port"] == 6333
        assert d["vector_store"]["config"]["embedding_model_dims"] == 384

    def test_fastembed_block_has_no_base_url(self):
        d = Mem0Config().to_mem0_dict()
        emb = d["embedder"]
        assert emb["provider"] == "fastembed"
        assert emb["config"]["model"] == "BAAI/bge-small-en-v1.5"
        # fastembed is local — no base URL key at all.
        assert "lmstudio_base_url" not in emb["config"]
        assert "openai_base_url" not in emb["config"]

    def test_llm_placeholder_when_provider_unset(self):
        """mem0's Memory requires an llm block at init; with no provider
        configured we pass an inert localhost lmstudio placeholder."""
        d = Mem0Config().to_mem0_dict()
        llm = d["llm"]
        assert llm["provider"] == "lmstudio"
        assert llm["config"]["lmstudio_base_url"].startswith("http://127.0.0.1")
        # Never invoked (infer=False default) — model is a placeholder.
        assert llm["config"]["model"] == "unused"

    def test_openrouter_mapping_when_env_key_set(self, monkeypatch):
        """llm_provider=openai + OPENROUTER_API_KEY → mem0's OpenAI
        provider auto-selects OpenRouter (openrouter_base_url config)."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        cfg = Mem0Config(
            llm_provider="openai",
            llm_model="openrouter/qwen/qwen3-coder",
        )
        llm = cfg.to_mem0_dict()["llm"]
        assert llm["provider"] == "openai"
        assert llm["config"]["model"] == "openrouter/qwen/qwen3-coder"
        assert llm["config"]["openrouter_base_url"] == "https://openrouter.ai/api/v1"
        assert "openai_base_url" not in llm["config"]

    def test_openai_compatible_mapping_without_env_key(self, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        cfg = Mem0Config(
            llm_provider="openai",
            llm_model="some-model",
            llm_base_url="http://127.0.0.1:9999/v1",
        )
        llm = cfg.to_mem0_dict()["llm"]
        assert llm["provider"] == "openai"
        assert llm["config"]["openai_base_url"] == "http://127.0.0.1:9999/v1"
        assert "openrouter_base_url" not in llm["config"]

    def test_custom_llm_base_url_for_openrouter(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        cfg = Mem0Config(
            llm_provider="openai",
            llm_model="openrouter/qwen/qwen3-coder",
            llm_base_url="https://openrouter.example/api/v1",
        )
        llm = cfg.to_mem0_dict()["llm"]
        assert llm["config"]["openrouter_base_url"] == "https://openrouter.example/api/v1"

    def test_history_db_path_present(self):
        assert Mem0Config().to_mem0_dict()["history_db_path"].endswith(
            "mem0_history.db"
        )

    def test_roundtrip_through_yaml(self, tmp_path):
        """A config serialized to YAML and reloaded equals the original."""
        cfg = Mem0Config(
            vector_store_host="10.1.2.3",
            infer=True,
            llm_provider="openai",
            embedder_model="Qdrant/reserved",
        )
        f = tmp_path / "roundtrip.yaml"
        f.write_text(yaml.safe_dump(cfg.__dict__))
        loaded = Mem0Config.from_file(str(f))
        assert loaded == cfg


# ── DEFAULT_CONFIG_PATH sanity ────────────────────────────────────


def test_default_config_path_is_etc_parousia():
    assert DEFAULT_CONFIG_PATH == "/etc/parousia/mem0.yaml"