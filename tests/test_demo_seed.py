"""Tests for the demo library seed generator (scripts/seed_demo_library.py)."""

import importlib.util
import re
from datetime import date
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "seed_demo_library", Path(__file__).parent.parent / "scripts" / "seed_demo_library.py"
)
seed = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(seed)

_LINK_RE = re.compile(r"\]\((/[^)]+\.md)\)")


def _degrees(tree):
    """Out-degree + in-degree per path from absolute .md links in bodies."""
    paths = set(tree)
    out = {}
    in_deg = dict.fromkeys(paths, 0)
    for path, (_, body) in tree.items():
        targets = {t for t in _LINK_RE.findall(body) if t in paths}
        out[path] = len(targets)
        for t in targets:
            in_deg[t] += 1
    return {p: out[p] + in_deg[p] for p in paths}


def _cluster(path):
    return path.split("/")[1] if path.count("/") >= 2 else "root"


def test_demo_tree_has_five_galaxies_with_systems():
    paths = set(seed.demo_tree())
    galaxies = {p.split("/")[1] for p in paths if p.count("/") >= 2}
    assert galaxies == {"atlas", "helix", "orion", "vega", "polaris"}
    assert any(p.startswith("/atlas/releases/") for p in paths)
    assert any(p.startswith("/helix/daily/") for p in paths)
    assert any(p.startswith("/vega/core/") for p in paths)


def test_demo_tree_fills_the_universe():
    tree = seed.demo_tree()
    assert len(tree) >= 75
    moons = [p for p in tree if p.count("/") >= 4]  # /galaxy/system/sub/file.md
    assert len(moons) >= 5, "expected depth-3 concepts (moons)"
    assert "/helix/daily/log/day-001.md" in moons
    assert "/drifting-memo.md" in tree  # folderless root planet
    sizes = {_cluster(p) for p in tree}
    assert sizes == {"atlas", "helix", "orion", "vega", "polaris", "root"}


def test_demo_tree_links_are_internal_and_health_mix():
    tree = seed.demo_tree()
    paths = set(tree)
    for path, (_, body) in tree.items():
        for target in re.findall(r"\]\((/[^)]+)\)", body):
            assert target in paths, f"{path} links to missing {target}"

    fms = [fm for fm, _ in tree.values()]
    verified = [fm["verified"]["by"] for fm in fms if "verified" in fm]
    assert any(str(v).startswith("human:") for v in verified)  # human-reviewed
    assert any(not str(v).startswith("human:") for v in verified)  # machine-confirmed
    assert any("verified" not in fm for fm in fms)  # unverified
    assert any("stale_after" in fm for fm in fms)  # stale


def test_demo_tree_generated_at_spread():
    tree = seed.demo_tree()
    dates = []
    missing = 0
    for fm, _ in tree.values():
        generated = fm.get("generated")
        if generated:
            dates.append(date.fromisoformat(str(generated["at"])[:10]))
        else:
            missing += 1
    assert len(dates) >= 60, "most concepts need generated.at for the recency metric"
    assert missing >= 3, "some concepts without generated.at exercise the oldest fallback"
    assert (max(dates) - min(dates)).days >= 250, "recency needs a real radial gradient"
    assert max(dates) <= seed.ANCHOR, "deterministic anchor, never wall-clock"


def test_demo_tree_link_density_hubs_and_isolates():
    tree = seed.demo_tree()
    degrees = _degrees(tree)
    assert min(degrees.values()) == 0, "expected real isolates (degree 0)"
    assert degrees["/drifting-memo.md"] == 0
    assert max(degrees.values()) >= 20, "expected real hubs"
    assert degrees["/atlas/overview.md"] >= 20
    assert degrees["/vega/core/core-1.md"] >= 8
    paths = set(tree)
    cross_cluster = [
        (p, t)
        for p, (_, body) in tree.items()
        for t in _LINK_RE.findall(body)
        if t in paths and _cluster(t) != _cluster(p)
    ]
    assert len(cross_cluster) >= 10, "expected links across cluster boundaries"
