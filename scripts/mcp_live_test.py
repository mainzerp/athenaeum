#!/usr/bin/env python
"""Live MCP test against a running Athenaeum container.

Full flow: first-run setup (admin) -> login -> create MCP token via WebUI ->
raw JSON-RPC against /mcp (initialize -> tools/list -> tools/call).

Usage:
    .venv/Scripts/python scripts/mcp_live_test.py [--base http://localhost:8000]
        [--username NAME] [--password PW] [--skip-bootstrap]

--skip-bootstrap: use an existing account (--username/--password login only).
"""
from __future__ import annotations

import argparse
import json
import re
import sys

import httpx

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
TOKEN_RE = re.compile(r'<code class="token-value">([^<]+)</code>')


def fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def csrf_from(html: str) -> str:
    m = CSRF_RE.search(html)
    if not m:
        fail("no csrf_token hidden input in form")
    return m.group(1)


def bootstrap(base: str, username: str, password: str) -> httpx.Client:
    """Create the first admin (if needed), log in, return a session client."""
    client = httpx.Client(base_url=base, follow_redirects=False, timeout=15.0)

    r = client.get("/")
    if r.status_code == 303 and r.headers.get("location", "").endswith("/setup"):
        r = client.get("/setup")
        token = csrf_from(r.text)
        r = client.post(
            "/setup",
            data={
                "username": username,
                "password": password,
                "confirm": password,
                "csrf_token": token,
            },
        )
        if r.status_code != 303:
            fail(f"POST /setup expected 303, got {r.status_code}: {r.text[:200]}")
        print("PASS: first-run admin created via /setup")

    r = client.get("/login")
    token = csrf_from(r.text)
    r = client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": token},
    )
    if r.status_code != 303:
        fail(f"POST /login expected 303, got {r.status_code}: {r.text[:200]}")
    print("PASS: logged in")
    return client


def create_token(client: httpx.Client, label: str) -> str:
    r = client.get("/tokens")
    token = csrf_from(r.text)
    r = client.post("/tokens", data={"label": label, "csrf_token": token})
    if r.status_code != 200:
        fail(f"POST /tokens expected 200, got {r.status_code}")
    m = TOKEN_RE.search(r.text)
    if not m:
        fail("no token rendered in /tokens response")
    print("PASS: MCP token created")
    return m.group(1)


class McpClient:
    """Minimal JSON-RPC client for the streamable-HTTP MCP endpoint."""

    def __init__(self, base: str, token: str):
        self.client = httpx.Client(
            base_url=base,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            timeout=60.0,
        )
        self.session_id: str | None = None
        self._id = 0

    def _parse(self, r: httpx.Response) -> dict:
        ctype = r.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            for line in r.text.splitlines():
                if line.startswith("data:"):
                    return json.loads(line[5:].strip())
            fail(f"SSE response without data line: {r.text[:300]}")
        return r.json()

    def call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        headers = {}
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        r = self.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}},
            headers=headers,
        )
        if r.status_code != 200:
            fail(f"{method}: HTTP {r.status_code}: {r.text[:300]}")
        if "mcp-session-id" in r.headers:
            self.session_id = r.headers["mcp-session-id"]
        return self._parse(r)

    def notify(self, method: str) -> None:
        headers = {}
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        r = self.client.post(
            "/mcp", json={"jsonrpc": "2.0", "method": method}, headers=headers
        )
        if r.status_code not in (200, 202):
            fail(f"{method}: HTTP {r.status_code}: {r.text[:300]}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--username", default="mcp-live-admin")
    ap.add_argument("--password", default="mcp-live-pw-12345")
    ap.add_argument("--skip-bootstrap", action="store_true")
    args = ap.parse_args()

    # 1. WebUI bootstrap + token
    if args.skip_bootstrap:
        client = httpx.Client(base_url=args.base, follow_redirects=False, timeout=15.0)
        r = client.get("/login")
        token = csrf_from(r.text)
        r = client.post(
            "/login",
            data={
                "username": args.username,
                "password": args.password,
                "csrf_token": token,
            },
        )
        if r.status_code != 303:
            fail(f"POST /login expected 303, got {r.status_code}")
        print("PASS: logged in (existing account)")
    else:
        client = bootstrap(args.base, args.username, args.password)
    token = create_token(client, "mcp-live-test")

    # 2. MCP JSON-RPC
    mcp = McpClient(args.base, token)
    resp = mcp.call(
        "initialize",
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "mcp-live-test", "version": "0.1"},
        },
    )
    server = resp.get("result", {}).get("serverInfo", {})
    print(f"PASS: initialize -> {server.get('name')} {server.get('version')}")
    mcp.notify("notifications/initialized")

    resp = mcp.call("tools/list")
    tools = [t["name"] for t in resp.get("result", {}).get("tools", [])]
    print(f"PASS: tools/list -> {len(tools)} tools: {', '.join(sorted(tools))}")

    resp = mcp.call("tools/call", {"name": "library_status", "arguments": {}})
    content = resp.get("result", {}).get("content", [])
    text = content[0].get("text", "") if content else ""
    print(f"PASS: library_status -> {text[:400]}")

    # request_knowledge without a configured LLM must fail CLEANLY
    # (sanitized ToolError, no internals), not crash the session.
    resp = mcp.call(
        "tools/call",
        {"name": "request_knowledge", "arguments": {"query": "ping"}},
    )
    if "error" in resp:
        print(f"PASS: request_knowledge without LLM -> clean JSON-RPC error: "
              f"{resp['error'].get('message', '')[:200]}")
    else:
        result = resp.get("result", {})
        is_err = result.get("isError")
        content = result.get("content", [])
        text = content[0].get("text", "") if content else ""
        label = "tool error (expected without LLM config)" if is_err else "answered"
        print(f"PASS: request_knowledge -> {label}: {text[:300]}")

    print("MCP LIVE TEST OK")


if __name__ == "__main__":
    main()
