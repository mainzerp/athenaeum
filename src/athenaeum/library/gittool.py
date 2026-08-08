"""Git-backed library history: one commit per compound write (0.22.0).

Thin subprocess wrapper around the ``git`` binary — no third-party Python
dependency. Two call styles share the module:

- The auto-commit path (``commit_all``) NEVER raises: a missing binary, a
  failed commit, or a failed push logs one warning and returns None so a
  library write is never broken by its history bookkeeping.
- The explicit history ops (``list_commits``/``commit_diff``/
  ``revert_staged``/``reset_staged``/``commit_staged``/``pull_ff_only``)
  raise ``GitError`` so the WebUI can map failures to 4xx responses.
- The per-file reads (``file_commits``/``file_at_commit``/``file_diff``)
  power the document timeline: ``file_commits`` never raises (the UI
  degrades to "no timeline"), while ``file_at_commit``/``file_diff`` raise
  only for a bad or unknown sha, like ``commit_diff``.

Reset semantics are append-only (plan Decision 4): ``reset_staged`` uses
``git read-tree --reset -u <target>`` (index+worktree become exactly the
target, HEAD does not move) followed by one new commit, so the pre-reset
state stays reachable as the reset commit's parent — undo is another reset
through the same UI and the branch stays fast-forward pushable.
"""

from __future__ import annotations

import functools
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

GITIGNORE_CONTENT = """# Athenaeum internal stores — not library content
.athenaeum/versions/
.athenaeum/payloads/
.traces/
# Atomic-write temp siblings leaked by a hard crash
*.tmp
"""

GIT_USER_NAME = "Athenaeum Librarian"
GIT_USER_EMAIL = "athenaeum@localhost"

_SHA_RE = re.compile(r"^[0-9a-f]{4,40}$")
# git name-status rename records: R plus the zero-padded similarity score.
_RENAME_STATUS_RE = re.compile(r"^R\d{3}$")
_STDERR_TAIL = 500


class GitError(RuntimeError):
    """An explicit git history operation failed (routes map it to 4xx)."""


@functools.lru_cache(maxsize=1)
def git_available() -> bool:
    """True when a ``git`` binary is on PATH (probed once per process)."""
    return shutil.which("git") is not None


def _git_env() -> dict[str, str]:
    # Never block on a credential/ passphrase prompt: push/pull fail instead.
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0"}


def _run(root: Path, args: list[str], *, timeout: int = 30) -> str:
    """Run ``git <args>`` in ``root``; stdout on success, GitError otherwise."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=_git_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {args[0]} timed out after {timeout}s") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip()[-_STDERR_TAIL:]
        raise GitError(f"git {args[0]} failed: {tail}")
    return proc.stdout


def _status(root: Path, args: list[str], *, timeout: int = 30) -> int:
    """Exit code of ``git <args>`` (for quiet checks that signal via codes)."""
    proc = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=_git_env(),
    )
    return proc.returncode


def _sanitize(message: str) -> str:
    """One-line commit subject, capped at 200 chars (plan Decision 3)."""
    return " ".join(message.split())[:200]


def _require_sha_shape(sha: str) -> None:
    if not _SHA_RE.match(sha or ""):
        raise GitError(f"invalid commit sha: {sha!r}")


class GitRepo:
    """The git repository rooted at one library bundle root."""

    def __init__(
        self,
        root: str | Path,
        *,
        remote_url: str | None = None,
        auto_push: bool = False,
    ) -> None:
        self.root = Path(root)
        self.remote_url = remote_url
        self.auto_push = auto_push

    # --------------------------------------------------------------- setup

    def ensure(self) -> bool:
        """Initialize the repo when absent; sync the remote. Never raises.

        Fresh inits get branch ``main``, the library ``.gitignore``, and a
        repo-local identity (never ``--global``); pre-existing repositories
        and their local config are respected as-is. False when the binary is
        missing or setup fails (one warning; callers degrade gracefully).
        """
        if not git_available():
            logger.warning("git binary not found; library commit history disabled")
            return False
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            if not (self.root / ".git").exists():
                _run(self.root, ["init", "-b", "main"])
                ignore = self.root / ".gitignore"
                if not ignore.exists():
                    ignore.write_text(GITIGNORE_CONTENT, encoding="utf-8")
                _run(self.root, ["config", "user.name", GIT_USER_NAME])
                _run(self.root, ["config", "user.email", GIT_USER_EMAIL])
            if self.remote_url:
                try:
                    _run(self.root, ["remote", "set-url", "origin", self.remote_url])
                except GitError:
                    _run(self.root, ["remote", "add", "origin", self.remote_url])
            return True
        except Exception:
            logger.warning("git repo setup failed for %s", self.root, exc_info=True)
            return False

    # -------------------------------------------------------- auto-commit

    def commit_all(self, message: str) -> str | None:
        """Stage everything and commit; short sha of the new commit or None.

        Never raises: any failure (missing binary, repo setup, commit, push)
        logs a warning and returns None — history must never break a write.
        """
        if not git_available():
            return None  # the backend warned once at construction time
        try:
            if not self.ensure():
                return None
            _run(self.root, ["add", "-A"])
            if _status(self.root, ["diff", "--cached", "--quiet"]) == 0:
                return None  # nothing to commit
            _run(self.root, ["commit", "-m", _sanitize(message)])
            short = _run(self.root, ["rev-parse", "--short", "HEAD"]).strip()
            if self.auto_push and _status(self.root, ["remote", "get-url", "origin"]) == 0:
                try:
                    _run(self.root, ["push"], timeout=60)
                except Exception:
                    logger.warning("git push failed for %s", self.root, exc_info=True)
            return short
        except Exception:
            logger.warning("git auto-commit failed for %s", self.root, exc_info=True)
            return None

    # ----------------------------------------------------------- queries

    def list_commits(self, limit: int = 200) -> list[dict]:
        """Commit list, newest first: {sha, short, timestamp, subject, is_root}.

        ``[]`` for an unborn HEAD, a missing repo, or a missing git binary;
        other git failures raise ``GitError``.
        """
        if self.head_sha() is None:
            return []  # unborn HEAD (no commits yet) or not a repository
        out = _run(
            self.root,
            ["log", f"--max-count={limit}", "--pretty=format:%H%x1f%h%x1f%cI%x1f%s"],
        )
        commits = []
        for line in out.splitlines():
            if not line.strip():
                continue
            parts = line.split("\x1f")
            if len(parts) != 4:
                # Defensive: a polluted record (e.g. a multi-line subject
                # from an external commit) is skipped, never unpack-crashed.
                continue
            sha, short, timestamp, subject = parts
            commits.append(
                {
                    "sha": sha,
                    "short": short,
                    "timestamp": timestamp,
                    "subject": subject,
                    "is_root": False,
                }
            )
        if commits:
            roots = set(_run(self.root, ["rev-list", "--max-parents=0", "HEAD"]).split())
            for commit in commits:
                commit["is_root"] = commit["sha"] in roots
        return commits

    def commit_diff(self, sha: str) -> str:
        """Unified patch of one commit (``git show`` handles root commits)."""
        _require_sha_shape(sha)
        return _run(self.root, ["show", "--format=", "--patch", sha])

    def file_commits(self, path: str, limit: int = 100) -> list[dict]:
        """Commits touching ``path`` (repo-relative, no leading slash).

        Newest first; same dict shape as ``list_commits`` plus ``"path"`` —
        the path valid AT that commit, tracked through renames via
        ``--follow --name-status`` (a rename commit itself keeps the new
        name; older commits report the pre-rename name). Never raises:
        ``[]`` on an unborn HEAD / missing repo / git failure so the UI
        degrades to "no timeline".
        """
        if self.head_sha() is None:
            return []  # unborn HEAD (no commits yet) or not a repository
        try:
            out = _run(
                self.root,
                [
                    "log",
                    "--follow",
                    f"--max-count={limit}",
                    "--pretty=format:%H%x1f%h%x1f%cI%x1f%s",
                    "--name-status",
                    "--",
                    path,
                ],
            )
        except GitError:
            logger.warning("git per-file log failed for %s", path, exc_info=True)
            return []
        commits: list[dict] = []
        current = path
        entry: dict | None = None
        block_renames: list[tuple[str, str]] = []

        def close_block() -> None:
            nonlocal current, entry
            if entry is None:
                return
            commits.append(entry)
            entry = None
            # Older commits live under the pre-rename name; the rename
            # commit itself keeps the NEW name (its tree contains it).
            for old, new in block_renames:
                if new == current:
                    current = old
                    break

        for line in out.splitlines():
            if "\x1f" in line:
                parts = line.split("\x1f")
                if len(parts) != 4:
                    # Defensive: a polluted record (e.g. a multi-line
                    # subject from an external commit) is skipped, never
                    # unpack-crashed.
                    continue
                close_block()
                block_renames = []
                sha, short, timestamp, subject = parts
                entry = {
                    "sha": sha,
                    "short": short,
                    "timestamp": timestamp,
                    "subject": subject,
                    "is_root": False,
                    "path": current,
                }
            elif line.strip() and entry is not None:
                parts = line.split("\t")
                # Only real name-status rename records (R + 3-digit score,
                # exactly old/new tab fields) count — anything else (e.g. a
                # stray subject continuation line) is ignored.
                if _RENAME_STATUS_RE.match(parts[0]) and len(parts) == 3:
                    block_renames.append((parts[1], parts[2]))
        close_block()
        if commits:
            roots = set(_run(self.root, ["rev-list", "--max-parents=0", "HEAD"]).split())
            for commit in commits:
                commit["is_root"] = commit["sha"] in roots
        return commits

    def file_at_commit(self, sha: str, path: str) -> str | None:
        """Exact file text (utf-8) at ``sha``.

        ``None`` when the path is not in that commit's tree; ``GitError``
        on a malformed or unknown sha (same contract as ``commit_diff``).
        """
        full = self._require_commit(sha)
        if _status(self.root, ["cat-file", "-e", f"{full}:{path}"]) != 0:
            return None
        return _run(self.root, ["show", f"{full}:{path}"])

    def file_diff(self, sha: str, path: str) -> str:
        """Unified patch of ``sha`` limited to ``path``.

        ``git show`` handles root commits (the file's creation shows as a
        whole-file addition); ``""`` when the commit did not touch the
        path. ``GitError`` on a malformed or unknown sha.
        """
        _require_sha_shape(sha)
        return _run(self.root, ["show", "--format=", "--patch", sha, "--", path])

    def diff_to_head(self, sha: str, *paths: str, context: int | None = None) -> str:
        """Unified patch of ``sha``..HEAD limited to ``paths``.

        ``git diff <sha> HEAD -- <paths...>``; ``""`` when the tree state
        of those paths matches HEAD. ``GitError`` on a malformed or
        unknown sha (same contract as ``file_diff``). ``context`` overrides
        the unified-context line count (pass a value >= the file length for
        a whole-file diff, e.g. for inline in-flow rendering).
        """
        full = self._require_commit(sha)
        args = ["diff"]
        if context is not None:
            args.append(f"--unified={context}")
        return _run(self.root, [*args, full, "HEAD", "--", *paths])

    def head_sha(self) -> str | None:
        """Full sha of HEAD; None when unborn or not a repository."""
        try:
            return _run(self.root, ["rev-parse", "HEAD"]).strip()
        except Exception:
            return None

    def count_commits(self) -> int:
        """Number of commits reachable from HEAD (0 on any failure)."""
        try:
            return int(_run(self.root, ["rev-list", "--count", "HEAD"]).strip())
        except Exception:
            return 0

    # ------------------------------------------------------ explicit ops

    def revert_staged(self, sha: str) -> str:
        """Apply the reverse of ``sha`` to index+worktree (no commit yet).

        Returns the reverted commit's subject (for log/commit messages).
        The root commit cannot be reverted: that would delete index.md and
        log.md and break the bundle.
        """
        full = self._require_commit(sha)
        roots = set(_run(self.root, ["rev-list", "--max-parents=0", "HEAD"]).split())
        if full in roots:
            raise GitError("cannot revert the initial commit")
        subject = _run(self.root, ["log", "-1", "--format=%s", full]).strip()
        _run(self.root, ["revert", "--no-commit", full])
        return subject

    def reset_staged(self, sha: str) -> str:
        """Make index+worktree exactly ``sha`` (HEAD does NOT move).

        Returns the target's short sha. Append-only by design: the caller
        commits the staged tree, so the pre-reset state stays reachable as
        the reset commit's parent (undo = another reset through the UI).
        """
        full = self._require_commit(sha)
        if full == self.head_sha():
            raise GitError("already at this commit")
        _run(self.root, ["read-tree", "--reset", "-u", full])
        return _run(self.root, ["rev-parse", "--short", full]).strip()

    def commit_staged(self, message: str) -> str:
        """Stage all and commit an already-prepared tree; short sha.

        ``GitError("no changes")`` when the staged tree matches HEAD.
        """
        _run(self.root, ["add", "-A"])
        if _status(self.root, ["diff", "--cached", "--quiet"]) == 0:
            raise GitError("no changes")
        _run(self.root, ["commit", "-m", _sanitize(message)])
        return _run(self.root, ["rev-parse", "--short", "HEAD"]).strip()

    def abort_staged(self) -> None:
        """Reset index+worktree back to HEAD after a failed staged op."""
        try:
            _run(self.root, ["read-tree", "--reset", "-u", "HEAD"])
        except Exception:
            logger.warning("git abort failed for %s", self.root, exc_info=True)

    def pull_ff_only(self) -> None:
        """Fast-forward pull from the configured remote (divergence -> GitError)."""
        _run(self.root, ["pull", "--ff-only"], timeout=60)

    # ------------------------------------------------------------ internal

    def _require_commit(self, sha: str) -> str:
        """Validate shape + existence; returns the canonical full sha."""
        _require_sha_shape(sha)
        try:
            return _run(self.root, ["rev-parse", "--verify", f"{sha}^{{commit}}"]).strip()
        except GitError as exc:
            raise GitError(f"unknown commit: {sha}") from exc
