"""History-route WebUI tests (0.22.0, DOC_TIMELINE rework).

The standalone Time-Machine page is gone: the library-wide history section
lives on the tree page, the per-document timeline on the document page (the
page-state coverage moved to test_webui.py). This file keeps the real-backend
mutation tests (revert/reset/pull/restore through the routes, skip-guarded on
a real git binary), the legacy ``/library/time-machine`` 301 redirects, and
the CSRF / login gating of the moved routes.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from urllib.parse import unquote_plus

import pytest
from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from athenaeum import db as db_module
from athenaeum import security
from athenaeum.library.backend import LibraryBackend, provision_library
from athenaeum.library.gittool import GitError
from athenaeum.webui import ROUTERS, deps
from conftest import CsrfTestClient

SECRET = "test-secret-key"

skipif_no_git = pytest.mark.skipif(shutil.which("git") is None, reason="git binary required")

_real_get_library_backend = deps.get_library_backend


class FakeBackend:
    """Stand-in for the backend history surface (default factory backend)."""

    def __init__(self, commits=(), *, diff="", configured=True, available=True):
        self._commits = list(commits)
        self._diff = diff
        self.history_configured = configured
        self.history_available = available

    def list_commits(self, limit: int = 200) -> list[dict]:
        return self._commits

    def git_head(self) -> str | None:
        return self._commits[0]["sha"] if self._commits else None

    def commit_diff(self, sha: str) -> str:
        if not any(c["sha"] == sha for c in self._commits):
            raise GitError(f"unknown commit: {sha}")
        return self._diff

    def revert_commit(self, sha: str) -> None:
        return None

    def reset_to_commit(self, sha: str) -> None:
        return None

    def git_pull(self) -> None:
        return None


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ATHENAEUM_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("ATHENAEUM_SECRET_KEY", SECRET)

    backends: dict[str, object] = {}

    def fake_backend_factory(settings, user, conn):
        user_id = user["id"]
        if user_id not in backends:
            backends[user_id] = FakeBackend()
        return backends[user_id]

    monkeypatch.setattr(deps, "get_library_backend", fake_backend_factory)

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key=SECRET)
    for router in ROUTERS:
        app.include_router(router)

    client = CsrfTestClient(app, follow_redirects=False)
    return client, backends, tmp_path


def make_user(data_root, username, password):
    db_path = Path(data_root) / "app.db"
    db_module.init_db(db_path)
    conn = db_module.connect(db_path)
    try:
        user = db_module.create_user(conn, username, security.hash_password(password))
    finally:
        conn.close()
    provision_library(data_root, user["id"])
    return user


def login(client, username, password):
    response = client.post("/login", data={"username": username, "password": password})
    assert response.status_code == 303
    return response


def set_remote_url(data_root, user_id, remote_url):
    conn = db_module.connect(Path(data_root) / "app.db")
    try:
        db_module.update_library_settings(
            conn,
            user_id,
            name=None,
            description=None,
            git_enabled=True,
            git_remote_url=remote_url,
            git_auto_push=False,
            trace_keep=0,
            activity_keep=0,
        )
    finally:
        conn.close()


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


# --- gating ---------------------------------------------------------------------


def test_history_routes_require_login(env):
    client, _, data_root = env
    make_user(data_root, "alice", "pw")
    sha = "b" * 40
    for url in (
        "/library/tree",
        "/library/document?path=/concepts/alpha.md",
        "/library/diff?commit=abc123",
    ):
        response = client.get(url)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"
    for url in (
        f"/library/history/{sha}/revert",
        "/library/history/reset",
        "/library/history/pull",
        "/library/document/restore",
    ):
        response = client.post(url, data={"sha": sha, "path": "/a.md"})
        assert response.status_code == 303
        assert response.headers["location"] == "/login"


def test_post_routes_require_csrf(env):
    client, _, data_root = env
    make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")
    sha = "b" * 40
    assert client.post(f"/library/history/{sha}/revert", csrf=False).status_code == 403
    assert client.post("/library/history/reset", data={"sha": sha}, csrf=False).status_code == 403
    assert client.post("/library/history/pull", csrf=False).status_code == 403
    assert (
        client.post(
            "/library/document/restore",
            data={"path": "/a.md", "sha": sha},
            csrf=False,
        ).status_code
        == 403
    )


# --- legacy Time-Machine URL redirects (301) ------------------------------------


def test_legacy_time_machine_urls_redirect(env):
    client, _, data_root = env
    make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")

    response = client.get("/library/time-machine")
    assert response.status_code == 301
    assert response.headers["location"] == "/library/tree"

    response = client.get("/library/time-machine/diff", params={"commit": "abc1234"})
    assert response.status_code == 301
    assert response.headers["location"] == "/library/diff?commit=abc1234"

    # a 301 on a POST is re-issued as GET by browsers (stale pre-upgrade pages)
    sha = "b" * 40
    for url in (
        f"/library/time-machine/{sha}/revert",
        "/library/time-machine/reset",
        "/library/time-machine/pull",
    ):
        response = client.post(url)
        assert response.status_code == 301
        assert response.headers["location"] == "/library/tree"


# --- mutations through the real backend ----------------------------------------


@skipif_no_git
def test_revert_route_with_real_backend(env, monkeypatch, tmp_path):
    client, _, data_root = env
    user = make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")

    lib = tmp_path / "library"
    backend = LibraryBackend(lib, actor="athenaeum-test/0.0.0")
    backend.init_bundle()
    backend.create_concept("/concepts/alpha.md", {"title": "Alpha", "type": "Note"}, "v1\n")
    backend.edit_concept("/concepts/alpha.md", new_body="v2\n")
    monkeypatch.setattr(deps, "get_library_backend", lambda settings, user, conn: backend)

    commits = backend.list_commits()
    assert len(commits) == 3
    edit, _, init = commits  # newest first

    response = client.get("/library/tree")
    assert response.status_code == 200
    assert edit["subject"] in response.text

    # reverting the edit restores the previous body
    response = client.post(f"/library/history/{edit['sha']}/revert")
    assert response.status_code == 303
    assert response.headers["location"] == "/library/tree?msg=Commit+reverted."
    assert backend.read_document("/concepts/alpha.md")["body"] == "v1\n"

    # the root commit cannot be reverted (would break the bundle)
    response = client.post(f"/library/history/{init['sha']}/revert")
    assert response.status_code == 303
    location = unquote_plus(response.headers["location"])
    assert location.startswith("/library/tree?")
    assert "error=" in location
    assert "cannot revert the initial commit" in location
    assert backend.read_document("/concepts/alpha.md")["body"] == "v1\n"

    # both attempts journaled
    conn = db_module.connect(Path(data_root) / "app.db")
    try:
        rows = db_module.list_activity(conn, user["id"], limit=50)
    finally:
        conn.close()
    outcomes = sorted(r["outcome"] for r in rows if r["tool"] == "time_machine_revert")
    assert outcomes == ["error", "ok"]


@skipif_no_git
def test_reset_route_with_real_backend(env, monkeypatch, tmp_path):
    client, _, data_root = env
    make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")

    lib = tmp_path / "library"
    backend = LibraryBackend(lib, actor="athenaeum-test/0.0.0")
    backend.init_bundle()
    backend.create_concept("/concepts/alpha.md", {"title": "Alpha", "type": "Note"}, "v1\n")
    backend.edit_concept("/concepts/alpha.md", new_body="v2\n")
    backend.edit_concept("/concepts/alpha.md", new_body="v3\n")
    monkeypatch.setattr(deps, "get_library_backend", lambda settings, user, conn: backend)

    target = backend.list_commits()[2]  # the creation commit (v1 state)

    # resetting to HEAD is refused
    head = backend.list_commits()[0]
    response = client.post("/library/history/reset", data={"sha": head["sha"]})
    assert response.status_code == 303
    assert "already at this commit" in unquote_plus(response.headers["location"])

    response = client.post("/library/history/reset", data={"sha": target["sha"]})
    assert response.status_code == 303
    assert (
        response.headers["location"]
        == "/library/tree?msg=Library+reset.+Use+the+slider+again+to+undo."
    )
    assert backend.read_document("/concepts/alpha.md")["body"] == "v1\n"

    # undo through the same UI: the pre-reset HEAD is the reset commit's parent
    after = backend.list_commits()
    assert after[0]["subject"].startswith("Update: Reset library to ")
    response = client.post("/library/history/reset", data={"sha": after[1]["sha"]})
    assert response.status_code == 303
    assert backend.read_document("/concepts/alpha.md")["body"] == "v3\n"


@skipif_no_git
def test_pull_route_requires_remote(env):
    client, _, data_root = env
    make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")
    response = client.post("/library/history/pull")
    assert response.status_code == 303
    assert "No+remote+configured." in response.headers["location"]


@skipif_no_git
def test_pull_route_unreachable_remote_flashes_error(env, monkeypatch, tmp_path):
    client, _, data_root = env
    user = make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")
    set_remote_url(data_root, user["id"], str(tmp_path / "missing.git"))
    monkeypatch.setattr(deps, "get_library_backend", _real_get_library_backend)

    response = client.post("/library/history/pull")
    assert response.status_code == 303
    assert "error=" in response.headers["location"]

    conn = db_module.connect(Path(data_root) / "app.db")
    try:
        rows = db_module.list_activity(conn, user["id"], limit=50)
    finally:
        conn.close()
    pull_rows = [r for r in rows if r["tool"] == "time_machine_pull"]
    assert [r["outcome"] for r in pull_rows] == ["error"]


@skipif_no_git
def test_pull_route_with_real_remote(env, monkeypatch, tmp_path):
    """Full wiring: real backend on the provisioned library, local bare remote."""
    client, _, data_root = env
    user = make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")

    bare = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(bare))
    set_remote_url(data_root, user["id"], str(bare))
    monkeypatch.setattr(deps, "get_library_backend", _real_get_library_backend)

    lib = Path(data_root) / "users" / user["id"] / "library"
    # the first page load constructs the backend; origin is wired explicitly
    # here (the app wires it lazily via ensure() on the next write)
    assert client.get("/library/tree").status_code == 200
    _git(lib, "remote", "add", "origin", str(bare))
    _git(lib, "push", "-u", "origin", "main")

    other = tmp_path / "other"
    _git(tmp_path, "clone", str(bare), str(other))
    _git(other, "config", "user.name", "Test")
    _git(other, "config", "user.email", "test@localhost")
    (other / "pulled.md").write_text("from remote\n", encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "remote commit")
    _git(other, "push")

    response = client.post("/library/history/pull")
    assert response.status_code == 303
    assert response.headers["location"] == "/library/tree?msg=Pulled."
    assert (lib / "pulled.md").read_text(encoding="utf-8") == "from remote\n"


@skipif_no_git
def test_pull_route_evicts_manager(env, monkeypatch, tmp_path):
    """A successful pull evicts the cached librarian (0.22.0 sync fix):
    embedding/FTS reconciliation runs on the next agent entry."""
    client, _, data_root = env
    user = make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")

    bare = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(bare))
    set_remote_url(data_root, user["id"], str(bare))
    monkeypatch.setattr(deps, "get_library_backend", _real_get_library_backend)

    class FakeManager:
        def __init__(self):
            self.evicted: list[str] = []

        def evict(self, user_id: str) -> None:
            self.evicted.append(user_id)

    manager = FakeManager()
    client.app.state.librarian_manager = manager

    lib = Path(data_root) / "users" / user["id"] / "library"
    assert client.get("/library/tree").status_code == 200
    _git(lib, "remote", "add", "origin", str(bare))
    _git(lib, "push", "-u", "origin", "main")

    other = tmp_path / "other"
    _git(tmp_path, "clone", str(bare), str(other))
    _git(other, "config", "user.name", "Test")
    _git(other, "config", "user.email", "test@localhost")
    (other / "pulled.md").write_text("from remote\n", encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "remote commit")
    _git(other, "push")

    response = client.post("/library/history/pull")
    assert response.status_code == 303
    assert response.headers["location"] == "/library/tree?msg=Pulled."
    assert manager.evicted == [user["id"]]


@skipif_no_git
def test_restore_route_with_real_backend(env, monkeypatch, tmp_path):
    client, _, data_root = env
    user = make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")

    lib = tmp_path / "library"
    backend = LibraryBackend(lib, actor="athenaeum-test/0.0.0")
    backend.init_bundle()
    backend.create_concept("/concepts/alpha.md", {"title": "Alpha", "type": "Note"}, "v1\n")
    create_sha = backend.list_commits()[0]["sha"]
    backend.edit_concept("/concepts/alpha.md", new_body="v2\n")
    monkeypatch.setattr(deps, "get_library_backend", lambda settings, user, conn: backend)

    # the restore lands as one new commit and flashes msg=
    response = client.post(
        "/library/document/restore", data={"path": "/concepts/alpha.md", "sha": create_sha}
    )
    assert response.status_code == 303
    location = unquote_plus(response.headers["location"])
    assert location.startswith("/library/document?")
    assert f"msg=Restored to commit {create_sha[:7]}." in location
    assert backend.read_document("/concepts/alpha.md")["body"] == "v1\n"

    # a no-op restore is refused with an error flash
    response = client.post(
        "/library/document/restore", data={"path": "/concepts/alpha.md", "sha": create_sha}
    )
    assert response.status_code == 303
    assert "already at this state" in unquote_plus(response.headers["location"])

    # restoring from a commit predating the file is refused (never deletes)
    init_sha = backend.list_commits()[-1]["sha"]
    response = client.post(
        "/library/document/restore", data={"path": "/concepts/alpha.md", "sha": init_sha}
    )
    assert response.status_code == 303
    assert "did not exist" in unquote_plus(response.headers["location"])
    assert backend.read_document("/concepts/alpha.md")["body"] == "v1\n"

    # all three attempts journaled
    conn = db_module.connect(Path(data_root) / "app.db")
    try:
        rows = db_module.list_activity(conn, user["id"], limit=50)
    finally:
        conn.close()
    outcomes = sorted(r["outcome"] for r in rows if r["tool"] == "document_restore")
    assert outcomes == ["error", "error", "ok"]
