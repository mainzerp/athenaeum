#!/usr/bin/env python3
"""Fetch activity journal entries and LLM traces from a live Athenaeum instance.

Debugging helper for the running server (WebUI session-cookie auth with CSRF).
Credentials are read from ``.secrets/athenaeum-live.json`` (gitignored):

    {"base_url": "https://...", "username": "...", "password": "..."}

Usage (from the repo root):

    python scripts/athenaeum_debug.py version
    python scripts/athenaeum_debug.py activity [--limit N] [--errors]
    python scripts/athenaeum_debug.py trace TRACE_ID [--out FILE]

Stdlib only; no project dependencies required.
"""

from __future__ import annotations

import argparse
import json
import re
import ssl
import sys
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, HTTPSHandler, build_opener

REPO_ROOT = Path(__file__).resolve().parent.parent
SECRETS_PATH = REPO_ROOT / ".secrets" / "athenaeum-live.json"

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _ssl_context() -> ssl.SSLContext:
    """CA bundle from certifi when available.

    The Windows system store on some machines carries stale intermediates and
    fails valid Let's Encrypt chains ("certificate has expired"); certifi's
    bundle verifies them fine.
    """
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _make_opener(with_cookies: bool = False):
    handlers = [HTTPSHandler(context=_ssl_context())]
    if with_cookies:
        handlers.append(HTTPCookieProcessor(CookieJar()))
    return build_opener(*handlers)


def _die(msg: str, code: int = 1) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def _load_secrets() -> dict:
    if not SECRETS_PATH.exists():
        _die(f"secrets file not found: {SECRETS_PATH}")
    try:
        data = json.loads(SECRETS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _die(f"invalid JSON in {SECRETS_PATH}: {exc}")
    for key in ("base_url", "username", "password"):
        if not data.get(key):
            _die(f"missing '{key}' in {SECRETS_PATH}")
    data["base_url"] = data["base_url"].rstrip("/")
    return data


def _login(secrets: dict):
    """Log in via the WebUI form; return an opener carrying the session cookie."""
    opener = _make_opener(with_cookies=True)
    base = secrets["base_url"]
    try:
        html = opener.open(base + "/login", timeout=30).read().decode("utf-8", "replace")
    except (HTTPError, URLError) as exc:
        _die(f"cannot reach {base}/login: {exc}")
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    if not m:
        _die("no CSRF token found on /login (unexpected page shape)")
    body = urlencode(
        {
            "username": secrets["username"],
            "password": secrets["password"],
            "csrf_token": m.group(1),
        }
    ).encode("utf-8")
    resp = opener.open(base + "/login", body, timeout=30)
    # A failed login re-renders the login form; a good one redirects to "/".
    if resp.geturl().endswith("/login"):
        _die("login failed (bad credentials or lockout)")
    return opener


def _get(opener, base: str, path: str) -> tuple[str, bytes]:
    try:
        resp = opener.open(base + path, timeout=30)
    except HTTPError as exc:
        _die(f"GET {path} -> HTTP {exc.code}")
    except URLError as exc:
        _die(f"GET {path} failed: {exc}")
    ctype = resp.headers.get("Content-Type", "")
    return ctype, resp.read()


def _strip_tags(fragment: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub("", fragment)).strip()


def _parse_rows(html: str) -> tuple[list[dict], list[dict]]:
    """Parse /activity/rows: (in_flight, journal). Cell order matches the template."""
    in_flight: list[dict] = []
    journal: list[dict] = []
    for tr in re.findall(r"<tr>(.*?)</tr>", html, re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if not cells:
            continue
        started_m = re.search(r'data-utc="([^"]+)"', tr)
        started = started_m.group(1) if started_m else _strip_tags(cells[0])
        texts = [_strip_tags(c) for c in cells]
        trace_m = re.search(r"/library/traces/([\w.-]+)", tr)
        if len(texts) == 4:  # in-flight table: started, tool, agent, arguments
            in_flight.append(
                {"started_at": started, "tool": texts[1], "agent": texts[2], "arguments": texts[3]}
            )
        elif len(texts) >= 8:  # journal: started, tool, agent, outcome, duration, iters, tokens, error
            journal.append(
                {
                    "started_at": started,
                    "tool": texts[1],
                    "agent": texts[2],
                    "outcome": texts[3],
                    "duration": texts[4],
                    "iterations": texts[5],
                    "tokens": texts[6],
                    "error": texts[7],
                    "trace_id": trace_m.group(1) if trace_m else None,
                }
            )
    return in_flight, journal


def cmd_version(secrets: dict) -> None:
    opener = _make_opener()
    try:
        resp = opener.open(secrets["base_url"] + "/openapi.json", timeout=30)
        info = json.loads(resp.read()).get("info", {})
        print(f"{secrets['base_url']}  version={info.get('version', '?')}  title={info.get('title', '?')}")
    except (HTTPError, URLError, json.JSONDecodeError) as exc:
        _die(f"version check failed: {exc}")


def cmd_activity(secrets: dict, limit: int, errors_only: bool) -> None:
    opener = _login(secrets)
    ctype, raw = _get(opener, secrets["base_url"], "/activity/rows")
    if "html" not in ctype:
        _die(f"unexpected content type for /activity/rows: {ctype}")
    in_flight, journal = _parse_rows(raw.decode("utf-8", "replace"))
    if errors_only:
        journal = [r for r in journal if r["outcome"] != "ok"]
    journal = journal[:limit]

    if in_flight:
        print("IN FLIGHT")
        for e in in_flight:
            print(f"  {e['started_at']}  {e['tool']:<20} {e['agent']:<12} {e['arguments'][:80]}")
        print()
    if not journal:
        print("No journal rows." + (" (no failures)" if errors_only else ""))
        return
    print(f"{'STARTED (UTC)':<20} {'TOOL':<20} {'OUTCOME':<8} {'DURATION':<10} {'ITER':<5} {'TOKENS':<8} TRACE_ID")
    for r in journal:
        print(
            f"{r['started_at']:<20} {r['tool']:<20} {r['outcome']:<8} {r['duration']:<10} "
            f"{r['iterations']:<5} {r['tokens']:<8} {r['trace_id'] or '-'}"
        )
        if r["error"]:
            print(f"{'':<20} error: {r['error']}")


def cmd_trace(secrets: dict, trace_id: str, out: str | None) -> None:
    if not re.fullmatch(r"[\w.-]+", trace_id):
        _die(f"invalid trace id: {trace_id!r}")
    opener = _login(secrets)
    ctype, raw = _get(opener, secrets["base_url"], f"/api/traces/{trace_id}")
    try:
        trace = json.loads(raw)
    except json.JSONDecodeError:
        _die(f"trace {trace_id} did not return JSON (got {ctype}) — unknown id or not logged in")
    text = json.dumps(trace, indent=2, ensure_ascii=False)
    if out:
        Path(out).write_text(text + "\n", encoding="utf-8")
        events = trace.get("events")
        print(f"wrote {out} ({len(events)} events)" if isinstance(events, list) else f"wrote {out}")
    else:
        print(text)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("version", help="print the instance version (unauthenticated)")
    p_act = sub.add_parser("activity", help="list in-flight calls + journaled tool calls")
    p_act.add_argument("--limit", type=int, default=20)
    p_act.add_argument("--errors", action="store_true", help="only rows with outcome != ok")
    p_tr = sub.add_parser("trace", help="dump one trace as JSON")
    p_tr.add_argument("trace_id")
    p_tr.add_argument("--out", help="write JSON to this file instead of stdout")
    args = parser.parse_args()

    secrets = _load_secrets()
    if args.cmd == "version":
        cmd_version(secrets)
    elif args.cmd == "activity":
        cmd_activity(secrets, args.limit, args.errors)
    elif args.cmd == "trace":
        cmd_trace(secrets, args.trace_id, args.out)


if __name__ == "__main__":
    main()
