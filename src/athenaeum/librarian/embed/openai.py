"""OpenAI embeddings adapter (also OpenRouter / OpenAI-compatible endpoints)."""

from __future__ import annotations

from typing import Any

import httpx

from athenaeum.librarian.embed import KIND_DOCUMENT, EmbeddingConfig, EmbeddingProviderError

DEFAULT_BASE_URL = "https://api.openai.com/v1"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class OpenAIEmbeddingProvider:
    """Thin REST mapping onto POST {base_url}/embeddings. ``kind`` is ignored."""

    async def embed(
        self,
        texts: list[str],
        config: EmbeddingConfig,
        *,
        kind: str = KIND_DOCUMENT,
    ) -> list[list[float]]:
        default = OPENROUTER_BASE_URL if config.provider == "openrouter" else DEFAULT_BASE_URL
        base_url = (config.base_url or default).rstrip("/")
        body: dict[str, Any] = {"model": config.model, "input": texts}

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{base_url}/embeddings",
                headers={"Authorization": f"Bearer {config.api_key}"},
                json=body,
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                # Mirror llm.http_status_error: HTTP failures surface as
                # EmbeddingProviderError, never raw httpx exceptions.
                body_text = exc.response.text[:200]
                raise EmbeddingProviderError(
                    f"{config.provider} returned HTTP {exc.response.status_code}: {body_text}"
                ) from exc
            data = response.json()

        # OpenRouter and some compatible gateways return HTTP 200 with an error
        # payload, or a success body without data — never index blindly.
        if "error" in data:
            error = data["error"]
            message = error.get("message") if isinstance(error, dict) else str(error)
            code = error.get("code") if isinstance(error, dict) else None
            raise EmbeddingProviderError(
                f"{config.provider} returned an error (code={code}): {message}"
            )
        items = data.get("data") or []
        if len(items) != len(texts):
            raise EmbeddingProviderError(
                f"{config.provider} returned {len(items)} embeddings for {len(texts)} texts"
            )
        # The API guarantees input order via each item's int index; a
        # non-conformant gateway that reorders data[] must not silently
        # mis-assign vectors to texts — sort when every item carries an index.
        if all(isinstance(item.get("index"), int) for item in items):
            items = sorted(items, key=lambda item: item["index"])
        vectors: list[list[float]] = []
        for item in items:
            embedding = item.get("embedding")
            if embedding is None:
                raise EmbeddingProviderError(
                    f"{config.provider} returned an item without embedding"
                )
            vectors.append(list(embedding))
        return vectors
