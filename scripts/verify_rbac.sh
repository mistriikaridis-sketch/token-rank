#!/usr/bin/env bash
# Verify member cannot see other accounts' project rankings / export.
set -euo pipefail
BASE="${BASE_URL:-http://localhost:8000}"
WRITE_KEY="${TOKEN_RANKINGS_API_KEY:-dev-token-rankings-change-me}"
ALICE="${ALICE_KEY:-demo-alice-key}"
DAVE="${DAVE_KEY:-demo-dave-key}"
ADMIN="${ADMIN_KEY:-demo-admin-key}"

echo "== health =="
curl -sf "$BASE/health" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('version'), 'users=', d.get('rbac',{}).get('user_count'), 'db=', d.get('database',{}).get('backend'))"

echo "== alice projects (must only acme-prod) =="
ALICE_JSON=$(curl -sf -H "Authorization: Bearer $ALICE" "$BASE/api/v1/rankings/projects?window=all&hide_unlabeled=true")
printf '%s' "$ALICE_JSON" | python3 -c '
import json,sys
d=json.load(sys.stdin)
accounts=sorted({i.get("account_id") for i in d.get("items",[]) if i.get("account_id")})
print("accounts:", accounts)
assert accounts == ["acme-prod"], accounts
print("OK alice scoped")
'

echo "== alice cannot request dave account =="
CODE=$(curl -s -o /tmp/rbac_forbid.json -w '%{http_code}' -H "Authorization: Bearer $ALICE" \
  "$BASE/api/v1/rankings/projects?window=all&account_id=dave%40acme.com")
echo "HTTP $CODE"
test "$CODE" = "403"

echo "== dave projects (must only dave@acme.com) =="
DAVE_JSON=$(curl -sf -H "Authorization: Bearer $DAVE" "$BASE/api/v1/rankings/projects?window=all&hide_unlabeled=true")
printf '%s' "$DAVE_JSON" | python3 -c '
import json,sys
d=json.load(sys.stdin)
accounts=sorted({i.get("account_id") for i in d.get("items",[]) if i.get("account_id")})
print("accounts:", accounts)
assert accounts == ["dave@acme.com"], accounts
print("OK dave scoped")
'

echo "== export requires auth =="
CODE=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/v1/export/usage.csv?window=all")
echo "no key HTTP $CODE"
test "$CODE" = "401"

echo "== alice export =="
curl -sf -H "Authorization: Bearer $ALICE" "$BASE/api/v1/export/usage.csv?window=all" | head -n 2
CODE=$(curl -s -o /tmp/audit_forbid.json -w '%{http_code}' -H "Authorization: Bearer $ALICE" \
  "$BASE/api/v1/admin/audit")
echo "alice audit HTTP $CODE (expect 403)"
test "$CODE" = "403"

echo "== admin audit =="
AUDIT=$(curl -sf -H "Authorization: Bearer $ADMIN" "$BASE/api/v1/admin/audit?limit=5")
printf '%s' "$AUDIT" | python3 -c '
import json,sys
d=json.load(sys.stdin)
assert d.get("count",0) >= 1, d
print("latest action:", d["items"][0]["action"], "rows:", d["items"][0]["row_count"])
print("OK audit visible to admin")
'

echo "== write key still works =="
RID="rbac-verify-$(date +%s)-$$"
curl -sf -X POST "$BASE/api/v1/usage" \
  -H "Authorization: Bearer $WRITE_KEY" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"gpt-4o-mini\",\"prompt_tokens\":1,\"completion_tokens\":1,\"request_id\":\"$RID\",\"account_id\":\"acme-prod\",\"source\":\"verify\"}" \
  | python3 -c "import sys,json; print('ingest id', json.load(sys.stdin).get('id'))"

echo "ALL RBAC CHECKS PASSED"
