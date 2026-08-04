"""Seed a demo library (user "demo") with a rich universe for graph testing.

Idempotent: wipes and rewrites the demo user's library on every run.

Deterministic (fixed dates, no wall-clock randomness) and tuned for the graph
rework universe metrics: ``generated.at`` frontmatter spreads over ~320 days
(recency gradient; a few docs carry none and fall back to "oldest"), and
cross-links within and across clusters produce real link_density hubs and
real isolates.

Usage (inside the container):
    docker cp scripts/seed_demo_library.py athenaeum-athenaeum-1:/tmp/
    docker exec athenaeum-athenaeum-1 python /tmp/seed_demo_library.py

Env: ATHENAEUM_DATA_ROOT (default /data), DEMO_PASSWORD (default demo1234).
"""

from __future__ import annotations

import os
import shutil
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import yaml

HUMAN = {"by": "human:demo", "at": "2026-07-20T12:00:00+00:00"}
MACHINE = {"by": "athenaeum-librarian/0.9.0", "at": "2026-07-21T12:00:00+00:00"}
STALE_DATE = "2020-01-01"

# Fixed anchor for the generated.at spread — deterministic, never wall-clock.
ANCHOR = date(2026, 7, 25)
AGES_DAYS = [2, 4, 7, 11, 17, 26, 38, 55, 80, 115, 165, 230, 320]

# Docs that attract many in-links from across the tree (link_density hubs).
SUPERHUBS = ("/atlas/overview.md", "/vega/core/core-1.md")


def _iso(d: date) -> str:
    return datetime(d.year, d.month, d.day, 9, 30, tzinfo=UTC).isoformat()


def _render_links(links: list[str]) -> str:
    if not links:
        return "\n"
    refs = ", ".join(f"[{t}]({t})" for t in links)
    return f" Cross-link: {refs}.\n"


def demo_tree() -> dict[str, tuple[dict, str]]:
    """Pure generator: bundle path -> (frontmatter, body)."""
    tree: dict[str, tuple[dict, str]] = {}
    health_cycle = [HUMAN, MACHINE, None, None, "STALE"]
    counter = {"n": 0}

    def health():
        pick = health_cycle[counter["n"] % len(health_cycle)]
        counter["n"] += 1
        if pick == "STALE":
            return {"stale_after": STALE_DATE}
        return {"verified": pick} if pick else {}

    def add(path, ftype, title, description, body, *, age_days=None, **extra):
        fm = {"type": ftype, "title": title, "description": description, **health(), **extra}
        if age_days is not None:
            fm["generated"] = {
                "by": MACHINE["by"],
                "at": _iso(ANCHOR - timedelta(days=age_days)),
            }
        tree[path] = (fm, body)

    galaxies = {
        "atlas": {"systems": {"releases": 6, "research": 10, "specs": 6}, "root": 3},
        "helix": {"systems": {"daily": 4, "ops": 8}, "root": 2},
        "orion": {"systems": {"lab": 8, "field": 5}, "root": 3},
        "vega": {"systems": {"core": 6, "edge": 4}, "root": 2},
        "polaris": {"systems": {"nav": 4}, "root": 2},
    }
    cross = {
        "atlas": "/helix/helix-hub.md",
        "helix": "/orion/orion-hub.md",
        "orion": "/atlas/releases/releases-1.md",
        "vega": "/polaris/nav/nav-1.md",
        "polaris": "/atlas/overview.md",
    }
    types = ["project", "note", "guide", "version"]

    # Pass 1: collect entries (path, ftype, title, description, base text,
    # cluster, fixed links) so pass 2 can wire deterministic cross-links.
    entries = []
    for galaxy, spec in galaxies.items():
        hub = f"/{galaxy}/overview.md" if galaxy == "atlas" else f"/{galaxy}/{galaxy}-hub.md"
        for i in range(spec["root"]):
            if i == 0:
                if galaxy == "atlas":
                    path, title = "/atlas/overview.md", "Atlas Overview"
                else:
                    path, title = f"/{galaxy}/{galaxy}-hub.md", f"{galaxy.title()} Hub"
            else:
                path = f"/{galaxy}/{galaxy}-note-{i}.md"
                title = f"{galaxy.title()} Note {i}"
            base = f"# {title}\n\nHub of the {galaxy} galaxy."
            entries.append(
                (
                    path,
                    "project",
                    title,
                    f"{galaxy} root concept {i}",
                    base,
                    galaxy,
                    [cross[galaxy]],
                )
            )
        for system, count in spec["systems"].items():
            for i in range(1, count + 1):
                title = f"{system.title()} {i}"
                ftype = "version" if system == "releases" else types[i % len(types)]
                base = f"# {title}\n\nPart of the {system} system in {galaxy}."
                entries.append(
                    (
                        f"/{galaxy}/{system}/{system}-{i}.md",
                        ftype,
                        title,
                        f"{galaxy}/{system} entry {i}",
                        base,
                        galaxy,
                        [hub],
                    )
                )

    moons = [
        ("day-001", "Daily log 001"),
        ("day-002", "Daily log 002"),
        ("day-003", "Daily log 003"),
        ("day-004", "Daily log 004"),
        ("day-005", "Daily log 005"),
        ("day-006", "Daily log 006"),
    ]
    for slug, title in moons:
        base = f"# {title}\n\nLog entry, digest in [Daily 1](/helix/daily/daily-1.md)."
        entries.append(
            (
                f"/helix/daily/log/{slug}.md",
                "note",
                title,
                f"helix daily log {slug}",
                base,
                "helix",
                [],
            )
        )

    # Pass 2: deterministic links + generated.at spread. Every 9th entry is a
    # link isolate (no out-links, never a link target); every 13th entry and
    # the drifting memo carry no generated.at (recency falls back to oldest).
    total = len(entries)

    def is_isolate(idx):
        return idx % 9 == 5

    for idx, (path, ftype, title, desc, base, cluster, fixed) in enumerate(entries):
        links = [] if is_isolate(idx) else list(fixed)
        if not is_isolate(idx):
            if idx % 4 == 0:
                superhub = SUPERHUBS[(idx // 4) % len(SUPERHUBS)]
                if superhub != path and superhub not in links:
                    links.append(superhub)
            if idx % 3 == 1:
                j = (idx * 13 + 7) % total
                while j == idx or entries[j][5] == cluster or is_isolate(j):
                    j = (j + 1) % total
                if entries[j][0] not in links:
                    links.append(entries[j][0])
        age = None if idx % 13 == 6 else AGES_DAYS[idx % len(AGES_DAYS)]
        add(path, ftype, title, desc, base + _render_links(links), age_days=age)

    add(
        "/drifting-memo.md",
        "note",
        "Drifting Memo",
        "folderless, unlinked root planet",
        "# Drifting Memo\n\nA planet without a galaxy and without links — drifting alone.\n",
    )
    return tree


def write_tree(root: Path, tree: dict[str, tuple[dict, str]]) -> int:
    count = 0
    for rel, (fm, body) in sorted(tree.items()):
        path = root / rel.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n\n" + body, "utf-8")
        count += 1
    return count


def main() -> int:
    from athenaeum import db, security
    from athenaeum.library.backend import LibraryBackend, provision_library

    data_root = Path(os.environ.get("ATHENAEUM_DATA_ROOT", "/data"))
    password = os.environ.get("DEMO_PASSWORD", "demo1234")
    db_path = data_root / "app.db"
    db.init_db(db_path)
    with db.connect(db_path) as conn:
        user = db.get_user_by_username(conn, "demo")
        if user is None:
            user = db.create_user(conn, "demo", security.hash_password(password), is_admin=False)
            provision_library(data_root, user["id"])
            print(f"created user demo ({user['id']})")
        else:
            print(f"reusing user demo ({user['id']})")
    root = data_root / "users" / user["id"] / "library"
    root.mkdir(parents=True, exist_ok=True)
    for child in root.iterdir():
        if child.name.startswith("."):
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    count = write_tree(root, demo_tree())
    LibraryBackend(root, actor="demo-seed", git_enabled=False).reconcile()
    print(f"seeded {count} concepts into {root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
