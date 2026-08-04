"""Raw-payload archive for ``store_knowledge`` requests (0.20.0).

One JSON record per store request under
``<library_root>/.athenaeum/payloads/<request_id>.json``, written
two-phase by ``Librarian.handle_store``: a ``received`` record on entry
(so busy rejections are recorded too) and a final-outcome rewrite on
exit (``ok`` / ``partial`` / ``error`` / ``busy``). The archive answers
"what exactly was the librarian asked to store?" when a store fails or
lands partially; the curator surfaces those records as one-shot
``store_payload_reviews`` findings.

Dot-prefixed, so invisible to every OKF traversal, and excluded from git
history (``.gitignore``) — same posture as ``.traces`` ("what was requested"
is history). Image params are archived as content-addressed refs only; the
bytes live once in the asset store. Retention: pruned on create behind
per-user ``payload_keep`` (0 = keep all).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from athenaeum.library.frontmatter import write_text_atomic

PAYLOAD_DIR = ".athenaeum/payloads"

_PAYLOAD_ID_RE = re.compile(r"^[0-9A-Za-z-]+$")

_SUMMARY_KEYS = (
    "request_id",
    "tool",
    "agent_label",
    "received_at",
    "finished_at",
    "outcome",
    "error",
)


class PayloadStore:
    """JSON payload records for one library root (``.athenaeum/payloads/<id>.json``)."""

    def __init__(self, root: str | Path, keep: int = 0) -> None:
        self.root = Path(root)
        self.store = self.root / PAYLOAD_DIR
        self.keep = keep

    def create(self, payload: dict) -> str:
        """Persist ``payload`` (overwrite-by-id) and return its request_id.

        Prunes when keep > 0.
        """
        request_id = str(payload.get("request_id", ""))
        if not _PAYLOAD_ID_RE.fullmatch(request_id):
            raise ValueError(f"invalid request id {request_id!r}")
        write_text_atomic(
            self.store / f"{request_id}.json",
            json.dumps(payload, indent=2, default=str) + "\n",
        )
        if self.keep > 0:
            self.prune(self.keep)
        return request_id

    def list(self, limit: int = 100) -> list[dict]:
        """Payload summaries (no params/stored), newest first."""
        out: list[dict] = []
        if self.store.is_dir():
            for path in sorted(self.store.glob("*.json"), reverse=True):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                out.append({key: data.get(key) for key in _SUMMARY_KEYS})
                if len(out) >= limit:
                    break
        return out

    def read(self, request_id: str) -> dict:
        """Full payload record. Raises ValueError on bad ids (traversal guard)."""
        if not _PAYLOAD_ID_RE.fullmatch(request_id):
            raise ValueError(f"invalid request id {request_id!r}")
        path = self.store / f"{request_id}.json"
        if not path.is_file():
            raise FileNotFoundError(f"no such payload: {request_id!r}")
        return json.loads(path.read_text(encoding="utf-8"))

    def since(self, ts: str | None) -> list[dict]:
        """Full records with ``received_at >= ts``, newest first; None = all retained.

        ``received_at`` and ``ts`` are same-format ISO 8601 UTC strings, so
        the comparison is lexicographic (the ``curate_last_run_at`` baseline
        and the archive share ``datetime.now(UTC).isoformat()``).
        """
        out: list[dict] = []
        if self.store.is_dir():
            for path in sorted(self.store.glob("*.json"), reverse=True):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if ts is not None and str(data.get("received_at") or "") < ts:
                    continue
                out.append(data)
        return out

    def prune(self, keep_last: int) -> int:
        """Delete all but the newest ``keep_last`` payloads; returns deletions."""
        if not self.store.is_dir():
            return 0
        files = sorted(f for f in self.store.glob("*.json") if _PAYLOAD_ID_RE.fullmatch(f.stem))
        excess = files[: max(0, len(files) - keep_last)]
        for path in excess:
            path.unlink()
        return len(excess)
