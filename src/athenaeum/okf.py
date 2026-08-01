"""OKF frontmatter derivations shared by the librarian and the WebUI.

Single source of truth for the trust tier (OKF §5.3) and staleness
(OKF §5.5) derivations from concept frontmatter — previously duplicated
between ``webui/deps.py`` and ``librarian/agent.py`` with divergent
staleness boundary semantics (CS-7).
"""

from __future__ import annotations

from datetime import date

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


def is_stale(frontmatter: dict, *, today: date | None = None) -> bool:
    """OKF §5.5 staleness: stale ON or after ``stale_after`` (``stale_after <= today``)."""
    stale_after = frontmatter.get("stale_after")
    if not stale_after:
        return False
    today = today or date.today()
    try:
        return date.fromisoformat(str(stale_after)) <= today
    except ValueError:
        return False
