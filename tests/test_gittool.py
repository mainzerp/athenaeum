"""Tests for athenaeum.library.gittool (real git binary required)."""

import shutil
import subprocess

import pytest

from athenaeum.library import gittool
from athenaeum.library.gittool import GITIGNORE_CONTENT, GitError, GitRepo

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git binary required")


def _git(root, *args) -> str:
    proc = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def make_repo(tmp_path) -> GitRepo:
    repo = GitRepo(tmp_path / "lib")
    assert repo.ensure() is True
    return repo


def test_ensure_initializes_repo_with_identity_and_gitignore(tmp_path):
    repo = make_repo(tmp_path)
    root = tmp_path / "lib"
    assert (root / ".git").is_dir()
    assert (root / ".gitignore").read_text(encoding="utf-8") == GITIGNORE_CONTENT
    assert _git(root, "config", "--local", "user.name") == "Athenaeum Librarian"
    assert _git(root, "config", "--local", "user.email") == "athenaeum@localhost"
    assert _git(root, "branch", "--show-current") == "main"
    assert repo.ensure() is True  # idempotent: pre-existing repo respected


def test_ensure_respects_preexisting_identity(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Custom Author")
    repo = GitRepo(root)
    assert repo.ensure() is True
    assert _git(root, "config", "--local", "user.name") == "Custom Author"


def test_commit_all_commits_and_skips_empty_tree(tmp_path):
    repo = make_repo(tmp_path)
    root = tmp_path / "lib"
    (root / "a.md").write_text("one\n", encoding="utf-8")
    short = repo.commit_all("Creation: Created [A](/a.md).")
    assert short is not None
    assert _git(root, "log", "-1", "--format=%s") == "Creation: Created [A](/a.md)."
    # no staged/unstaged changes left; an immediate second call is a no-op
    assert repo.commit_all("nothing happened") is None
    assert _git(root, "rev-list", "--count", "HEAD") == "1"


def test_commit_all_sanitizes_multiline_messages(tmp_path):
    repo = make_repo(tmp_path)
    (tmp_path / "lib" / "a.md").write_text("x\n", encoding="utf-8")
    repo.commit_all("Update: line one\nline two\r\nline three")
    assert (
        _git(tmp_path / "lib", "log", "-1", "--format=%s") == "Update: line one line two line three"
    )


def test_list_commits_empty_on_unborn_head_and_missing_repo(tmp_path):
    repo = make_repo(tmp_path)  # initialized, but no commits yet
    assert repo.list_commits() == []
    assert repo.head_sha() is None
    assert repo.count_commits() == 0
    # missing repo entirely
    absent = GitRepo(tmp_path / "not-a-repo")
    (tmp_path / "not-a-repo").mkdir()
    assert absent.list_commits() == []


def test_list_commits_newest_first_with_root_flag(tmp_path):
    repo = make_repo(tmp_path)
    root = tmp_path / "lib"
    (root / "a.md").write_text("one\n", encoding="utf-8")
    first = repo.commit_all("first commit")
    (root / "a.md").write_text("two\n", encoding="utf-8")
    repo.commit_all("second commit")
    commits = repo.list_commits()
    assert [c["subject"] for c in commits] == ["second commit", "first commit"]
    assert commits[0]["is_root"] is False
    assert commits[1]["is_root"] is True
    assert commits[1]["short"] == first
    assert commits[1]["sha"].startswith(first)
    assert commits[0]["timestamp"]  # ISO 8601 committer date
    assert repo.count_commits() == 2


def test_commit_diff_contains_changed_line(tmp_path):
    repo = make_repo(tmp_path)
    root = tmp_path / "lib"
    (root / "a.md").write_text("before\n", encoding="utf-8")
    repo.commit_all("c1")
    (root / "a.md").write_text("after\n", encoding="utf-8")
    repo.commit_all("c2")
    diff = repo.commit_diff(repo.head_sha())
    assert "-before" in diff
    assert "+after" in diff
    with pytest.raises(GitError, match="invalid commit sha"):
        repo.commit_diff("not-a-sha!!")
    with pytest.raises(GitError, match="invalid commit sha"):
        repo.commit_diff("../../etc")


def test_revert_staged_refuses_root_commit(tmp_path):
    repo = make_repo(tmp_path)
    (tmp_path / "lib" / "a.md").write_text("one\n", encoding="utf-8")
    repo.commit_all("initial")
    root_sha = repo.head_sha()
    with pytest.raises(GitError, match="cannot revert the initial commit"):
        repo.revert_staged(root_sha)


def test_revert_staged_restores_content_then_commit(tmp_path):
    repo = make_repo(tmp_path)
    root = tmp_path / "lib"
    (root / "a.md").write_text("v1\n", encoding="utf-8")
    repo.commit_all("c1")
    (root / "b.md").write_text("added\n", encoding="utf-8")
    middle = repo.commit_all("c2")
    (root / "a.md").write_text("v2\n", encoding="utf-8")
    repo.commit_all("c3")
    subject = repo.revert_staged(middle)
    assert subject == "c2"
    # the middle commit's change is undone on top of HEAD: b.md is gone again,
    # the later c3 change to a.md is untouched
    assert not (root / "b.md").exists()
    assert (root / "a.md").read_text(encoding="utf-8") == "v2\n"
    short = repo.commit_staged("Update: Reverted commit c2.")
    assert short is not None
    assert _git(root, "log", "-1", "--format=%s") == "Update: Reverted commit c2."


def test_reset_staged_then_commit_keeps_undo_path(tmp_path):
    repo = make_repo(tmp_path)
    root = tmp_path / "lib"
    (root / "a.md").write_text("v1\n", encoding="utf-8")
    first = repo.commit_all("c1")
    (root / "a.md").write_text("v2\n", encoding="utf-8")
    pre_reset = repo.commit_all("c2")
    # reset to the first commit: worktree becomes v1, HEAD does not move yet
    short = repo.reset_staged(first)
    assert short == first
    assert (root / "a.md").read_text(encoding="utf-8") == "v1\n"
    assert repo.head_sha() == _git(root, "rev-parse", f"{pre_reset}^{{commit}}")
    repo.commit_staged(f"Update: Reset library to {short}.")
    # the reset commit's parent is the pre-reset commit: undo stays reachable
    assert _git(root, "rev-parse", "--short", "HEAD~1") == pre_reset
    assert _git(root, "show", "--format=", "--patch", pre_reset)  # parent intact
    assert repo.count_commits() == 3


def test_reset_staged_refuses_current_head(tmp_path):
    repo = make_repo(tmp_path)
    (tmp_path / "lib" / "a.md").write_text("v1\n", encoding="utf-8")
    repo.commit_all("c1")
    with pytest.raises(GitError, match="already at this commit"):
        repo.reset_staged(repo.head_sha())


def test_reset_staged_unknown_commit(tmp_path):
    repo = make_repo(tmp_path)
    (tmp_path / "lib" / "a.md").write_text("v1\n", encoding="utf-8")
    repo.commit_all("c1")
    with pytest.raises(GitError, match="unknown commit"):
        repo.reset_staged("deadbeef")


def test_commit_staged_no_changes_raises(tmp_path):
    repo = make_repo(tmp_path)
    (tmp_path / "lib" / "a.md").write_text("v1\n", encoding="utf-8")
    repo.commit_all("c1")
    with pytest.raises(GitError, match="no changes"):
        repo.commit_staged("Update: nothing.")


def test_commit_all_never_raises_on_git_failure(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)

    def boom(root, args, *, timeout=30):
        raise GitError("simulated failure")

    monkeypatch.setattr(gittool, "_run", boom)
    assert repo.commit_all("Creation: anything.") is None  # no exception escapes


def test_ensure_false_when_binary_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    gittool.git_available.cache_clear()
    try:
        assert gittool.git_available() is False
        repo = GitRepo(tmp_path / "lib")
        assert repo.ensure() is False
        assert not (tmp_path / "lib" / ".git").exists()
        assert repo.commit_all("x") is None  # degrades silently
    finally:
        gittool.git_available.cache_clear()


def test_file_commits_filters_to_path(tmp_path):
    repo = make_repo(tmp_path)
    root = tmp_path / "lib"
    (root / "a.md").write_text("a1\n", encoding="utf-8")
    repo.commit_all("add a")
    (root / "b.md").write_text("b1\n", encoding="utf-8")
    repo.commit_all("add b")
    (root / "log.md").write_text("churn\n", encoding="utf-8")
    repo.commit_all("log-only churn")
    (root / "a.md").write_text("a2\n", encoding="utf-8")
    repo.commit_all("edit a")
    commits = repo.file_commits("a.md")
    assert [c["subject"] for c in commits] == ["edit a", "add a"]  # newest first
    assert all(c["path"] == "a.md" for c in commits)
    assert commits[-1]["is_root"] is True


def test_file_commits_follows_rename_with_path_at_commit(tmp_path):
    repo = make_repo(tmp_path)
    root = tmp_path / "lib"
    (root / "a.md").write_text("content\n", encoding="utf-8")
    repo.commit_all("add a")
    _git(root, "mv", "a.md", "b.md")
    repo.commit_all("rename a to b")
    commits = repo.file_commits("b.md")
    assert [c["subject"] for c in commits] == ["rename a to b", "add a"]
    # the rename commit itself keeps the NEW name; older ones the old name
    assert commits[0]["path"] == "b.md"
    assert commits[1]["path"] == "a.md"
    assert repo.file_at_commit(commits[1]["sha"], "a.md") == "content\n"
    assert repo.file_at_commit(commits[1]["sha"], "b.md") is None


def test_file_commits_empty_states(tmp_path):
    repo = make_repo(tmp_path)  # initialized, but no commits yet
    assert repo.file_commits("a.md") == []
    # missing repo entirely
    absent = GitRepo(tmp_path / "not-a-repo")
    (tmp_path / "not-a-repo").mkdir()
    assert absent.file_commits("a.md") == []


def test_file_at_commit_exact_bytes_and_absent(tmp_path):
    repo = make_repo(tmp_path)
    root = tmp_path / "lib"
    text = "unicode: äöü ✓\ntrailing newline kept\n"
    (root / "a.md").write_text(text, encoding="utf-8")
    repo.commit_all("add a")
    sha = repo.head_sha()
    assert repo.file_at_commit(sha, "a.md") == text
    assert repo.file_at_commit(sha, "missing.md") is None
    with pytest.raises(GitError, match="invalid commit sha"):
        repo.file_at_commit("not-a-sha!!", "a.md")
    with pytest.raises(GitError, match="unknown commit"):
        repo.file_at_commit("deadbeef", "a.md")


def test_file_diff_middle_and_root(tmp_path):
    repo = make_repo(tmp_path)
    root = tmp_path / "lib"
    (root / "a.md").write_text("old\n", encoding="utf-8")
    (root / "b.md").write_text("b1\n", encoding="utf-8")
    repo.commit_all("create both")
    (root / "a.md").write_text("new\n", encoding="utf-8")
    (root / "b.md").write_text("b2\n", encoding="utf-8")
    repo.commit_all("edit both")
    # a middle commit's patch is limited to the path
    diff = repo.file_diff(repo.head_sha(), "a.md")
    assert "-old" in diff
    assert "+new" in diff
    assert "b.md" not in diff  # the other file's hunks are absent
    # the file's creation commit shows the whole file as additions
    root_diff = repo.file_diff(repo.list_commits()[-1]["sha"], "a.md")
    assert "+old" in root_diff
    # a commit that did not touch the path produces no patch
    (root / "b.md").write_text("b3\n", encoding="utf-8")
    repo.commit_all("touch b only")
    assert repo.file_diff(repo.head_sha(), "a.md") == ""
