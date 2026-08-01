"""Local fastembed (ONNX) embedding provider — import-guarded optional extra.

fastembed ships as the ``local`` optional dependency; it must never be
required to import this module. The blocking ONNX inference runs via
``asyncio.to_thread`` so the single-worker event loop is never stalled.
Model weights download from HuggingFace on first use; ``cache_dir`` must
live under the data root so models survive container recreation.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from athenaeum.librarian.embed import (
    KIND_DOCUMENT,
    KIND_QUERY,
    EmbeddingConfig,
    EmbeddingProviderError,
)

# Curated shortlist (locked decision k): (model name, dims) with dims in the
# WebUI labels. Hardcoded — fastembed may be absent, so its supported-model
# listing is never consulted here.
LOCAL_MODEL_SHORTLIST: list[tuple[str, int]] = [
    ("BAAI/bge-small-en-v1.5", 384),
    ("BAAI/bge-base-en-v1.5", 768),
    ("sentence-transformers/all-MiniLM-L6-v2", 384),
    ("intfloat/multilingual-e5-small", 384),
]

DEFAULT_LOCAL_MODEL = LOCAL_MODEL_SHORTLIST[0][0]


def _requires_e5_prefixes(model_name: str) -> bool:
    """Only the E5 family requires ``query:``/``passage:`` prefixes (L9).

    BGE models specify a different query instruction and no passage prefix;
    MiniLM has no prefix convention at all. Applying E5 prefixes to those
    models produces vectors off the calibrated space the per-model duplicate
    thresholds presume.
    """
    return "e5-" in model_name.lower()


class LocalFastembedProvider:
    """fastembed TextEmbedding wrapper; the model is constructed lazily and
    memoized per model name on first embed call."""

    def __init__(self, cache_dir: Path | None = None) -> None:
        self._cache_dir = cache_dir
        self._models: dict[str, object] = {}
        # Construction now happens on worker threads (A6); guard the memo.
        self._models_lock = threading.Lock()

    def _model(self, model_name: str):
        with self._models_lock:
            if model_name not in self._models:
                try:
                    from fastembed import TextEmbedding
                except ImportError:
                    raise EmbeddingProviderError(
                        "local embeddings require the 'local' extra (pip install athenaeum[local])"
                    ) from None
                kwargs = {"model_name": model_name}
                if self._cache_dir is not None:
                    kwargs["cache_dir"] = str(self._cache_dir)
                self._models[model_name] = TextEmbedding(**kwargs)
            return self._models[model_name]

    async def embed(
        self,
        texts: list[str],
        config: EmbeddingConfig,
        *,
        kind: str = KIND_DOCUMENT,
    ) -> list[list[float]]:
        # First construction downloads the ONNX weights — off the loop (A6).
        model = await asyncio.to_thread(self._model, config.model)
        # E5 prefix convention only: asymmetric retrieval prefixes per kind
        # (L9 — BGE/MiniLM embed the raw text).
        if _requires_e5_prefixes(config.model):
            prefix = "query: " if kind == KIND_QUERY else "passage: "
            texts = [prefix + text for text in texts]

        def _run() -> list[list[float]]:
            return [list(vector) for vector in model.embed(texts)]

        return await asyncio.to_thread(_run)
