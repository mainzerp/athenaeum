"""History-route WebUI tests (0.22.0 DOC_TIMELINE rework; document-view takeover).

The standalone Time-Machine page and the library-wide history section are
gone: the document view lives at ``/library/tree`` with the per-document
timeline (the page-state coverage is in test_webui.py). This file keeps the
real-backend restore-mutation test (skip-guarded on a real git binary), the
legacy ``/library/time-machine`` 301 redirects, and the CSRF / login gating
of the surviving routes.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from urllib.parse import unquote_plus

import pytest
from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from athenaeum import db as db_module
from athenaeum import security
from athenaeum.library.backend import LibraryBackend, provision_library
from athenaeum.webui import ROUTERS, deps
from conftest import CsrfTestClient

SECRET = "test-secret-key-timemachine-0123456"  # >= 32 chars (SERVER-03 validator)

skipif_no_git = pytest.mark.skipif(shutil.which("git") is None, reason="git binary required")


class FakeBackend:
    """Stand-in for the backend history surface (default factory backend)."""

    def __init__(self, commits=(), *, configured=True, available=True):
        self._commits = list(commits)
        self.history_configured = configured
        self.history_available = available

    def list_commits(self, limit: int = 200) -> list[dict]:
        return self._commits

    def git_head(self) -> str | None:
        return self._commits[0]["sha"] if self._commits else None


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


# --- gating ---------------------------------------------------------------------


def test_history_routes_require_login(env):
    client, _, data_root = env
    make_user(data_root, "alice", "pw")
    sha = "b" * 40
    for url in (
        "/library/tree",
        "/library/tree?path=/concepts/alpha.md",
        "/library/document/data?path=/concepts/alpha.md",
        "/library/document/diff?path=/concepts/alpha.md&sha=abcd1234",
    ):
        response = client.get(url)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"
    for url in (
        "/library/document/restore",
        "/library/document/edit",
        "/library/document/delete",
    ):
        response = client.post(url, data={"sha": sha, "path": "/a.md", "body": "x\n"})
        assert response.status_code == 303
        assert response.headers["location"] == "/login"


def test_post_routes_require_csrf(env):
    client, _, data_root = env
    make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")
    sha = "b" * 40
    assert (
        client.post(
            "/library/document/restore",
            data={"path": "/a.md", "sha": sha},
            csrf=False,
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/library/document/edit",
            data={"path": "/a.md", "body": "x\n"},
            csrf=False,
        ).status_code
        == 403
    )
    assert (
        client.post("/library/document/delete", data={"path": "/a.md"}, csrf=False).status_code
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
    # the per-commit diff page is gone with the library-wide history section
    assert response.headers["location"] == "/library/tree"

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


# --- restore through the real backend --------------------------------------------


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
    assert location.startswith("/library/tree?")
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


# --- document slider order + preview diff endpoint (0.23.0 rework) ---------------


@skipif_no_git
def test_document_slider_order_and_diff_endpoint(env, monkeypatch, tmp_path):
    client, _, data_root = env
    make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")

    lib = tmp_path / "library"
    backend = LibraryBackend(lib, actor="athenaeum-test/0.0.0")
    backend.init_bundle()
    backend.create_concept("/concepts/alpha.md", {"title": "Alpha", "type": "Note"}, "v1\n")
    create_sha = backend.git_head()
    backend.edit_concept("/concepts/alpha.md", new_body="v2\n")
    v2_sha = backend.git_head()
    backend.edit_concept("/concepts/alpha.md", new_body="v3\n")
    head_sha = backend.git_head()
    monkeypatch.setattr(deps, "get_library_backend", lambda settings, user, conn: backend)

    response = client.get("/library/tree", params={"path": "/concepts/alpha.md"})
    assert response.status_code == 200
    text = response.text
    # embedded bootstrap timeline JSON is OLDEST-first (creation < v2 < v3);
    # tojson sorts the bootstrap keys, so slice from the timeline array on
    json_text = text[text.index('"timeline":') :]
    assert json_text.index(create_sha) < json_text.index(v2_sha) < json_text.index(head_sha)
    # slider sits on the rightmost (live) stop: value == max == len-1
    assert 'max="2"' in text
    assert 'value="2"' in text
    # one visible snap-point dot per timeline stop, oldest-first, live active
    assert text.count('data-index="') == 3
    assert 'class="history-tick active" data-index="2"' in text
    tick_text = text[text.index('id="history-ticks"') :]
    assert (
        tick_text.index(create_sha[:7])
        < tick_text.index(v2_sha[:7])
        < tick_text.index(head_sha[:7])
    )
    # the body is server-rendered; the marked/DOMPurify CDN pipeline is gone
    assert "<p>v3</p>" in text
    assert "marked" not in text
    assert "dompurify" not in text.lower()
    assert "cdn.jsdelivr" not in text

    # the preview diff is selected-commit vs HEAD, not the commit's own change
    response = client.get(
        "/library/document/diff", params={"path": "/concepts/alpha.md", "sha": create_sha}
    )
    assert response.status_code == 200
    diff_html = response.json()["diff_html"]
    assert '<span class="diff-del">-v1</span>' in diff_html
    assert '<span class="diff-add">+v3</span>' in diff_html

    # HEAD itself diffs to nothing
    response = client.get(
        "/library/document/diff", params={"path": "/concepts/alpha.md", "sha": head_sha}
    )
    assert response.status_code == 200
    assert response.json()["diff_html"] == ""

    # unknown commit and reserved path both 404
    response = client.get(
        "/library/document/diff",
        params={"path": "/concepts/alpha.md", "sha": "b" * 40},
    )
    assert response.status_code == 404
    response = client.get("/library/document/diff", params={"path": "/index.md", "sha": create_sha})
    assert response.status_code == 404

    # inline mode (slider preview): in-flow vs-HEAD diff, no hunk chrome
    response = client.get(
        "/library/document/diff",
        params={"path": "/concepts/alpha.md", "sha": create_sha, "mode": "inline"},
    )
    assert response.status_code == 200
    diff_html = response.json()["diff_html"]
    assert '<div class="diff-del-block"><p>v1</p>\n</div>' in diff_html
    assert '<div class="diff-add-block"><p>v3</p>\n</div>' in diff_html
    assert "@@" not in diff_html

    # HEAD diffs to nothing in inline mode too; the 404s carry over
    response = client.get(
        "/library/document/diff",
        params={"path": "/concepts/alpha.md", "sha": head_sha, "mode": "inline"},
    )
    assert response.status_code == 200
    assert response.json()["diff_html"] == ""
    response = client.get(
        "/library/document/diff",
        params={"path": "/concepts/alpha.md", "sha": "b" * 40, "mode": "inline"},
    )
    assert response.status_code == 404


@skipif_no_git
def test_document_diff_inline_full_context(env, monkeypatch, tmp_path):
    """Inline mode renders the WHOLE document with the accumulated changes vs
    HEAD marked in-flow: unchanged lines far beyond the default 3-line hunk
    window must appear as context, not be cut away."""
    client, _, data_root = env
    make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")

    lib = tmp_path / "library"
    backend = LibraryBackend(lib, actor="athenaeum-test/0.0.0")
    backend.init_bundle()
    old_body = "".join(f"line {i}\n" for i in range(1, 21))
    backend.create_concept("/concepts/big.md", {"title": "Big", "type": "Note"}, old_body)
    create_sha = backend.git_head()
    new_body = old_body.replace("line 10\n", "line 10 changed\n")
    backend.edit_concept("/concepts/big.md", new_body=new_body)
    monkeypatch.setattr(deps, "get_library_backend", lambda settings, user, conn: backend)

    response = client.get(
        "/library/document/diff",
        params={"path": "/concepts/big.md", "sha": create_sha, "mode": "inline"},
    )
    assert response.status_code == 200
    diff_html = response.json()["diff_html"]
    assert '<div class="diff-del-block"><p>line 10</p>\n</div>' in diff_html
    assert '<div class="diff-add-block"><p>line 10 changed</p>\n</div>' in diff_html
    # unchanged lines far from the edit survive as in-flow context
    assert "line 1" in diff_html
    assert "line 20" in diff_html


@skipif_no_git
def test_document_diff_endpoint_rename_aware(env, monkeypatch, tmp_path):
    client, _, data_root = env
    make_user(data_root, "alice", "pw")
    login(client, "alice", "pw")

    lib = tmp_path / "library"
    backend = LibraryBackend(lib, actor="athenaeum-test/0.0.0")
    backend.init_bundle()
    backend.create_concept("/concepts/alpha.md", {"title": "Alpha", "type": "Note"}, "v1\n")
    create_sha = backend.git_head()
    backend.move_concept("/concepts/alpha.md", "/concepts/beta.md")
    backend.edit_concept("/concepts/beta.md", new_body="v2\n")
    monkeypatch.setattr(deps, "get_library_backend", lambda settings, user, conn: backend)

    response = client.get(
        "/library/document/diff", params={"path": "/concepts/beta.md", "sha": create_sha}
    )
    assert response.status_code == 200
    diff_html = response.json()["diff_html"]
    # the vs-HEAD patch covers both the old and the current path (rename)
    assert "concepts/alpha.md" in diff_html
    assert "concepts/beta.md" in diff_html
    assert "diff-del" in diff_html
    assert "diff-add" in diff_html
