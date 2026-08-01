"""Embedding provider protocol, config, and adapter factory.

Mirrors ``athenaeum.librarian.llm`` one-for-one: the embedding subsystem
depends only on the ``EmbeddingProvider`` protocol; provider adapters are
thin mappings (httpx REST for API sources, fastembed ONNX for local models)
created via ``create_embedding_provider``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

KIND_QUERY = "query"
KIND_DOCUMENT = "document"


class EmbeddingProviderError(Exception):
    """The embedding endpoint returned an error payload or an unusable response."""


@dataclass
class EmbeddingConfig:
    """Per-user embedding settings (from the librarian_configs DB row)."""

    source: str  # 'local' | 'api'
    model: str
    provider: str | None = None  # api only: provider_configs.provider
    api_key: str = ""  # api only
    base_url: str | None = None  # api only


class EmbeddingProvider(Protocol):
    """One batched embedding method; ``kind`` drives prefix/taskType conventions."""

    async def embed(
        self,
        texts: list[str],
        config: EmbeddingConfig,
        *,
        kind: str = KIND_DOCUMENT,
    ) -> list[list[float]]: ...


def create_embedding_provider(
    config: EmbeddingConfig, *, cache_dir: Path | None = None
) -> EmbeddingProvider:
    """Build the adapter for ``config.source`` (api providers branch on provider)."""
    if config.source == "local":
        from athenaeum.librarian.embed.local import LocalFastembedProvider

        return LocalFastembedProvider(cache_dir=cache_dir)
    if config.source == "api":
        if config.provider == "anthropic":
            raise EmbeddingProviderError(
                "anthropic has no embeddings endpoint; choose an OpenAI/OpenRouter/"
                "Gemini connection or a local model"
            )
        if config.provider in ("openai", "openrouter", "openai-compatible"):
            from athenaeum.librarian.embed.openai import OpenAIEmbeddingProvider

            return OpenAIEmbeddingProvider()
        if config.provider == "gemini":
            from athenaeum.librarian.embed.gemini import GeminiEmbeddingProvider

            return GeminiEmbeddingProvider()
        raise ValueError(
            f"Unknown embedding provider {config.provider!r}; expected one of "
            "['gemini', 'openai', 'openai-compatible', 'openrouter']"
        )
    raise ValueError(f"Unknown embedding source {config.source!r}; expected 'local' or 'api'")
