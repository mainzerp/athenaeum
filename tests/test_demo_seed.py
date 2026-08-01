"""Tests for the demo library seed generator (scripts/seed_demo_library.py)."""

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "seed_demo_library", Path(__file__).parent.parent / "scripts" / "seed_demo_library.py"
)
seed = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(seed)


def test_demo_tree_has_five_galaxies_with_systems():
    paths = set(seed.demo_tree())
    galaxies = {p.split("/")[1] for p in paths if p.count("/") >= 2}
    assert galaxies == {"atlas", "helix", "orion", "vega", "polaris"}
    assert any(p.startswith("/atlas/releases/") for p in paths)
    assert any(p.startswith("/helix/daily/") for p in paths)
    assert any(p.startswith("/vega/core/") for p in paths)


def test_demo_tree_fills_the_universe():
    tree = seed.demo_tree()
    assert len(tree) >= 45
    moons = [p for p in tree if p.count("/") >= 4]  # /galaxy/system/sub/file.md
    assert len(moons) >= 5, "expected depth-3 concepts (moons)"
    assert "/helix/daily/log/day-001.md" in moons
    assert "/drifting-memo.md" in tree  # folderless root planet


def test_demo_tree_links_are_internal_and_health_mix():
    tree = seed.demo_tree()
    paths = set(tree)
    import re

    for path, (_, body) in tree.items():
        for target in re.findall(r"\]\((/[^)]+)\)", body):
            assert target in paths, f"{path} links to missing {target}"

    fms = [fm for fm, _ in tree.values()]
    verified = [fm["verified"]["by"] for fm in fms if "verified" in fm]
    assert any(str(v).startswith("human:") for v in verified)  # human-reviewed
    assert any(not str(v).startswith("human:") for v in verified)  # machine-confirmed
    assert any("verified" not in fm for fm in fms)  # unverified
    assert any("stale_after" in fm for fm in fms)  # stale
