"""Seed a demo library (user "demo") with a rich universe for graph testing.

Idempotent: wipes and rewrites the demo user's library on every run.

Usage (inside the container):
    docker cp scripts/seed_demo_library.py athenaeum-athenaeum-1:/tmp/
    docker exec athenaeum-athenaeum-1 python /tmp/seed_demo_library.py

Env: ATHENAEUM_DATA_ROOT (default /data), DEMO_PASSWORD (default demo1234).
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import yaml

HUMAN = {"by": "human:demo", "at": "2026-07-20T12:00:00+00:00"}
MACHINE = {"by": "athenaeum-librarian/0.9.0", "at": "2026-07-21T12:00:00+00:00"}
STALE_DATE = "2020-01-01"


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

    def add(path, ftype, title, description, body, **extra):
        fm = {"type": ftype, "title": title, "description": description, **health(), **extra}
        tree[path] = (fm, body)

    galaxies = {
        "atlas": {"systems": {"releases": 4, "research": 4}, "root": 3},
        "helix": {"systems": {"daily": 2, "ops": 4}, "root": 2},
        "orion": {"systems": {"lab": 4, "field": 3}, "root": 3},
        "vega": {"systems": {"core": 4, "edge": 3}, "root": 2},
        "polaris": {"systems": {"nav": 3}, "root": 2},
    }
    cross = {
        "atlas": "/helix/helix-hub.md",
        "helix": "/orion/orion-hub.md",
        "orion": "/atlas/releases/releases-1.md",
        "vega": "/polaris/nav/nav-1.md",
        "polaris": "/atlas/overview.md",
    }
    types = ["project", "note", "guide", "version"]

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
            body = (
                f"# {title}\n\nHub of the {galaxy} galaxy. Cross-link: "
                f"[{cross[galaxy]}]({cross[galaxy]}).\n"
            )
            add(path, "project", title, f"{galaxy} root concept {i}", body)

        for system, count in spec["systems"].items():
            for i in range(1, count + 1):
                title = f"{system.title()} {i}"
                body = (
                    f"# {title}\n\nPart of the {system} system in {galaxy}. Back to [hub]({hub}).\n"
                )
                ftype = "version" if system == "releases" else types[i % len(types)]
                desc = f"{galaxy}/{system} entry {i}"
                add(f"/{galaxy}/{system}/{system}-{i}.md", ftype, title, desc, body)

    moons = [
        ("day-001", "Daily log 001"),
        ("day-002", "Daily log 002"),
        ("day-003", "Daily log 003"),
        ("day-004", "Daily log 004"),
        ("day-005", "Daily log 005"),
        ("day-006", "Daily log 006"),
    ]
    for slug, title in moons:
        body = f"# {title}\n\nLog entry, digest in [Daily 1](/helix/daily/daily-1.md).\n"
        add(f"/helix/daily/log/{slug}.md", "note", title, f"helix daily log {slug}", body)

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
    LibraryBackend(root, actor="demo-seed", versioning=False).reconcile()
    print(f"seeded {count} concepts into {root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
