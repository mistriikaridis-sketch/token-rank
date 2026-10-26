#!/usr/bin/env bash
# 客户验收冒烟：健康检查 + 导入一份示例 + 排行 + 成员隔离 + 生产登录路径。
# 任一步失败即以非 0 退出。不要在输出里打印密钥。
#
# 试用服务（RANKINGS_REQUIRE_AUTH=0、演示密钥）仍可通过。
# 交付验收请再跑：SMOKE_REQUIRE_AUTH=1 bash scripts/customer_smoke.sh
#   此时目标服务必须 rankings_require_auth=true 且 demo_keys_in_use=false。
# 本机装了 Python 依赖时，脚本还会用内存探针验证 AUTH=1（不改动目标库）。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

BASE="${BASE_URL:-http://localhost:8000}"
FIXTURE="${FIXTURE:-$ROOT/fixtures/litellm_spend_logs.json}"
ALICE="${ALICE_KEY:-demo-alice-key}"
ALICE_ACCOUNT="${ALICE_ACCOUNT:-acme-prod}"
OTHER_ACCOUNT="${OTHER_ACCOUNT:-dave@acme.com}"
SMOKE_REQUIRE_AUTH="${SMOKE_REQUIRE_AUTH:-0}"
export ALICE_ACCOUNT OTHER_ACCOUNT SMOKE_REQUIRE_AUTH

read_env_value() {
  local key="$1"
  local file="$2"
  [[ -f "$file" ]] || return 0
  local line
  line="$(grep -E "^[[:space:]]*${key}=" "$file" | tail -n 1 || true)"
  [[ -n "$line" ]] || return 0
  local val="${line#*=}"
  val="${val#"${val%%[![:space:]]*}"}"
  val="${val%"${val##*[![:space:]]}"}"
  if [[ "$val" == \"*\" && "$val" == *\" ]]; then
    val="${val:1:${#val}-2}"
  elif [[ "$val" == \'*\' && "$val" == *\' ]]; then
    val="${val:1:${#val}-2}"
  fi
  printf '%s' "$val"
}

KEY="${TOKEN_RANKINGS_API_KEY:-}"
if [[ -z "$KEY" ]]; then
  KEY="$(read_env_value TOKEN_RANKINGS_API_KEY "$ROOT/.env")"
fi
KEY="${KEY:-dev-token-rankings-change-me}"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

command -v curl >/dev/null 2>&1 || fail "需要 curl"
command -v python3 >/dev/null 2>&1 || fail "需要 python3"
[[ -f "$FIXTURE" ]] || fail "找不到示例文件: $FIXTURE"

echo "== 1/7 健康检查 $BASE/health =="
HEALTH="$(curl -sfS --max-time 20 "$BASE/health")" || fail "打不开 /health。请先按 docs/INSTALL.md 启动服务"
python3 -c 'import json,sys,pathlib
d=json.load(sys.stdin)
assert d.get("status")=="ok", d
expected=pathlib.Path("VERSION").read_text(encoding="utf-8").strip()
assert d.get("version")==expected, (d.get("version"), expected)
setup=d.get("setup") or {}
assert "demo_keys_in_use" in setup, setup
assert "rankings_require_auth" in setup, setup
print("version", d.get("version"), "events", d.get("events"),
      "demo_keys_in_use", setup.get("demo_keys_in_use"),
      "rankings_require_auth", setup.get("rankings_require_auth"))
' <<<"$HEALTH" || fail "健康检查内容异常"

echo "== 2/7 导入示例 =="
CODE="$(curl -sS --max-time 60 -o /tmp/token-rank-smoke-ingest.json -w '%{http_code}' \
  -X POST "$BASE/api/v1/ingest/litellm" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  --data-binary @"$FIXTURE")" || fail "导入请求没有完成"
if [[ "$CODE" != "200" ]]; then
  echo "导入响应（前 400 字）：" >&2
  head -c 400 /tmp/token-rank-smoke-ingest.json >&2 || true
  echo >&2
  fail "导入返回 HTTP $CODE（期望 200）。请确认写入密钥与服务器 TOKEN_RANKINGS_API_KEY 一致"
fi
python3 -c 'import json,sys
d=json.load(open(sys.argv[1], encoding="utf-8"))
assert "accepted" in d and "skipped" in d, d
total=int(d["accepted"])+int(d["skipped"])
assert total>0, d
print("accepted", d["accepted"], "skipped", d["skipped"])
' /tmp/token-rank-smoke-ingest.json || fail "导入结果无法解析，或没有写入任何行"

echo "== 3/7 排行（个人榜 + Agent） =="
PEOPLE="$(curl -sfS --max-time 20 -H "Authorization: Bearer $KEY" \
  "$BASE/api/v1/rankings/people?window=all")" || fail "打不开个人排行"
python3 -c 'import json,sys
d=json.load(sys.stdin)
items=d.get("items") or []
assert items, d
print("people", len(items))
' <<<"$PEOPLE" || fail "个人排行没有数据。请确认示例已导入"

AGENTS="$(curl -sfS --max-time 20 -H "Authorization: Bearer $KEY" \
  "$BASE/api/v1/rankings/agents?window=all&hide_unlabeled=false")" || fail "打不开 Agent 排行"
python3 -c 'import json,sys
d=json.load(sys.stdin)
items=d.get("items") or []
assert items, d
print("agents", len(items))
' <<<"$AGENTS" || fail "Agent 排行没有数据。请确认示例已导入"

echo "== 4/7 我的（成员账号） =="
ME_CODE="$(curl -sS --max-time 20 -G -o /tmp/token-rank-smoke-me.json -w '%{http_code}' \
  -H "Authorization: Bearer $ALICE" \
  --data-urlencode "account_id=$ALICE_ACCOUNT" \
  --data-urlencode "window=all" \
  "$BASE/api/v1/me")" || fail "请求 /api/v1/me 失败"
if [[ "$ME_CODE" != "200" ]]; then
  head -c 400 /tmp/token-rank-smoke-me.json >&2 || true
  echo >&2
  fail "打开「我的」返回 HTTP $ME_CODE。演示成员密钥是 demo-alice-key；若已更换，请设置 ALICE_KEY 和 ALICE_ACCOUNT"
fi
python3 -c 'import json,os,sys
d=json.load(open(sys.argv[1], encoding="utf-8"))
expect=os.environ["ALICE_ACCOUNT"]
assert d.get("account_id")==expect, d
print("me", d.get("account_id"))
' /tmp/token-rank-smoke-me.json || fail "「我的」没有返回成员自己的账号"

echo "== 5/7 成员不能读取别人的账号（403） =="
READ_CODE="$(curl -sS --max-time 20 -G -o /tmp/token-rank-smoke-forbid.json -w '%{http_code}' \
  -H "Authorization: Bearer $ALICE" \
  --data-urlencode "window=all" \
  --data-urlencode "account_id=$OTHER_ACCOUNT" \
  "$BASE/api/v1/rankings/projects")" || fail "跨账号读取请求没有完成"
if [[ "$READ_CODE" != "403" ]]; then
  head -c 400 /tmp/token-rank-smoke-forbid.json >&2 || true
  echo >&2
  fail "成员读取别人的账号返回 HTTP $READ_CODE（期望 403）"
fi
echo "read other account HTTP 403"

echo "== 6/7 成员不能写入别人的账号（403） =="
python3 -c 'import json,os
json.dump({
  "model":"gpt-4o-mini",
  "prompt_tokens":1,
  "completion_tokens":1,
  "request_id":"smoke-member-write",
  "account_id":os.environ["OTHER_ACCOUNT"],
  "source":"smoke",
}, open("/tmp/token-rank-smoke-write-body.json","w",encoding="utf-8"))'
WRITE_CODE="$(curl -sS --max-time 20 -o /tmp/token-rank-smoke-write.json -w '%{http_code}' \
  -X POST "$BASE/api/v1/usage" \
  -H "Authorization: Bearer $ALICE" \
  -H "Content-Type: application/json" \
  --data-binary @/tmp/token-rank-smoke-write-body.json)" || fail "成员写入请求没有完成"
if [[ "$WRITE_CODE" != "403" ]]; then
  head -c 400 /tmp/token-rank-smoke-write.json >&2 || true
  echo >&2
  fail "成员写入别人的账号返回 HTTP $WRITE_CODE（期望 403）"
fi
echo "write other account HTTP 403"

echo "== 7/7 登录开关与演示密钥 =="
export SMOKE_REQUIRE_AUTH HEALTH BASE
python3 - <<'PY' || fail "生产登录检查未通过"
import json, os, subprocess, sys
health = json.loads(os.environ["HEALTH"])
setup = health.get("setup") or {}
require = bool(setup.get("rankings_require_auth"))
demo = setup.get("demo_keys_in_use")
strict = os.environ.get("SMOKE_REQUIRE_AUTH") == "1"
print("target rankings_require_auth", require, "demo_keys_in_use", demo)
if strict or require:
    if not require:
        print("FAIL: SMOKE_REQUIRE_AUTH=1 但服务未设置 RANKINGS_REQUIRE_AUTH=1", file=sys.stderr)
        sys.exit(1)
    if demo is not False:
        print("FAIL: 交付验收要求 demo_keys_in_use=false", file=sys.stderr)
        sys.exit(1)
    proc = subprocess.run(
        ["curl", "-sS", "--max-time", "20", "-o", "/tmp/token-rank-smoke-anon.json", "-w", "%{http_code}",
         os.environ["BASE"].rstrip("/") + "/api/v1/rankings/people?window=all"],
        check=False, capture_output=True, text=True,
    )
    code = (proc.stdout or "").strip()
    print("anonymous rankings HTTP", code)
    if code != "401":
        print("FAIL: 已要求登录时，不带密钥看排行应返回 401", file=sys.stderr)
        sys.exit(1)
else:
    print("当前目标是试用模式。黄条是否出现取决于 demo_keys_in_use。")
    print("交付请换成自己的密钥，设 RANKINGS_REQUIRE_AUTH=1，再以 SMOKE_REQUIRE_AUTH=1 重跑。")
    print("黄条消失的判据：/health 里 setup.demo_keys_in_use 为 false。")
PY

if python3 -c 'import fastapi' >/dev/null 2>&1; then
  echo "-- 内置探针：RANKINGS_REQUIRE_AUTH=1 且 demo_keys_in_use=false --"
  python3 - <<'PY' || fail "内置 AUTH=1 探针未通过"
import hashlib, json, os, pathlib, secrets, shutil, tempfile
root = pathlib.Path(".").resolve()
tmp = pathlib.Path(tempfile.mkdtemp(prefix="tr-smoke-auth-"))
os.environ["DATA_DIR"] = str(tmp)
os.environ.pop("DATABASE_URL", None)
os.environ["SEED_ON_START"] = "0"
os.environ["FORUM_SEED_ON_START"] = "0"
os.environ.pop("USERS_JSON", None)
write_key = "trk_smoke_write_" + secrets.token_hex(16)
alice = "trk_smoke_alice_" + secrets.token_hex(16)
dave = "trk_smoke_dave_" + secrets.token_hex(16)
os.environ["TOKEN_RANKINGS_API_KEY"] = write_key
os.environ["RANKINGS_REQUIRE_AUTH"] = "1"
users = [
    {
        "api_key_sha256": hashlib.sha256(alice.encode()).hexdigest(),
        "role": "member",
        "account_id": "acme-prod",
        "name": "Alice",
    },
    {
        "api_key": dave,
        "role": "member",
        "account_id": "dave@acme.com",
        "name": "Dave",
    },
]
users_path = tmp / "users.json"
users_path.write_text(json.dumps(users), encoding="utf-8")
os.environ["USERS_FILE"] = str(users_path)

from fastapi.testclient import TestClient
from app.main import app

try:
  with TestClient(app) as client:
    health = client.get("/health")
    assert health.status_code == 200, health.text
    body = health.json()
    setup = body["setup"]
    assert setup["rankings_require_auth"] is True, setup
    assert setup["demo_keys_in_use"] is False, setup
    assert body["version"] == (root / "VERSION").read_text(encoding="utf-8").strip()
    anon = client.get("/api/v1/rankings/people?window=all")
    assert anon.status_code == 401, anon.status_code
    fixture = json.loads((root / "fixtures" / "litellm_spend_logs.json").read_text(encoding="utf-8"))
    ingested = client.post(
        "/api/v1/ingest/litellm",
        headers={"Authorization": f"Bearer {write_key}"},
        json=fixture,
    )
    assert ingested.status_code == 200, ingested.text
    forbidden = client.get(
        "/api/v1/rankings/projects",
        params={"window": "all", "account_id": "dave@acme.com"},
        headers={"Authorization": f"Bearer {alice}"},
    )
    assert forbidden.status_code == 403, forbidden.text
    write_forbidden = client.post(
        "/api/v1/usage",
        headers={"Authorization": f"Bearer {alice}"},
        json={
            "model": "gpt-4o-mini",
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "request_id": "embedded-member-write",
            "account_id": "dave@acme.com",
            "source": "smoke",
        },
    )
    assert write_forbidden.status_code == 403, write_forbidden.text
    me = client.get(
        "/api/v1/me",
        params={"account_id": "acme-prod", "window": "all"},
        headers={"Authorization": f"Bearer {alice}"},
    )
    assert me.status_code == 200, me.text
    assert me.json().get("account_id") == "acme-prod"

  from app.auth import setup_warnings
  demo_users = tmp / "demo-hash-users.json"
  demo_users.write_text(json.dumps([{
      "api_key_sha256": hashlib.sha256(b"demo-alice-key").hexdigest(),
      "role": "member",
      "account_id": "acme-prod",
      "name": "Alice",
  }]), encoding="utf-8")
  os.environ["USERS_FILE"] = str(demo_users)
  flagged = setup_warnings()
  assert flagged["demo_keys_in_use"] is True, flagged
  print("embedded AUTH=1 ok; demo hash still flags demo_keys_in_use")
finally:
    shutil.rmtree(tmp, ignore_errors=True)
PY
else
  echo "SKIP 内置 AUTH=1 探针：当前 python3 没有 fastapi。"
  echo "试用冒烟不因此失败。交付请在已安装依赖的环境重跑，或对生产服务设置 SMOKE_REQUIRE_AUTH=1。"
  if [[ "$SMOKE_REQUIRE_AUTH" == "1" ]]; then
    python3 -c 'import json,os
h=json.loads(os.environ["HEALTH"])
s=h.get("setup") or {}
assert s.get("rankings_require_auth") is True
assert s.get("demo_keys_in_use") is False
' || fail "目标服务未满足 AUTH=1 / demo_keys_in_use=false"
  fi
fi

echo "ALL CUSTOMER SMOKE CHECKS PASSED"
