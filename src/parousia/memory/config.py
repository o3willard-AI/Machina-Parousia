"""Mem0 configuration loader for Parousia.

Current topology (the deployment — see the brief's note; the original plan in
``docs/plans/mem0-implementation-plan.md`` carries stale LM Studio/Clubhouse
references and must NOT be used for defaults):

- **Vector store** — Qdrant on **localhost:6333** (same VPS as Parousia).
- **Embedder** — **fastembed** (local ONNX, no external model server; the
  old LM Studio host is shut down). Model cache location comes from the
  ``FASTEMBED_CACHE_PATH`` environment variable (fastembed honors it).
- **LLM** — only used for ``infer=True`` fact extraction. Off by default
  (``infer: false`` → facts are stored verbatim; no LLM is contacted).
  To enable: set ``llm_provider: openai`` + ``llm_model`` (e.g.
  ``openrouter/<model>``) and put ``OPENROUTER_API_KEY`` in the
  environment — mem0's OpenAI provider auto-selects OpenRouter when that
  variable is set (any OpenAI-compatible endpoint also works via
  ``llm_base_url`` + ``OPENAI_API_KEY``).
"""

import os
import yaml
from dataclasses import dataclass

DEFAULT_CONFIG_PATH = "/etc/parousia/mem0.yaml"


@dataclass
class Mem0Config:
    """Parousia Mem0 settings.

    Every field can be overridden from ``/etc/parousia/mem0.yaml``; unknown
    keys in the file are ignored (filtered against the dataclass fields).
    """

    mode: str = "local"
    user_id_prefix: str = "parousia-"
    # Vector store: Qdrant runs on the same VPS as Parousia.
    vector_store_host: str = "127.0.0.1"
    vector_store_port: int = 6333
    vector_store_provider: str = "qdrant"
    # MUST match the embedder model's dimensionality
    # (BAAI/bge-small-en-v1.5 = 384).
    embedding_model_dims: int = 384
    # LLM for infer=True fact extraction. OFF by default — facts are
    # stored verbatim (mem0 skips the extraction call entirely when
    # infer=False). See the module docstring for enabling OpenRouter.
    llm_provider: str = ""
    llm_model: str = ""
    llm_base_url: str = ""
    llm_temperature: float = 0.1
    # Whether MemoryRecorder passes infer=True to mem0 add().
    infer: bool = False
    # Embedder: fastembed = local ONNX embeddings, no model server.
    # Cache location: FASTEMBED_CACHE_PATH environment variable.
    embedder_provider: str = "fastembed"
    embedder_model: str = "BAAI/bge-small-en-v1.5"
    # Only used by non-fastembed embedder providers (lmstudio/openai/...).
    embedder_base_url: str = ""

    @classmethod
    def from_file(cls, path: str = DEFAULT_CONFIG_PATH) -> "Mem0Config":
        """Load from YAML config file, falling back to defaults."""
        if os.path.exists(path):
            with open(path) as f:
                data = yaml.safe_load(f) or {}
        else:
            data = {}
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)

    def to_mem0_dict(self) -> dict:
        """Convert to mem0.Memory.from_config() dict."""
        config = {
            "vector_store": {
                "provider": self.vector_store_provider,
                "config": {
                    "host": self.vector_store_host,
                    "port": self.vector_store_port,
                    "embedding_model_dims": self.embedding_model_dims,
                },
            },
            "history_db_path": "/var/lib/parousia/mem0_history.db",
        }

        # ── Embedder ──────────────────────────────────────────────
        if self.embedder_provider == "fastembed":
            # Local ONNX: model only (no base URL; dims are auto-derived
            # from the model when mem0 builds the embedder). The download
            # cache honors FASTEMBED_CACHE_PATH in the environment.
            config["embedder"] = {
                "provider": "fastembed",
                "config": {
                    "model": self.embedder_model,
                },
            }
        elif self.embedder_provider == "lmstudio":
            config["embedder"] = {
                "provider": "lmstudio",
                "config": {
                    "model": self.embedder_model,
                    "lmstudio_base_url": self.embedder_base_url,
                },
            }
        else:
            # openai / ollama / any OpenAI-compatible provider.
            config["embedder"] = {
                "provider": self.embedder_provider,
                "config": {
                    "model": self.embedder_model,
                    "openai_base_url": self.embedder_base_url or None,
                },
            }

        # ── LLM ───────────────────────────────────────────────────
        # mem0's Memory requires an llm block at init. When no provider
        # is configured we pass an inert lmstudio placeholder pointing at
        # localhost — it is NEVER contacted, because the recorder writes
        # with infer=False by default (see Mem0Config.infer).
        provider = self.llm_provider or "lmstudio"
        llm_config: dict = {"temperature": self.llm_temperature}
        if self.llm_provider == "openai":
            llm_config["model"] = self.llm_model
            if os.environ.get("OPENROUTER_API_KEY"):
                # mem0's OpenAI provider auto-selects OpenRouter when
                # OPENROUTER_API_KEY is set.
                llm_config["openrouter_base_url"] = (
                    self.llm_base_url or "https://openrouter.ai/api/v1"
                )
            else:
                llm_config["openai_base_url"] = self.llm_base_url or None
        elif provider == "lmstudio":
            llm_config["model"] = self.llm_model or "unused"
            llm_config["lmstudio_base_url"] = (
                self.llm_base_url or "http://127.0.0.1:1234/v1"
            )
        else:
            llm_config["model"] = self.llm_model
            llm_config["openai_base_url"] = self.llm_base_url or None
        config["llm"] = {"provider": provider, "config": llm_config}

        return config