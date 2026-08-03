"""OKF v0.2 validator: errors are spec MUSTs, warnings are conventions.

Error classes (6, spec section 11 / analysis section 1.5):
1. ``frontmatter-parse`` — concept without a parseable YAML frontmatter block.
2. ``missing-type`` — frontmatter without a non-empty ``type``.
3. ``reserved-structure`` — index.md frontmatter anywhere except root
   ``okf_version``; reserved-name discipline.
4. ``log-date-format`` — log.md ``## `` heading not ``YYYY-MM-DD``.
5. ``required-within`` — ``generated`` without ``by``, ``sources[]`` entry
   without ``resource``, Attested Computation without ``runtime``.
6. ``bad-status`` — ``status`` outside {draft, stable, deprecated}.

Warning classes (9): ``broken-link``, ``missing-recommended``,
``non-conventional-actor``, ``verified-bare-mapping``, ``malformed-date``,
``index-drift``, ``footnote-mismatch``, plus ``orphan`` (no inbound and no
outbound bundle links; deprecated concepts are never reported — they are
pending removal, not graph citizens to wire in). Broken links are warnings,
never errors (spec 6.1).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Any

from . import frontmatter as fm_mod
from . import index as index_mod
from . import links as links_mod

_HEADING_RE = re.compile(r"^## (\S+)\s*$")
_ACTOR_RE = re.compile(r"^[^/\s]+/\S+$")
_FOOTNOTE_RE = re.compile(r"\[\^([^\]]+)\]")
STATUSES = frozenset({"draft", "stable", "deprecated"})


def validate_bundle(root: str | Path, scope: str | None = None) -> dict:
    """Validate the bundle under ``root``; return ``{"errors", "warnings"}``.

    ``scope`` (bundle-relative directory or file) restricts which paths are
    reported on; graph-based checks (links, orphans) are computed bundle-wide
    but filtered to the scope.
    """
    root = Path(root)
    errors: list[dict] = []
    warnings: list[dict] = []

    def in_scope(bundle_path: str) -> bool:
        if scope is None:
            return True
        s = scope if scope.startswith("/") else "/" + scope
        return bundle_path == s or bundle_path.startswith(s.rstrip("/") + "/")

    md_files = sorted(
        p
        for p in root.rglob("*.md")
        if not any(part.startswith(".") for part in p.relative_to(root).parts)
    )
    statuses: dict[str, Any] = {}  # rel -> frontmatter status (orphan check)
    for path in md_files:
        rel = "/" + path.relative_to(root).as_posix()
        if not in_scope(rel):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(_entry(rel, "frontmatter-parse", f"unreadable: {exc}"))
            continue
        if path.name in links_mod.RESERVED_NAMES:
            _check_reserved(rel, path.name, text, path.parent == root, errors)
            continue
        try:
            raw_fm, body = fm_mod.split_document(text, normalize_verified=False)
        except fm_mod.FrontmatterError as exc:
            errors.append(_entry(rel, "frontmatter-parse", str(exc)))
            continue
        if not raw_fm:
            errors.append(_entry(rel, "frontmatter-parse", "concept has no frontmatter block"))
            continue
        fm, _ = fm_mod.split_document(text)
        statuses[rel] = fm.get("status")
        _check_concept(rel, fm, raw_fm, body, errors, warnings)

    # link integrity (warning, never error)
    for source, target, resolved in links_mod.iter_bundle_links(root):
        if not in_scope(source):
            continue
        if not (root / resolved.lstrip("/")).exists():
            warnings.append(
                {
                    "path": source,
                    "code": "broken-link",
                    "target": target,
                    "message": f"broken link to {target!r}",
                }
            )

    # index drift (warning, auto-repairable by the generator)
    for dir_path in _iter_dirs(root):
        rel_dir = "/" if dir_path == root else "/" + dir_path.relative_to(root).as_posix()
        idx_rel = "/index.md" if rel_dir == "/" else rel_dir + "/index.md"
        if not in_scope(idx_rel):
            continue
        idx_file = dir_path / "index.md"
        if not idx_file.is_file():
            continue
        if idx_file.read_text(encoding="utf-8") != index_mod.generate_index(root, rel_dir):
            warnings.append(
                _entry(idx_rel, "index-drift", "index.md differs from regenerated content")
            )

    # orphan concepts (warning): no inbound and no outbound bundle links
    graph = links_mod.link_graph(root)
    inbound = {p: 0 for p in graph}
    for targets in graph.values():
        for target in targets:
            if target in inbound:
                inbound[target] += 1
    for concept, outbound in graph.items():
        if statuses.get(concept) == "deprecated":
            continue  # pending removal: never reported, never wired in (L16)
        if in_scope(concept) and not outbound and inbound.get(concept, 0) == 0:
            warnings.append(_entry(concept, "orphan", "no inbound or outbound bundle links"))

    return {"errors": errors, "warnings": warnings}


def _entry(path: str, code: str, message: str) -> dict:
    return {"path": path, "code": code, "message": message}


def _iter_dirs(root: Path) -> Iterator[Path]:
    yield root
    for path in sorted(root.rglob("*")):
        if path.is_dir() and not any(part.startswith(".") for part in path.relative_to(root).parts):
            yield path


def _check_reserved(rel: str, name: str, text: str, is_root_dir: bool, errors: list[dict]) -> None:
    if name == "index.md":
        fm_text, _ = fm_mod._split_raw(text)
        if fm_text is None:
            return
        try:
            data, _ = fm_mod.split_document(text)
        except fm_mod.FrontmatterError as exc:
            errors.append(
                _entry(rel, "reserved-structure", f"index.md frontmatter not parseable: {exc}")
            )
            return
        if not is_root_dir:
            errors.append(
                _entry(rel, "reserved-structure", "index.md frontmatter only allowed at root")
            )
        elif set(data) - {"okf_version"}:
            errors.append(
                _entry(
                    rel,
                    "reserved-structure",
                    "root index.md frontmatter may only contain okf_version",
                )
            )
    else:  # log.md
        for line in text.split("\n"):
            if not line.startswith("## "):
                continue
            match = _HEADING_RE.match(line)
            valid = False
            if match:
                try:
                    date.fromisoformat(match.group(1))
                    valid = True
                except ValueError:
                    valid = False
            if not valid:
                errors.append(
                    _entry(rel, "log-date-format", f"log.md heading not YYYY-MM-DD: {line!r}")
                )


def _check_concept(
    rel: str,
    fm: dict,
    raw_fm: dict,
    body: str,
    errors: list[dict],
    warnings: list[dict],
) -> None:
    type_ = fm.get("type")
    if not isinstance(type_, str) or not type_.strip():
        errors.append(_entry(rel, "missing-type", "frontmatter requires a non-empty 'type'"))

    status = fm.get("status")
    if status is not None and status not in STATUSES:
        errors.append(_entry(rel, "bad-status", f"status must be one of {sorted(STATUSES)}"))

    generated = fm.get("generated")
    if generated is not None:
        if not isinstance(generated, dict) or not generated.get("by"):
            errors.append(_entry(rel, "required-within", "'generated' requires 'by'"))
        else:
            _check_actor(rel, generated["by"], warnings)
            if generated.get("at") is not None and not _is_datetime(generated["at"]):
                warnings.append(
                    _entry(rel, "malformed-date", f"generated.at malformed: {generated['at']!r}")
                )

    sources = fm.get("sources")
    if isinstance(sources, list):
        for i, entry in enumerate(sources):
            if not isinstance(entry, dict) or not entry.get("resource"):
                errors.append(_entry(rel, "required-within", f"sources[{i}] requires 'resource'"))

    if type_ == "Attested Computation" and not fm.get("runtime"):
        errors.append(_entry(rel, "required-within", "Attested Computation requires 'runtime'"))

    # --- warnings ---
    for key in ("title", "description"):
        if not fm.get(key):
            warnings.append(_entry(rel, "missing-recommended", f"missing '{key}'"))

    if isinstance(raw_fm.get("verified"), dict):
        warnings.append(
            _entry(rel, "verified-bare-mapping", "'verified' is a bare mapping, not a list")
        )
    verified = fm.get("verified")
    if isinstance(verified, list):
        for entry in verified:
            if isinstance(entry, dict):
                _check_actor(rel, entry.get("by"), warnings)
                if entry.get("at") is not None and not _is_datetime(entry["at"]):
                    warnings.append(
                        _entry(rel, "malformed-date", f"verified[].at malformed: {entry['at']!r}")
                    )

    if fm.get("stale_after") is not None and not _is_date(fm["stale_after"]):
        warnings.append(
            _entry(rel, "malformed-date", f"stale_after is not YYYY-MM-DD: {fm['stale_after']!r}")
        )
    usage_window = fm.get("usage_window")
    if isinstance(usage_window, dict):
        for key in ("from", "to"):
            value = usage_window.get(key)
            if value is not None and not _is_date(value):
                warnings.append(
                    _entry(rel, "malformed-date", f"usage_window.{key} malformed: {value!r}")
                )

    ids = {
        entry["id"]
        for entry in (sources if isinstance(sources, list) else [])
        if isinstance(entry, dict) and "id" in entry
    }
    for label in set(_FOOTNOTE_RE.findall(body)):
        if label not in ids:
            warnings.append(
                _entry(rel, "footnote-mismatch", f"footnote {label!r} matches no sources[].id")
            )


def _check_actor(rel: str, actor: Any, warnings: list[dict]) -> None:
    if not isinstance(actor, str) or not (
        actor.startswith("human:") or actor.startswith("process:") or _ACTOR_RE.match(actor)
    ):
        warnings.append(_entry(rel, "non-conventional-actor", f"actor not conventional: {actor!r}"))


def _is_date(value: Any) -> bool:
    if isinstance(value, (date, datetime)):
        return True
    if isinstance(value, str):
        try:
            date.fromisoformat(value)
            return True
        except ValueError:
            return False
    return False


def _is_datetime(value: Any) -> bool:
    if isinstance(value, (date, datetime)):
        return True
    if isinstance(value, str):
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            return True
        except ValueError:
            return False
    return False
