#!/usr/bin/env bash
set -euo pipefail

PROJECT_NAME="querygate_release_smoke"
IMAGE_NAME="querygate:release-smoke"
APP_CONTAINER="querygate-release-smoke-app"
HOST_PORT="${QUERYGATE_SMOKE_PORT:-18080}"
BASE_URL="http://127.0.0.1:${HOST_PORT}"
export QUERYGATE_POSTGRES_PORT="${QUERYGATE_SMOKE_POSTGRES_PORT:-15433}"
export QUERYGATE_REDIS_PORT="${QUERYGATE_SMOKE_REDIS_PORT:-16379}"

cleanup() {
    status=$?
    if [ "$status" -ne 0 ]; then
        docker logs "$APP_CONTAINER" 2>/dev/null || true
    fi
    docker rm -f "$APP_CONTAINER" >/dev/null 2>&1 || true
    docker compose -p "$PROJECT_NAME" down -v >/dev/null 2>&1 || true
    return "$status"
}
trap cleanup EXIT

cleanup
docker compose -p "$PROJECT_NAME" up -d --wait
docker build -t "$IMAGE_NAME" .
docker run -d --name "$APP_CONTAINER" \
    --network "${PROJECT_NAME}_default" \
    -p "127.0.0.1:${HOST_PORT}:8000" \
    -e ENVIRONMENT=localhost \
    -e QUERYGATE_DEMO_DB_URL=postgresql+asyncpg://querygate:querygate@querygate-demo-db:5432/querygate_demo \
    -e CONCURRENCY_BACKEND=redis \
    -e CONCURRENCY_REDIS_URL=redis://querygate-redis:6379/0 \
    -e AUDIT_SINK_BACKEND=none \
    -e POLICY_FILE=/smoke_write_policy.yaml \
    -v "$(pwd)/scripts/smoke_write_policy.yaml:/smoke_write_policy.yaml:ro" \
    "$IMAGE_NAME" >/dev/null

for _ in $(seq 1 30); do
    if curl --fail --silent "$BASE_URL/health" >/dev/null; then
        break
    fi
    sleep 1
done

curl --fail --silent "$BASE_URL/health" >/dev/null
curl --fail --silent "$BASE_URL/api/v1/connections" >/dev/null

response=$(curl --fail --silent \
    -H 'Content-Type: application/json' \
    -d '{"from":"customers","select":["customers.id","customers.name"],"limit":2}' \
    "$BASE_URL/api/v1/demo/query")

RESPONSE="$response" python3 - <<'PY'
import json
import os

payload = json.loads(os.environ["RESPONSE"])
assert payload["row_count"] == 2, payload
assert len(payload["rows"]) == 2, payload
assert all(set(row) == {"id", "name"} for row in payload["rows"]), payload
print("release smoke passed: container queried real Postgres through the structured API")
PY

# ── Governed WRITE round-trip (item 93): preview -> execute a capped insert ->
# verify -> execute a governed delete -> verify — proving the shipped image
# mutates safely, not just reads. Uses a high id it inserts then deletes, so the
# seed is untouched.
SMOKE_ID=990001
INSERT_BODY="{\"op\":\"insert\",\"table\":\"orders\",\"rows\":[{\"id\":${SMOKE_ID},\"customer_id\":1,\"status\":\"smoke\",\"total_amount\":1,\"created_at\":\"2026-01-01T00:00:00\"}]}"
DELETE_BODY="{\"op\":\"delete\",\"table\":\"orders\",\"where\":{\"col\":\"orders.id\",\"op\":\"eq\",\"value\":${SMOKE_ID}}}"

# Dry-run preview mutates nothing.
curl --fail --silent -H 'Content-Type: application/json' -d "$INSERT_BODY" \
    "$BASE_URL/api/v1/demo/write/preview" >/dev/null

# Execute the insert.
exec_response=$(curl --fail --silent -H 'Content-Type: application/json' -d "$INSERT_BODY" \
    "$BASE_URL/api/v1/demo/write/execute")
EXEC_RESPONSE="$exec_response" python3 -c '
import json, os
p = json.loads(os.environ["EXEC_RESPONSE"])
assert p["affected_rows"] == 1, p
assert p["executed"] is True, p
'

# Verify the row landed.
verify=$(curl --fail --silent -H 'Content-Type: application/json' \
    -d "{\"from\":\"orders\",\"select\":[\"orders.id\"],\"where\":{\"col\":\"orders.id\",\"op\":\"eq\",\"value\":${SMOKE_ID}}}" \
    "$BASE_URL/api/v1/demo/query")
VERIFY="$verify" python3 -c 'import json,os; assert json.loads(os.environ["VERIFY"])["row_count"] == 1'

# Clean up with a governed delete and verify it is gone (leaves the seed intact).
curl --fail --silent -H 'Content-Type: application/json' -d "$DELETE_BODY" \
    "$BASE_URL/api/v1/demo/write/execute" >/dev/null
verify2=$(curl --fail --silent -H 'Content-Type: application/json' \
    -d "{\"from\":\"orders\",\"select\":[\"orders.id\"],\"where\":{\"col\":\"orders.id\",\"op\":\"eq\",\"value\":${SMOKE_ID}}}" \
    "$BASE_URL/api/v1/demo/query")
VERIFY2="$verify2" python3 -c 'import json,os; assert json.loads(os.environ["VERIFY2"])["row_count"] == 0'

echo "release smoke passed: container executed a governed write (insert + delete) on real Postgres"
