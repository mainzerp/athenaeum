"""OKF frontmatter derivations shared by the librarian and the WebUI.

Single source of truth for the trust tier (OKF §5.3) and staleness
(OKF §5.5) derivations from concept frontmatter — previously duplicated
between ``webui/deps.py`` and ``librarian/agent.py`` with divergent
staleness boundary semantics (CS-7).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time

TRUST_UNVERIFIED = "unverified"
TRUST_MACHINE = "machine-confirmed"
TRUST_HUMAN = "human-reviewed"


def trust_tier(frontmatter: dict) -> str:
    """OKF §5.3 trust tier from the ``verified`` frontmatter list.

    No verified entries (or none with a parseable verifier mapping, L4) ->
    unverified; non-human verifiers only -> machine-confirmed; any 'human:'
    verifier -> human-reviewed. A bare verified mapping is accepted as a
    one-element list.
    """
    verified = frontmatter.get("verified") or []
    if isinstance(verified, dict):  # tolerate bare mapping (normalized on read)
        verified = [verified]
    bys = [str(v.get("by", "")) for v in verified if isinstance(v, dict)]
    if not bys:
        return TRUST_UNVERIFIED
    if any(b.startswith("human:") for b in bys):
        return TRUST_HUMAN
    return TRUST_MACHINE


def is_stale(frontmatter: dict, *, now: datetime | None = None) -> bool:
    """OKF §5.5 staleness: stale ON or after ``stale_after`` (``now >= stale_after``).

    Per the OKF 2026-08 timestamp change, ``stale_after`` is an ISO 8601
    datetime with an explicit UTC offset. A naive datetime (no offset)
    names a different instant in every timezone, so it is ignored rather
    than guessed at — matching the OKF reference implementation. A legacy
    date-only ``YYYY-MM-DD`` value (written by athenaeum before that spec
    change) is read as midnight UTC so existing bundles keep working.
    """
    raw = str(frontmatter.get("stale_after") or "")
    if not raw:
        return False
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    if "T" not in raw:
        try:
            stale_after = datetime.combine(date.fromisoformat(raw[:10]), time.min, UTC)
        except ValueError:
            return False
    else:
        try:
            stale_after = datetime.fromisoformat(raw)
        except ValueError:
            return False
        if stale_after.tzinfo is None:
            return False
    return now >= stale_after
