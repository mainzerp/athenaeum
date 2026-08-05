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


# Process-wide model cache (0.23.0): ONNX sessions are expensive to build
# (~1 s+ on CPU) and independent of any per-user provider/librarian
# instance. Keyed by (model_name, cache_dir) so librarian eviction (30-min
# idle, config saves) never unloads a model; models stay warm for the
# process lifetime. Construction happens on worker threads; guard the memo.
_SHARED_MODELS: dict[tuple[str, str | None], object] = {}
_SHARED_MODELS_LOCK = threading.Lock()


def _shared_model(model_name: str, cache_dir: Path | None):
    key = (model_name, str(cache_dir) if cache_dir is not None else None)
    with _SHARED_MODELS_LOCK:
        if key not in _SHARED_MODELS:
            try:
                from fastembed import TextEmbedding
            except ImportError:
                raise EmbeddingProviderError(
                    "local embeddings require the 'local' extra (pip install athenaeum[local])"
                ) from None
            kwargs = {"model_name": model_name}
            if cache_dir is not None:
                kwargs["cache_dir"] = str(cache_dir)
            _SHARED_MODELS[key] = TextEmbedding(**kwargs)
        return _SHARED_MODELS[key]


def preload_local_models(model_names: list[str], cache_dir: Path | None) -> list[str]:
    """Eagerly construct the given models into the process-wide cache.

    Returns the names that loaded; failures are skipped (the first real
    embed call retries and surfaces the error). Called from the startup
    warm-up task; never raises for missing fastembed or download errors.
    """
    loaded = []
    for name in dict.fromkeys(model_names):
        try:
            _shared_model(name, cache_dir)
        except Exception:
            continue
        loaded.append(name)
    return loaded


class LocalFastembedProvider:
    """fastembed TextEmbedding wrapper; the model is constructed lazily and
    memoized per model name on first embed call."""

    def __init__(self, cache_dir: Path | None = None) -> None:
        self._cache_dir = cache_dir

    def _model(self, model_name: str):
        return _shared_model(model_name, self._cache_dir)

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
