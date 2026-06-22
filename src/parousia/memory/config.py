"""Mem0 configuration loader for Parousia."""

import os
import yaml
from dataclasses import dataclass

DEFAULT_CONFIG_PATH = "/etc/parousia/mem0.yaml"

@dataclass
class Mem0Config:
    mode: str = "local"
    user_id_prefix: str = "parousia-"
    vector_store_host: str = "127.0.0.1"
    vector_store_port: int = 6333
    vector_store_provider: str = "qdrant"
    embedding_model_dims: int = 768
    llm_provider: str = "lmstudio"
    llm_model: str = "qwen2.5-coder-3b-instruct"
    llm_base_url: str = "http://192.168.101.23:1234/v1"
    llm_temperature: float = 0.1
    embedder_provider: str = "lmstudio"
    embedder_model: str = "text-embedding-nomic-embed-text-v1.5"
    embedder_base_url: str = "http://192.168.101.23:1234/v1"

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

        # Embedder config — supports lmstudio and fastembed
        if self.embedder_provider == "fastembed":
            config["embedder"] = {
                "provider": "fastembed",
                "config": {
                    "model": self.embedder_model,
                    "embedding_dims": self.embedding_model_dims,
                },
            }
        else:
            config["embedder"] = {
                "provider": self.embedder_provider,
                "config": {
                    "model": self.embedder_model,
                    "lmstudio_base_url": self.embedder_base_url,
                },
            }

        # LLM config — always provide one to prevent OpenAI default
        # When llm_provider is empty, use a disabled placeholder
        config["llm"] = {
            "provider": self.llm_provider or "none",
            "config": {
                "model": self.llm_model or "disabled",
                "lmstudio_base_url": self.llm_base_url or "http://localhost:1234/v1",
                "temperature": self.llm_temperature,
            },
        }

        return config
