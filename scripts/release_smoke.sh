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

# The image self-configures on first boot (TODO.md item 215): it generates an
# admin API key, writes it to /app/var/admin-api-key at mode 0600, and REQUIRES
# it thereafter. Before that, every request here was unauthenticated and passed.
#
# This script was not updated with it, because item 215 landed on a branch whose
# CI never ran the smoke job — so the first authenticated build was PR #45,
# where /api/v1/connections returned 401 and curl exited 22. Reading the key the
# way docs/INSTALL.md tells an operator to keeps the smoke honest: it exercises
# the documented first-run path rather than a bypass.
ADMIN_KEY="$(docker exec "$APP_CONTAINER" cat /app/var/admin-api-key)"
if [ -z "$ADMIN_KEY" ]; then
    echo "release smoke: no admin key was generated at /app/var/admin-api-key" >&2
    exit 1
fi

curl --fail --silent -H "Authorization: Bearer $ADMIN_KEY" \
    "$BASE_URL/api/v1/connections" >/dev/null

response=$(curl --fail --silent \
    -H "Authorization: Bearer $ADMIN_KEY" \
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

# ── Window function round-trip (item 101) — the shipped image must actually run
# an OVER() clause against a real server, not merely accept the AST. A running
# total is the canonical case, and its correctness is self-evident from the rows:
# the last row's running total equals the sum of every row's value.
window_response=$(curl --fail --silent \
    -H "Authorization: Bearer $ADMIN_KEY" \
    -H 'Content-Type: application/json' \
    -d '{"from":"order_items","select":["order_items.id","order_items.quantity",{"fn":"sum","arg":{"col":"order_items.quantity"},"over":{"order_by":[{"col":"order_items.id"}],"frame":{"mode":"rows","start":{"bound":"unbounded_preceding"},"end":{"bound":"current_row"}}},"as":"running_quantity"}],"order_by":[{"col":"order_items.id"}],"limit":10}' \
    "$BASE_URL/api/v1/demo/query")

RESPONSE="$window_response" python3 - <<'PY'
import json
import os

payload = json.loads(os.environ["RESPONSE"])
rows = payload["rows"]
assert len(rows) > 1, payload
assert all(set(row) == {"id", "quantity", "running_quantity"} for row in rows), payload
running = [float(row["running_quantity"]) for row in rows]
assert running == sorted(running), running
assert running[-1] == sum(float(row["quantity"]) for row in rows), payload
print("release smoke passed: container ran a window function (running total) on real Postgres")
PY

# -- Range-join round-trip (item 103) -- the shipped image must actually run a
# non-equi ON clause against a real server, not merely accept the AST. A
# price-band self-join is the canonical case (regression-bar row 16), and its
# correctness is self-evident from the rows: every returned pair really does
# satisfy the band, and a product always bands against itself.
join_response=$(curl --fail --silent \
    -H "Authorization: Bearer $ADMIN_KEY" \
    -H 'Content-Type: application/json' \
    -d '{"from":"products","from_alias":"p","select":["p.id","p.price","band.id","band.price"],"joins":[{"table":"products","alias":"band","condition":{"and":[{"col":"p.price","op":"gte","value_col":"band.price"},{"col":"p.price","op":"lte","value_expr":{"left":{"col":"band.price"},"op":"*","right":{"literal":2}}}]}}],"order_by":[{"col":"p.id"},{"col":"band.id"}],"limit":50}' \
    "$BASE_URL/api/v1/demo/query")

RESPONSE="$join_response" python3 - <<'PYJOIN'
import json
import os

payload = json.loads(os.environ["RESPONSE"])
rows = payload["rows"]
assert len(rows) > 1, payload
assert all(set(row) == {"id", "price", "id_1", "price_1"} for row in rows), payload
for row in rows:
    price, band = float(row["price"]), float(row["price_1"])
    assert band <= price <= band * 2, row
# Every product bands against itself, so the self-pair must be present.
assert any(row["id"] == row["id_1"] for row in rows), payload
print("release smoke passed: container ran a non-equi range join on real Postgres")
PYJOIN

# ── Governed WRITE round-trip (item 93): preview -> execute a capped insert ->
# verify -> execute a governed delete -> verify — proving the shipped image
# mutates safely, not just reads. Uses a high id it inserts then deletes, so the
# seed is untouched.
SMOKE_ID=990001
INSERT_BODY="{\"op\":\"insert\",\"table\":\"orders\",\"rows\":[{\"id\":${SMOKE_ID},\"customer_id\":1,\"status\":\"smoke\",\"total_amount\":1,\"created_at\":\"2026-01-01T00:00:00\"}]}"
DELETE_BODY="{\"op\":\"delete\",\"table\":\"orders\",\"where\":{\"col\":\"orders.id\",\"op\":\"eq\",\"value\":${SMOKE_ID}}}"

# Dry-run preview mutates nothing.
curl --fail --silent -H 'Content-Type: application/json' -d "$INSERT_BODY" \
    -H "Authorization: Bearer $ADMIN_KEY" \
    "$BASE_URL/api/v1/demo/write/preview" >/dev/null

# Execute the insert.
exec_response=$(curl --fail --silent -H 'Content-Type: application/json' -d "$INSERT_BODY" \
    -H "Authorization: Bearer $ADMIN_KEY" \
    "$BASE_URL/api/v1/demo/write/execute")
EXEC_RESPONSE="$exec_response" python3 -c '
import json, os
p = json.loads(os.environ["EXEC_RESPONSE"])
assert p["affected_rows"] == 1, p
assert p["executed"] is True, p
'

# Verify the row landed.
verify=$(curl --fail --silent -H 'Content-Type: application/json' \
    -H "Authorization: Bearer $ADMIN_KEY" \
    -d "{\"from\":\"orders\",\"select\":[\"orders.id\"],\"where\":{\"col\":\"orders.id\",\"op\":\"eq\",\"value\":${SMOKE_ID}}}" \
    "$BASE_URL/api/v1/demo/query")
VERIFY="$verify" python3 -c 'import json,os; assert json.loads(os.environ["VERIFY"])["row_count"] == 1'

# Clean up with a governed delete and verify it is gone (leaves the seed intact).
curl --fail --silent -H 'Content-Type: application/json' -d "$DELETE_BODY" \
    -H "Authorization: Bearer $ADMIN_KEY" \
    "$BASE_URL/api/v1/demo/write/execute" >/dev/null
verify2=$(curl --fail --silent -H 'Content-Type: application/json' \
    -H "Authorization: Bearer $ADMIN_KEY" \
    -d "{\"from\":\"orders\",\"select\":[\"orders.id\"],\"where\":{\"col\":\"orders.id\",\"op\":\"eq\",\"value\":${SMOKE_ID}}}" \
    "$BASE_URL/api/v1/demo/query")
VERIFY2="$verify2" python3 -c 'import json,os; assert json.loads(os.environ["VERIFY2"])["row_count"] == 0'

echo "release smoke passed: container executed a governed write (insert + delete) on real Postgres"
