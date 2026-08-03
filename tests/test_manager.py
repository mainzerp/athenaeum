"""Tests for LibrarianManager: lazy init, caching, idle eviction, config reload."""

import sqlite3
import threading
import time

from athenaeum import db as db_module
from athenaeum.librarian.manager import LibrarianManager


class FakeBackend:
    def status(self):
        return {"stats": {}, "health": {}, "healthy": True}

    def link_health(self, paths: list[str]) -> dict:
        return {}


class FakeProvider:
    async def complete(self, messages, tools, config):  # pragma: no cover
        raise NotImplementedError


def make_db(tmp_path, *, model="model-a", configured=True) -> str:
    db_path = tmp_path / "app.db"
    db_module.init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO users (id, username, password_hash, created_at) "
            "VALUES ('user-1', 'alice', 'hash', '2026-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO librarian_configs "
            "(user_id, llm_model, prompt_addendum, trace_keep, activity_keep, library_name) "
            "VALUES ('user-1', ?, 'custom prompt', 4, 9, 'Lib')",
            (model if configured else None,),
        )
        if configured:
            conn.execute(
                "INSERT INTO provider_configs "
                "(id, user_id, label, provider, api_key_enc, max_iterations,"
                " is_default, created_at) "
                "VALUES ('conn-a', 'user-1', 'Default', 'openai', 'enc-key', 7, 1,"
                " '2026-01-01T00:00:00Z')"
            )
    return str(db_path)


def make_manager(db_path, tmp_path, **kwargs) -> LibrarianManager:
    kwargs.setdefault("backend_factory", lambda user_id, root, config: FakeBackend())
    kwargs.setdefault("provider_factory", lambda user_id, llm: FakeProvider())
    return LibrarianManager(db_path, tmp_path / "data", **kwargs)


def test_lazy_creation(tmp_path):
    manager = make_manager(make_db(tmp_path), tmp_path)
    assert manager.cached_user_ids() == []  # nothing built before first get
    librarian = manager.get("user-1")
    assert manager.cached_user_ids() == ["user-1"]
    assert librarian.root == tmp_path / "data" / "users" / "user-1" / "library"


def test_config_row_loaded(tmp_path):
    manager = make_manager(make_db(tmp_path), tmp_path)
    librarian = manager.get("user-1")
    assert librarian.config.llm.provider == "openai"
    assert librarian.config.llm.model == "model-a"
    assert librarian.config.llm.max_iterations == 7
    assert librarian.config.prompt_addendum == "custom prompt"
    assert librarian.config.trace_keep == 4
    assert librarian.config.activity_keep == 9
    assert librarian.config.library_name == "Lib"
    # unbound curator fully inherits: curate_llm stays None (explicit
    # "inherit" marker, A21); the effective curate config is the librarian's
    assert librarian.config.curate_llm is None
    assert librarian._curate_llm() is librarian.config.llm
    assert librarian.config.curate_last_run_at is None
    assert librarian.config.curate_prompt_addendum is None
    assert librarian.configured


def test_semantic_threshold_loaded(tmp_path):
    db_path = make_db(tmp_path)
    manager = make_manager(db_path, tmp_path)
    # NULL by default: per-model resolution happens in the Librarian
    assert manager._load_config("user-1").semantic_threshold is None
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE librarian_configs SET semantic_threshold = 0.82 WHERE user_id = 'user-1'"
        )
    assert manager._load_config("user-1").semantic_threshold == 0.82


def test_hybrid_toggles_loaded(tmp_path):
    db_path = make_db(tmp_path)
    manager = make_manager(db_path, tmp_path)
    # NOT NULL DEFAULT 1: hybrid search and rerank default to on
    config = manager._load_config("user-1")
    assert config.hybrid_search is True
    assert config.hybrid_rerank is True
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE librarian_configs SET hybrid_search = 0, hybrid_rerank = 0"
            " WHERE user_id = 'user-1'"
        )
    config = manager._load_config("user-1")
    assert config.hybrid_search is False
    assert config.hybrid_rerank is False


def test_payload_keep_loaded(tmp_path):
    db_path = make_db(tmp_path)
    manager = make_manager(db_path, tmp_path)
    # the raw INSERT names no payload_keep: the create-DDL default applies
    assert manager._load_config("user-1").payload_keep == db_module.DEFAULT_PAYLOAD_KEEP
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE librarian_configs SET payload_keep = 7 WHERE user_id = 'user-1'")
    assert manager._load_config("user-1").payload_keep == 7
    # L22: a stored 0 is a legitimate value, not "unset"
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE librarian_configs SET payload_keep = 0 WHERE user_id = 'user-1'")
    assert manager._load_config("user-1").payload_keep == 0


def test_config_round_trip_preserves_stored_zero(tmp_path):
    """L22: a stored 0 is a legitimate value, not "unset" — no `or default`."""
    db_path = make_db(tmp_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE provider_configs SET max_iterations = 0 WHERE id = 'conn-a'")
        conn.execute(
            "UPDATE librarian_configs SET trace_keep = 0, activity_keep = 0,"
            " snapshot_keep = 0 WHERE user_id = 'user-1'"
        )
    manager = make_manager(db_path, tmp_path)
    config = manager.get("user-1").config
    assert config.llm.max_iterations == 0
    assert config.trace_keep == 0
    assert config.activity_keep == 0
    assert config.snapshot_keep == 0


def test_curator_explicit_connection_resolution(tmp_path):
    db_path = make_db(tmp_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO provider_configs "
            "(id, user_id, label, provider, api_key_enc, max_iterations,"
            " is_default, created_at) "
            "VALUES ('conn-b', 'user-1', 'Second', 'anthropic', 'enc-key-b', 5, 0,"
            " '2026-01-02T00:00:00Z')"
        )
        conn.execute(
            "UPDATE librarian_configs SET curator_connection_id = 'conn-b',"
            " curator_model = 'big' WHERE user_id = 'user-1'"
        )
    manager = make_manager(db_path, tmp_path, key_decryptor=lambda enc: f"dec:{enc}")
    config = manager.get("user-1").config
    assert config.curate_llm is not config.llm
    assert config.curate_llm.provider == "anthropic"
    assert config.curate_llm.model == "big"
    assert config.curate_llm.max_iterations == 5
    # the curator's key comes from its own connection row, via the decryptor
    assert config.curate_llm.api_key == "dec:enc-key-b"
    assert config.llm.api_key == "dec:enc-key"


def test_curator_empty_model_falls_back_to_librarian_model(tmp_path):
    db_path = make_db(tmp_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO provider_configs "
            "(id, user_id, label, provider, is_default, created_at) "
            "VALUES ('conn-b', 'user-1', 'Second', 'anthropic', 0,"
            " '2026-01-02T00:00:00Z')"
        )
        conn.execute(
            "UPDATE librarian_configs SET curator_connection_id = 'conn-b' WHERE user_id = 'user-1'"
        )
    manager = make_manager(db_path, tmp_path)
    config = manager.get("user-1").config
    assert config.curate_llm is not config.llm
    assert config.curate_llm.provider == "anthropic"
    assert config.curate_llm.model == "model-a"  # librarian model fallback


def test_curate_addendum_loaded(tmp_path):
    db_path = make_db(tmp_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE librarian_configs SET curate_prompt_addendum = 'rule' WHERE user_id = 'user-1'"
        )
    manager = make_manager(db_path, tmp_path)
    assert manager.get("user-1").config.curate_prompt_addendum == "rule"


def test_set_curate_last_run_persists_without_touching_cached_config(tmp_path):
    db_path = make_db(tmp_path)
    manager = make_manager(db_path, tmp_path)
    librarian = manager.get("user-1")
    assert librarian.config.curate_last_run_at is None

    manager.set_curate_last_run("user-1", "2026-07-29T00:00:00+00:00")

    # the cached config stays a build-time snapshot (no in-place mirror)
    assert manager.get("user-1") is librarian
    assert librarian.config.curate_last_run_at is None
    # the fresh DB read sees the new value
    assert manager.curate_last_run_at("user-1") == "2026-07-29T00:00:00+00:00"
    # persisted to the DB row; a reload after eviction sees it
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT curate_last_run_at FROM librarian_configs WHERE user_id = 'user-1'"
        ).fetchone()
    assert row[0] == "2026-07-29T00:00:00+00:00"
    manager.evict("user-1")
    assert manager.get("user-1").config.curate_last_run_at == "2026-07-29T00:00:00+00:00"


def test_threaded_double_get_builds_exactly_one_librarian(tmp_path):
    db_path = make_db(tmp_path)
    builds: list[str] = []

    def backend_factory(user_id, root, config):
        builds.append(user_id)
        time.sleep(0.1)  # widen the race window: threads pile onto the lock
        return FakeBackend()

    manager = make_manager(db_path, tmp_path, backend_factory=backend_factory)
    barrier = threading.Barrier(8)
    results: list = []

    def getter() -> None:
        barrier.wait(timeout=10)
        results.append(manager.get("user-1"))

    threads = [threading.Thread(target=getter) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive(), "manager.get deadlocked"
    assert len(results) == 8
    assert all(result is results[0] for result in results)
    assert builds == ["user-1"]


def test_caching_returns_same_instance(tmp_path):
    manager = make_manager(make_db(tmp_path), tmp_path)
    assert manager.get("user-1") is manager.get("user-1")


def test_concurrent_get_and_evict_never_raises(tmp_path):
    """A4: cache reads/mutations are lock-guarded; interleaved get/evict from
    threads must not KeyError between the `in` check and the index."""
    manager = make_manager(make_db(tmp_path), tmp_path)
    manager.get("user-1")  # warm the cache once
    errors: list[BaseException] = []
    barrier = threading.Barrier(9)
    stop = threading.Event()

    def getter() -> None:
        barrier.wait(timeout=10)
        while not stop.is_set():
            try:
                manager.get("user-1")
            except BaseException as exc:  # noqa: BLE001 - record, assert below
                errors.append(exc)
                return

    def evictor() -> None:
        barrier.wait(timeout=10)
        for _ in range(200):
            manager.evict("user-1")
            manager.evict_idle()
        stop.set()

    getters = [threading.Thread(target=getter) for _ in range(8)]
    threads = [*getters, threading.Thread(target=evictor)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive(), "manager get/evict deadlocked"
    assert errors == []
    # final state is consistent: either cached or rebuildable without error
    assert manager.get("user-1") is manager.get("user-1")


def test_idle_eviction_and_config_reload(tmp_path):
    db_path = make_db(tmp_path)
    now = [1000.0]
    manager = make_manager(db_path, tmp_path, idle_timeout=60.0, clock=lambda: now[0])

    first = manager.get("user-1")
    now[0] += 30  # within the idle window: still cached
    assert manager.get("user-1") is first

    # config change while cached: old instance keeps old config
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE librarian_configs SET llm_model = 'model-b' WHERE user_id = 'user-1'")
    now[0] += 120  # past the idle timeout
    assert manager.evict_idle() == 1
    second = manager.get("user-1")
    assert second is not first
    assert second.config.llm.model == "model-b"  # config reloaded after evict


def test_explicit_evict(tmp_path):
    manager = make_manager(make_db(tmp_path), tmp_path)
    manager.get("user-1")
    manager.evict("user-1")
    assert manager.cached_user_ids() == []


def test_unconfigured_user(tmp_path):
    manager = make_manager(make_db(tmp_path, configured=False), tmp_path)
    librarian = manager.get("user-1")
    assert not librarian.configured
    assert librarian.config.llm is None


def test_missing_config_row_means_unconfigured(tmp_path):
    db_path = make_db(tmp_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM librarian_configs")
    manager = make_manager(db_path, tmp_path)
    assert not manager.get("user-1").configured


def test_key_decryptor_seam(tmp_path):
    manager = make_manager(make_db(tmp_path), tmp_path, key_decryptor=lambda enc: f"dec:{enc}")
    librarian = manager.get("user-1")
    assert librarian.config.llm.api_key == "dec:enc-key"


def test_plaintext_key_without_decryptor(tmp_path):
    manager = make_manager(make_db(tmp_path), tmp_path)
    assert manager.get("user-1").config.llm.api_key == "enc-key"
