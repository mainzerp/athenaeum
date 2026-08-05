#!/usr/bin/env python
"""Ad-hoc search latency benchmark against the live data in the container.

Run inside the container:  docker compose exec -T athenaeum python - < scripts/search_perf_bench.py

Builds the same objects LibrarianManager builds for the configured user
(local fastembed embeddings + FTS + cross-encoder reranker) and times
backend.search_semantic over several runs. Stage timings come from the
DEBUG logs added in the SEARCH_PERF change.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.DEBUG, format="%(name)s %(levelname)s %(message)s", stream=sys.stdout)
for noisy in ("mcp", "uvicorn", "httpx", "httpcore", "fastembed", "onnxruntime"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

from athenaeum.embeddings import EmbeddingService
from athenaeum.fts import FtsIndex
from athenaeum.librarian.embed import EmbeddingConfig, create_embedding_provider
from athenaeum.library.backend import LibraryBackend
from athenaeum.library.hybrid import CrossEncoderReranker

DATA = Path("/data")
UID = "e92ed9cf-4bdf-440c-9cc2-7c33bc0a8b4b"
QUERIES = [
    "hybrid search reranker lessons",
    "git time machine isolation",
    "librarian no-write detection",
]


def build(rerank: bool) -> LibraryBackend:
    cfg = EmbeddingConfig(source="local", model="sentence-transformers/all-MiniLM-L6-v2")
    provider = create_embedding_provider(cfg, cache_dir=DATA / "embedding-models")
    fts = FtsIndex(DATA / "app.db", UID)
    svc = EmbeddingService(DATA / "app.db", UID, cfg, provider, fts=fts)
    reranker = CrossEncoderReranker(cache_dir=DATA / "embedding-models") if rerank else None
    return LibraryBackend(
        DATA / "users" / UID / "library",
        actor="bench",
        embedding_service=svc,
        hybrid_search=True,
        hybrid_rerank=rerank,
        reranker=reranker,
    )


async def main() -> None:
    rerank = len(sys.argv) > 1 and sys.argv[1] == "rerank"
    backend = build(rerank)
    print(f"BENCH mode: hybrid_rerank={rerank}", flush=True)
    for i, query in enumerate(QUERIES, 1):
        t0 = time.perf_counter()
        hits = await backend.search_semantic(query, limit=8)
        dt = time.perf_counter() - t0
        print(f"BENCH run={i} query={query!r} total={dt:.3f}s hits={len(hits)}", flush=True)


asyncio.run(main())
