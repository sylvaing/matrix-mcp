#!/usr/bin/env bash
# End-to-end integration harness: boot a throwaway Synapse in Docker, register
# test users, run tests/integration against it, tear everything down.
#
# Usage:  bash scripts/integration-test.sh
# Env:    SYNAPSE_IMAGE (default matrixdotorg/synapse:latest)
#         IT_PORT       (default 18008 — host port for the homeserver)
#         PYTEST        (default "python -m pytest" from the current env)
set -euo pipefail

SYNAPSE_IMAGE="${SYNAPSE_IMAGE:-matrixdotorg/synapse:latest}"
IT_PORT="${IT_PORT:-18008}"
PYTEST="${PYTEST:-python -m pytest}"
CONTAINER="matrix-mcp-synapse-it"
BASE_URL="http://127.0.0.1:${IT_PORT}"

DATA_DIR="$(mktemp -d "${TMPDIR:-/tmp}/matrix-mcp-it.XXXXXX")"

cleanup() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  rm -rf "$DATA_DIR"
}
trap cleanup EXIT

echo "==> generating throwaway Synapse config in $DATA_DIR"
docker run --rm \
  -v "$DATA_DIR:/data" \
  -e SYNAPSE_SERVER_NAME=localhost \
  -e SYNAPSE_REPORT_STATS=no \
  "$SYNAPSE_IMAGE" generate >/dev/null

# Test-only settings appended to the generated config. PyYAML's last-key-wins
# duplicate handling makes appends act as overrides.
cat >> "$DATA_DIR/homeserver.yaml" <<'YAML'

## --- matrix-mcp integration-test overrides (throwaway server) ---
enable_registration: true
enable_registration_without_verification: true
suppress_key_server_warning: true
rc_message:
  per_second: 1000
  burst_count: 1000
rc_login:
  address: {per_second: 1000, burst_count: 1000}
  account: {per_second: 1000, burst_count: 1000}
  failed_attempts: {per_second: 1000, burst_count: 1000}
rc_registration:
  per_second: 1000
  burst_count: 1000
YAML

echo "==> starting Synapse ($SYNAPSE_IMAGE) on $BASE_URL"
docker run -d --name "$CONTAINER" \
  -v "$DATA_DIR:/data" \
  -p "127.0.0.1:${IT_PORT}:8008" \
  "$SYNAPSE_IMAGE" >/dev/null

echo -n "==> waiting for homeserver"
for i in $(seq 1 60); do
  if curl -fsS "$BASE_URL/_matrix/client/versions" >/dev/null 2>&1; then
    echo " up (${i}s)"
    break
  fi
  if [ "$i" -eq 60 ]; then
    echo " TIMEOUT"; docker logs "$CONTAINER" | tail -30; exit 1
  fi
  echo -n "."
  sleep 1
done

register_and_login() {  # $1 = localpart -> prints access token
  local user="$1" pass="it-password-$1"
  docker exec "$CONTAINER" register_new_matrix_user \
    -c /data/homeserver.yaml -u "$user" -p "$pass" --no-admin \
    http://localhost:8008 >/dev/null
  curl -fsS -X POST "$BASE_URL/_matrix/client/v3/login" \
    -H 'Content-Type: application/json' \
    -d "{\"type\":\"m.login.password\",\"identifier\":{\"type\":\"m.id.user\",\"user\":\"$user\"},\"password\":\"$pass\",\"initial_device_display_name\":\"matrix-mcp-it\"}" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])'
}

echo "==> registering test users"
ALICE_TOKEN="$(register_and_login it_alice)"
register_and_login it_bob >/dev/null

echo "==> running tests/integration"
MATRIX_MCP_IT_BASE_URL="$BASE_URL" \
MATRIX_MCP_IT_ALICE_MXID="@it_alice:localhost" \
MATRIX_MCP_IT_ALICE_TOKEN="$ALICE_TOKEN" \
MATRIX_MCP_IT_BOB_MXID="@it_bob:localhost" \
  $PYTEST -q tests/integration "$@"

echo "==> integration suite passed"
