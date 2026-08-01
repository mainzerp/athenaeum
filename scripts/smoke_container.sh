#!/usr/bin/env bash
# Container smoke test (plan section 8.4) for the athenaeum image.
#
# Requires Docker (with the compose plugin) and curl. Runs from the repo root
# and uses a throwaway compose project name ("athenaeum-smoke") with its own
# named volume, so real dev state (the default project's containers/volume)
# is never touched. Teardown (down -v) runs unconditionally on exit.

set -euo pipefail

cd "$(dirname "$0")/.."

export ATHENAEUM_SECRET_KEY="${ATHENAEUM_SECRET_KEY:-smoke-secret-key}"

COMPOSE="docker compose -p athenaeum-smoke"
BASE="http://localhost:8000"

fail() {
    echo "FAIL: $1" >&2
    exit 1
}

cleanup() {
    echo "--- teardown: compose down -v"
    $COMPOSE down -v >/dev/null 2>&1 || true
    [ -n "${JAR:-}" ] && rm -f "$JAR" || true
    [ -n "${SETUP_HTML:-}" ] && rm -f "$SETUP_HTML" || true
}
trap cleanup EXIT

echo "--- build image"
$COMPOSE build || fail "docker compose build"
echo "PASS: image built"

echo "--- up (waits for the container HEALTHCHECK -> /healthz 200)"
$COMPOSE up -d --wait || fail "container did not become healthy"
echo "PASS: container healthy"

body="$(curl -fsS "$BASE/healthz")" || fail "GET /healthz failed"
[ "$body" = '{"status":"ok"}' ] || fail "unexpected /healthz body: $body"
echo "PASS: /healthz returns {\"status\":\"ok\"}"

code="$(curl -s -o /dev/null -w '%{http_code}' "$BASE/")"
[ "$code" = "303" ] || fail "GET / expected 303 (first-run redirect), got $code"
echo "PASS: first run redirects / with 303"

JAR="$(mktemp)"
SETUP_HTML="$(mktemp)"

code="$(curl -s -o "$SETUP_HTML" -w '%{http_code}' -c "$JAR" -b "$JAR" "$BASE/setup")"
[ "$code" = "200" ] || fail "GET /setup expected 200, got $code"
echo "PASS: /setup page renders"

# CSRF: the session token is rendered into the form as a hidden input (0.15.0).
csrf="$(sed -n 's/.*name="csrf_token" value="\([^"]*\)".*/\1/p' "$SETUP_HTML" | head -1)"
[ -n "$csrf" ] || fail "no csrf_token hidden input in /setup form"
echo "PASS: CSRF token present in /setup form"

code="$(curl -s -o /dev/null -w '%{http_code}' -c "$JAR" -b "$JAR" -X POST \
    --data-urlencode "username=smoke-owner" \
    --data-urlencode "password=smoke-pw-12345" \
    --data-urlencode "confirm=smoke-pw-12345" \
    --data-urlencode "csrf_token=$csrf" \
    "$BASE/setup")"
[ "$code" = "303" ] || fail "POST /setup expected 303, got $code"
echo "PASS: owner account created via /setup"

code="$(curl -s -o /dev/null -w '%{http_code}' -c "$JAR" -b "$JAR" -X POST \
    --data-urlencode "username=smoke-owner2" \
    --data-urlencode "password=smoke-pw-12345" \
    --data-urlencode "confirm=smoke-pw-12345" \
    "$BASE/setup")"
[ "$code" = "403" ] || fail "POST /setup without CSRF token expected 403, got $code"
echo "PASS: POST /setup without CSRF token rejected (403)"

echo "--- restart (persistence check)"
$COMPOSE restart || fail "docker compose restart"
for _ in $(seq 1 30); do
    if curl -fsS "$BASE/healthz" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
curl -fsS "$BASE/healthz" >/dev/null || fail "container did not recover after restart"
location="$(curl -s -o /dev/null -w '%{redirect_url}' "$BASE/setup")"
case "$location" in
    /login | */login) ;;
    *) fail "GET /setup after restart expected redirect to /login, got '$location'" ;;
esac
echo "PASS: users/libraries survived the restart (volume persistence)"

echo "SMOKE OK"
