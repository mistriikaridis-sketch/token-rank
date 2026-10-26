#!/usr/bin/env bash
# Verify Postgres path: health backend=postgres, ingest idempotent, rankings, RBAC.
# Prereq:
#   docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d --build
set -euo pipefail
BASE="${BASE_URL:-http://localhost:8000}"
WRITE_KEY="${TOKEN_RANKINGS_API_KEY:-dev-token-rankings-change-me}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "== wait health =="
for i in $(seq 1 60); do
  if curl -sf "$BASE/health" >/tmp/tr_health.json 2>/dev/null; then
    break
  fi
  sleep 2
done
python3 - <<'PY'
import json
d=json.load(open("/tmp/tr_health.json"))
db=d.get("database") or {}
print("version", d.get("version"), "backend", db.get("backend"), "connected", db.get("connected"))
assert d.get("status")=="ok", d
assert db.get("backend")=="postgres", db
assert db.get("connected") is True, db
print("OK postgres health")
PY

echo "== clear + ingest litellm + agent fixtures =="
curl -sf -X POST "$BASE/api/v1/admin/clear?scope=all" \
  -H "Authorization: Bearer $WRITE_KEY" >/dev/null
curl -sf -X POST "$BASE/api/v1/ingest/litellm" \
  -H "Authorization: Bearer $WRITE_KEY" \
  -H 'Content-Type: application/json' \
  -d @"$ROOT/fixtures/litellm_spend_logs.json" | tee /tmp/tr_ingest1.json | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d); assert d.get("accepted",0)>0'
curl -sf -X POST "$BASE/api/v1/ingest/litellm" \
  -H "Authorization: Bearer $WRITE_KEY" \
  -H 'Content-Type: application/json' \
  -d @"$ROOT/fixtures/litellm_spend_logs.json" | python3 -c 'import sys,json; d=json.load(sys.stdin); print("second", d); assert d.get("accepted",0)==0 and d.get("skipped",0)>0'
curl -sf -X POST "$BASE/api/v1/ingest/litellm" \
  -H "Authorization: Bearer $WRITE_KEY" \
  -H 'Content-Type: application/json' \
  -d @"$ROOT/fixtures/agent_team_spend_logs.json" >/dev/null
# other adapters for coverage
for f in cursor_usage_events.json workbuddy_events.json; do
  name=${f%%_*}
  case "$f" in
    cursor*) path=cursor ;;
    workbuddy*) path=workbuddy ;;
  esac
  curl -sf -X POST "$BASE/api/v1/ingest/$path" \
    -H "Authorization: Bearer $WRITE_KEY" \
    -H 'Content-Type: application/json' \
    -d @"$ROOT/fixtures/$f" >/dev/null || true
done

echo "== rankings =="
curl -sf "$BASE/api/v1/rankings/people?window=all" | python3 -c 'import sys,json; d=json.load(sys.stdin); assert d.get("items"), d; print("people", len(d["items"]))'
curl -sf "$BASE/api/v1/rankings/projects?window=all&hide_unlabeled=true" | python3 -c 'import sys,json; d=json.load(sys.stdin); assert d.get("items"), d; print("projects", len(d["items"]))'
curl -sf "$BASE/api/v1/rankings/agents?window=all&account_id=acme-prod" | python3 -c 'import sys,json; d=json.load(sys.stdin); assert d.get("items"), d; print("agents", len(d["items"]))'

echo "== rbac =="
bash "$ROOT/scripts/verify_rbac.sh"

echo "ALL POSTGRES CHECKS PASSED"
