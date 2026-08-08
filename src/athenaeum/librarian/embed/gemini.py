"""Google Gemini embedContent adapter (one call per text)."""

from __future__ import annotations

import httpx

from athenaeum.librarian.embed import (
    KIND_DOCUMENT,
    KIND_QUERY,
    EmbeddingConfig,
    EmbeddingProviderError,
)

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


class GeminiEmbeddingProvider:
    """Thin REST mapping onto POST {base_url}/models/{model}:embedContent."""

    async def embed(
        self,
        texts: list[str],
        config: EmbeddingConfig,
        *,
        kind: str = KIND_DOCUMENT,
    ) -> list[list[float]]:
        base_url = (config.base_url or DEFAULT_BASE_URL).rstrip("/")
        task_type = "RETRIEVAL_QUERY" if kind == KIND_QUERY else "RETRIEVAL_DOCUMENT"

        vectors: list[list[float]] = []
        async with httpx.AsyncClient(timeout=60.0) as client:
            for text in texts:
                response = await client.post(
                    f"{base_url}/models/{config.model}:embedContent",
                    headers={"x-goog-api-key": config.api_key},
                    json={
                        "content": {"parts": [{"text": text}]},
                        "taskType": task_type,
                    },
                )
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    # Mirror llm.http_status_error: HTTP failures surface as
                    # EmbeddingProviderError, never raw httpx exceptions.
                    body_text = exc.response.text[:200]
                    raise EmbeddingProviderError(
                        f"gemini returned HTTP {exc.response.status_code}: {body_text}"
                    ) from exc
                data = response.json()
                values = (data.get("embedding") or {}).get("values")
                if not values:
                    raise EmbeddingProviderError(
                        "gemini returned a response without embedding values"
                    )
                vectors.append(list(values))
        return vectors
