"""LibrarianManager: per-user Librarian registry.

Contract: plan section 2 (stream B checklist). Lazy init from the
``librarian_configs`` DB row (schema: plan section 3.5), dict cache,
idle-timeout eviction with config reload on next access.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from athenaeum import db, isolation
from athenaeum.computation import ComputationRunner
from athenaeum.embeddings import EmbeddingService, EmbedStatusRegistry
from athenaeum.fts import FtsIndex
from athenaeum.librarian.agent import Librarian, LibrarianConfig, LibrarianNotConfiguredError
from athenaeum.librarian.embed import EmbeddingConfig, create_embedding_provider
from athenaeum.librarian.gate import RunGate
from athenaeum.librarian.llm import LLMConfig, create_provider
from athenaeum.library.hybrid import CrossEncoderReranker

if TYPE_CHECKING:
    from athenaeum.librarian.embed import EmbeddingProvider
    from athenaeum.librarian.llm import LLMProvider
    from athenaeum.librarian.tools import Backend

logger = logging.getLogger(__name__)

DEFAULT_IDLE_TIMEOUT = 30 * 60  # 30 minutes, seconds

_CONFIG_COLUMNS = (
    "llm_model, prompt_addendum, trace_keep, activity_keep, payload_keep, "
    "git_enabled, git_remote_url, git_auto_push, library_name, library_description, "
    "librarian_connection_id, curator_connection_id, curator_model, "
    "curate_last_run_at, curate_prompt_addendum, "
    "embedding_source, embedding_model, embedding_connection_id, semantic_threshold, "
    "hybrid_search, hybrid_rerank"
)


class LibrarianManager:
    """Maps user_id -> Librarian, lazily created, cached, idle-evicted."""

    def __init__(
        self,
        db_path: str | Path,
        data_root: str | Path,
        *,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
        clock: Callable[[], float] = time.monotonic,
        key_decryptor: Callable[[str], str] | None = None,
        backend_factory: Callable[[str, Path, LibrarianConfig], Backend] | None = None,
        provider_factory: Callable[[str, LLMConfig], LLMProvider] | None = None,
        embedding_provider_factory: Callable[[EmbeddingConfig], EmbeddingProvider] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.data_root = Path(data_root)
        self.idle_timeout = idle_timeout
        self._clock = clock
        # Fernet decrypt seam for provider_configs.api_key_enc; security.py
        # (Stream C) supplies the real decryptor at integration.
        # None = value is plaintext.
        self._key_decryptor = key_decryptor
        # One shared runner for Attested Computations (0.21.0): it owns the
        # toggle check, connection lookup, and credential decryption, so
        # neither the MCP layer nor the Librarian needs the decryptor.
        self.computation_runner = ComputationRunner(self.db_path, self._key_decryptor)
        self._backend_factory = backend_factory
        self._provider_factory = provider_factory
        self._embedding_provider_factory = embedding_provider_factory
        self._embed_status = EmbedStatusRegistry()
        self._cache: dict[str, tuple[Librarian, float]] = {}
        # Shared run gate: every built Librarian serializes same-kind runs
        # with the MCP tools and the scheduler (see librarian/gate.py).
        self.run_gate = RunGate()
        # Guards ALL _cache reads/mutations (A4): callers run on the event
        # loop AND the WebUI threadpool, so an unguarded evict between the
        # `in` check and the index could KeyError. Builds do file I/O under
        # the same lock, so a threaded double-get on a cold user cannot
        # build twice.
        self._get_lock = threading.Lock()

    def library_root(self, user_id: str) -> Path:
        # Cheap shape assertion: the user_id becomes path segments, so reject
        # traversal/slashes before any directory is touched (slugs allowed).
        isolation.validate_user_id(user_id)
        return self.data_root / "users" / user_id / "library"

    def _decrypt_api_key(self, user_id: str, connection_id: str, api_key: str) -> str:
        """Decrypt a stored provider key; surface failures as config errors.

        A rotated ``ATHENAEUM_SECRET_KEY`` makes stored Fernet ciphertexts
        undecryptable; an unclassified ``InvalidToken`` escaping from
        ``_load_config`` bricks every agent call for the user with a generic
        internal error (AGENT-10). Raise the targeted configuration error
        instead (the existing not-configured error path).
        """
        if self._key_decryptor is None or not api_key:
            return api_key
        try:
            return self._key_decryptor(api_key)
        except Exception as exc:
            logger.warning(
                "undecryptable api key for user %s connection %s", user_id, connection_id
            )
            raise LibrarianNotConfiguredError(
                "provider key undecryptable; re-enter the API key in the librarian "
                "settings (secret key rotated?)"
            ) from exc

    def _load_config(self, user_id: str) -> LibrarianConfig:
        """Read the librarian_configs row; absent row = unconfigured defaults."""
        with db.connect(self.db_path) as conn:
            row = conn.execute(
                f"SELECT {_CONFIG_COLUMNS} FROM librarian_configs WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if row is None:
                return LibrarianConfig(user_id=user_id)
            conn_rows = conn.execute(
                "SELECT id, provider, api_key_enc, base_url, max_iterations,"
                " temperature, max_tokens, is_default"
                " FROM provider_configs WHERE user_id = ?",
                (user_id,),
            ).fetchall()
        # Named access tied to _CONFIG_COLUMNS (A14): a positional unpack
        # silently mis-assigns every value when a column is added or
        # reordered without touching the unpack site.
        llm_model = row["llm_model"]
        prompt_addendum = row["prompt_addendum"]
        trace_keep = row["trace_keep"]
        activity_keep = row["activity_keep"]
        payload_keep = row["payload_keep"]
        # NOT NULL DEFAULT columns — never NULL (0.22.0 git history).
        git_enabled = bool(row["git_enabled"])
        git_remote_url = row["git_remote_url"]
        git_auto_push = bool(row["git_auto_push"])
        library_name = row["library_name"]
        library_description = row["library_description"]
        librarian_connection_id = row["librarian_connection_id"]
        curator_connection_id = row["curator_connection_id"]
        curator_model = row["curator_model"]
        curate_last_run_at = row["curate_last_run_at"]
        curate_prompt_addendum = row["curate_prompt_addendum"]
        embedding_source = row["embedding_source"]
        embedding_model = row["embedding_model"]
        embedding_connection_id = row["embedding_connection_id"]
        semantic_threshold = row["semantic_threshold"]
        # NOT NULL DEFAULT 1 columns — never NULL (0.19.0 hybrid toggles).
        hybrid_search = bool(row["hybrid_search"])
        hybrid_rerank = bool(row["hybrid_rerank"])
        by_id = {conn_row["id"]: conn_row for conn_row in conn_rows}
        default_row = next((r for r in conn_rows if r["is_default"]), None)

        def resolve(connection_id: str | None):
            if connection_id:
                return by_id.get(connection_id)
            return default_row

        def build_llm(conn_row, model: str | None) -> LLMConfig:
            api_key = conn_row["api_key_enc"] if conn_row["api_key_enc"] is not None else ""
            api_key = self._decrypt_api_key(user_id, conn_row["id"], api_key)
            # L22: `is not None` coercions — a stored 0 (e.g. max_iterations,
            # retention keeps) is a legitimate value, not "unset".
            return LLMConfig(
                provider=conn_row["provider"],
                model=model if model is not None else "",
                api_key=api_key,
                base_url=conn_row["base_url"],
                max_iterations=(
                    conn_row["max_iterations"] if conn_row["max_iterations"] is not None else 10
                ),
                temperature=conn_row["temperature"],
                max_tokens=conn_row["max_tokens"],
            )

        lib_conn = resolve(librarian_connection_id)
        llm: LLMConfig | None = build_llm(lib_conn, llm_model) if lib_conn is not None else None
        # A21: curate_llm stays None when no curator binding exists — None is
        # the explicit "inherit the librarian config" marker the agent
        # dispatches on (no shared-object identity involved).
        curate_llm: LLMConfig | None = None
        cur_conn = resolve(curator_connection_id)
        if (
            curator_connection_id is not None or curator_model is not None
        ) and cur_conn is not None:
            curate_llm = build_llm(cur_conn, curator_model or llm_model)
        embedding: EmbeddingConfig | None = None
        if embedding_source and embedding_model:
            if embedding_source == "local":
                embedding = EmbeddingConfig(source="local", model=embedding_model)
            elif embedding_source == "api":
                emb_conn = resolve(embedding_connection_id)
                if emb_conn is not None and emb_conn["provider"] != "anthropic":
                    api_key = emb_conn["api_key_enc"] if emb_conn["api_key_enc"] is not None else ""
                    api_key = self._decrypt_api_key(user_id, emb_conn["id"], api_key)
                    embedding = EmbeddingConfig(
                        source="api",
                        model=embedding_model,
                        provider=emb_conn["provider"],
                        api_key=api_key,
                        base_url=emb_conn["base_url"],
                    )
                else:
                    logger.warning(
                        "embedding config for user %s ignored: connection dangling or "
                        "anthropic (no embeddings endpoint)",
                        user_id,
                    )
            else:
                logger.warning(
                    "embedding config for user %s ignored: unknown source %r",
                    user_id,
                    embedding_source,
                )
        return LibrarianConfig(
            user_id=user_id,
            llm=llm,
            curate_llm=curate_llm,
            prompt_addendum=prompt_addendum,
            trace_keep=trace_keep if trace_keep is not None else 0,
            activity_keep=activity_keep if activity_keep is not None else 0,
            payload_keep=payload_keep if payload_keep is not None else 0,
            git_enabled=git_enabled,
            git_remote_url=git_remote_url,
            git_auto_push=git_auto_push,
            library_name=library_name,
            library_description=library_description,
            curate_last_run_at=curate_last_run_at,
            curate_prompt_addendum=curate_prompt_addendum,
            embedding=embedding,
            semantic_threshold=semantic_threshold,
            hybrid_search=hybrid_search,
            hybrid_rerank=hybrid_rerank,
        )

    def _build(self, user_id: str) -> Librarian:
        config = self._load_config(user_id)
        root = self.library_root(user_id)
        backend = (
            self._backend_factory(user_id, root, config)
            if self._backend_factory is not None
            else None
        )
        provider = None
        if config.llm is not None:
            factory = self._provider_factory or (lambda _uid, llm: create_provider(llm))
            provider = factory(user_id, config.llm)
        embedding_service = None
        fts_index = None
        reranker = None
        if config.embedding is not None:
            try:
                embed_factory = self._embedding_provider_factory or (
                    lambda cfg: create_embedding_provider(
                        cfg, cache_dir=self.data_root / "embedding-models"
                    )
                )
                # The FTS index rides the embedding service's flows (hybrid
                # search lexical leg); it only exists where a service exists.
                fts_index = FtsIndex(self.db_path, user_id)
                embedding_service = EmbeddingService(
                    self.db_path,
                    user_id,
                    config.embedding,
                    embed_factory(config.embedding),
                    status=self._embed_status,
                    fts=fts_index,
                )
                if config.hybrid_rerank:
                    # Construction stores config only; the ONNX model loads
                    # lazily on first rerank (downloads on first use).
                    reranker = CrossEncoderReranker(cache_dir=self.data_root / "embedding-models")
            except Exception as exc:
                # A broken embedding config must never break librarian
                # construction; the tool-level "unconfigured" path then applies.
                logger.warning(
                    "embedding service construction failed for user %s: %s", user_id, exc
                )
                embedding_service = None
        return Librarian(
            root,
            config,
            backend=backend,
            provider=provider,
            embedding_service=embedding_service,
            run_gate=self.run_gate,
            reranker=reranker,
            computation_runner=self.computation_runner,
        )

    def get(self, user_id: str) -> Librarian:
        """Return the user's librarian, creating and caching it on first use."""
        self.evict_idle()
        with self._get_lock:
            if user_id in self._cache:
                librarian, _ = self._cache[user_id]
                self._cache[user_id] = (librarian, self._clock())
                return librarian
            librarian = self._build(user_id)
            self._cache[user_id] = (librarian, self._clock())
            return librarian

    def curate_last_run_at(self, user_id: str) -> str | None:
        """Fresh curate baseline from the DB row (bypasses the cached config)."""
        with db.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT curate_last_run_at FROM librarian_configs WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return row["curate_last_run_at"] if row is not None else None

    def set_curate_last_run(self, user_id: str, ts: str) -> None:
        """Persist the curate run-end timestamp."""
        with db.connect(self.db_path) as conn:
            db.set_curate_last_run(conn, user_id, ts)

    def evict(self, user_id: str) -> None:
        """Drop a user's cached librarian (e.g. after a config change)."""
        with self._get_lock:
            entry = self._cache.pop(user_id, None)
        if entry is not None:
            entry[0].shutdown()  # cancel its pending embed reconcile (A5)

    def evict_idle(self) -> int:
        """Evict librarians idle longer than idle_timeout. Returns evicted count."""
        now = self._clock()
        with self._get_lock:
            idle = [
                user_id
                for user_id, (_, last_used) in self._cache.items()
                if now - last_used > self.idle_timeout
            ]
            evicted = [self._cache.pop(user_id)[0] for user_id in idle]
        for librarian in evicted:
            librarian.shutdown()  # cancel pending embed reconciles (A5)
        return len(evicted)

    def close(self) -> None:
        """Evict every cached librarian and cancel its background work."""
        with self._get_lock:
            evicted = [librarian for librarian, _ in self._cache.values()]
            self._cache.clear()
        for librarian in evicted:
            librarian.shutdown()

    def cached_user_ids(self) -> list[str]:
        with self._get_lock:
            return list(self._cache)

    def embed_status_for(self, user_id: str) -> dict | None:
        """Current embedding reconcile status (consumed by the WebUI card)."""
        return self._embed_status.get(user_id)
