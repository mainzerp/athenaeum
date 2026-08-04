#!/usr/bin/env bash
# Container smoke test (plan section 8.4) for the athenaeum image.
#
# Requires Docker (with the compose plugin) and curl. Runs from the repo root
# and uses a throwaway compose project name ("athenaeum-smoke") with its own
# named volume, so real dev state (the default project's containers/volume)
# is never touched. Teardown (down -v) runs unconditionally on exit.
#
# Manual fallback when Docker is unavailable: review the Dockerfile and render
# `docker compose config`; or run the image by hand with the flags from
# docker-compose.yml (read_only, tmpfs /tmp, cap_drop ALL, no-new-privileges)
# and repeat the assertions below (ReadonlyRootfs, git --version, import).
#
# Host port: defaults to 8000; set SMOKE_PORT to run alongside a dev instance
# (e.g. SMOKE_PORT=18000 bash scripts/smoke_container.sh).

set -euo pipefail

cd "$(dirname "$0")/.."

export ATHENAEUM_SECRET_KEY="${ATHENAEUM_SECRET_KEY:-smoke-secret-key}"
SMOKE_PORT="${SMOKE_PORT:-8000}"

# The only host-side deviation from docker-compose.yml: the published port
# (!override replaces the base ports list; a plain list would be appended).
OVERRIDE="$(mktemp)"
cat > "$OVERRIDE" <<EOF
services:
  athenaeum:
    ports: !override
      - "${SMOKE_PORT}:8000"
EOF

COMPOSE="docker compose -p athenaeum-smoke -f docker-compose.yml -f $OVERRIDE"
BASE="http://localhost:${SMOKE_PORT}"

fail() {
    echo "FAIL: $1" >&2
    exit 1
}

cleanup() {
    echo "--- teardown: compose down -v"
    $COMPOSE down -v >/dev/null 2>&1 || true
    rm -f "$OVERRIDE"
    [ -n "${JAR:-}" ] && rm -f "$JAR" || true
    [ -n "${SETUP_HTML:-}" ] && rm -f "$SETUP_HTML" || true
    [ -n "${LIB_HTML:-}" ] && rm -f "$LIB_HTML" || true
    [ -n "${IMPORT_ZIP:-}" ] && rm -f "$IMPORT_ZIP" || true
}
trap cleanup EXIT

echo "--- build image"
$COMPOSE build || fail "docker compose build"
echo "PASS: image built"

echo "--- up (waits for the container HEALTHCHECK -> /healthz 200)"
$COMPOSE up -d --wait || fail "container did not become healthy"
echo "PASS: container healthy"

echo "--- hardening assertions (compose file)"
cid="$($COMPOSE ps -q athenaeum)"
[ -n "$cid" ] || fail "could not resolve the athenaeum container id"
readonly_rootfs="$(docker inspect --format '{{.HostConfig.ReadonlyRootfs}}' "$cid")"
[ "$readonly_rootfs" = "true" ] || fail "ReadonlyRootfs is '$readonly_rootfs', expected 'true'"
echo "PASS: container runs with a read-only rootfs"

git_version="$($COMPOSE exec -T athenaeum git --version)" || fail "git --version failed in the container"
echo "PASS: $git_version"

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

echo "--- import roundtrip (few-MiB archive; proves the tmpfs multipart spool under read_only)"
LIB_HTML="$(mktemp)"
IMPORT_ZIP="$(mktemp)"

code="$(curl -s -o "$LIB_HTML" -w '%{http_code}' -c "$JAR" -b "$JAR" "$BASE/config/library")"
[ "$code" = "200" ] || fail "GET /config/library expected 200, got $code"
csrf_import="$(sed -n 's/.*name="csrf_token" value="\([^"]*\)".*/\1/p' "$LIB_HTML" | head -1)"
[ -n "$csrf_import" ] || fail "no csrf_token hidden input in /config/library form"

# Generate the archive inside the container (no host python/zip needed):
# a minimal valid bundle (index.md + log.md) plus a 2 MiB filler concept.
# Written to /data (not /tmp): `compose cp` cannot read tmpfs content through
# the daemon on Docker Desktop; the multipart POST itself still exercises /tmp.
$COMPOSE exec -T athenaeum python - <<'PYEOF'
import zipfile
with zipfile.ZipFile("/data/smoke-import.zip", "w") as z:
    z.writestr("index.md", '---\nokf_version: "0.2"\n---\n# Index\n')
    z.writestr("log.md", "# Log\n")
    z.writestr("filler.md", "---\ntype: Concept\ntitle: Filler\n---\n" + "x" * (2 * 1024 * 1024))
PYEOF
$COMPOSE cp athenaeum:/data/smoke-import.zip "$IMPORT_ZIP" || fail "compose cp of the generated archive failed"

# curl's -F token is not path-converted by MSYS/Git Bash (it is not a bare
# path argument), so hand curl a native path where cygpath exists.
curl_zip="$IMPORT_ZIP"
if command -v cygpath >/dev/null 2>&1; then
    curl_zip="$(cygpath -w "$IMPORT_ZIP")"
fi
code="$(curl -s -o /dev/null -w '%{http_code}' -c "$JAR" -b "$JAR" \
    -F "csrf_token=$csrf_import" -F "file=@$curl_zip;type=application/zip" \
    "$BASE/library/import")" || fail "curl POST /library/import failed (transport error)"
[ "$code" = "303" ] || fail "POST /library/import expected 303, got $code"
echo "PASS: few-MiB library archive imported (multipart spool OK under read_only)"

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
