#!/usr/bin/env bash
# 客户验收冒烟：健康检查 + 导入一份示例 + 排行（个人榜 / Agent / 我的）。
# 任一步失败即以非 0 退出。不要在输出里打印密钥。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

BASE="${BASE_URL:-http://localhost:8000}"
FIXTURE="${FIXTURE:-$ROOT/fixtures/litellm_spend_logs.json}"
ALICE="${ALICE_KEY:-demo-alice-key}"

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

echo "== 1/4 健康检查 $BASE/health =="
HEALTH="$(curl -sfS --max-time 20 "$BASE/health")" || fail "打不开 /health。请先按 docs/INSTALL.md 启动服务"
python3 -c 'import json,sys
d=json.load(sys.stdin)
assert d.get("status")=="ok", d
print("version", d.get("version"), "events", d.get("events"))
' <<<"$HEALTH" || fail "健康检查内容异常"

echo "== 2/4 导入示例 =="
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

echo "== 3/4 排行（个人榜 + Agent） =="
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

echo "== 4/4 我的（成员账号） =="
ME_CODE="$(curl -sS --max-time 20 -o /tmp/token-rank-smoke-me.json -w '%{http_code}' \
  -H "Authorization: Bearer $ALICE" \
  "$BASE/api/v1/me?account_id=acme-prod&window=all")" || fail "请求 /api/v1/me 失败"
if [[ "$ME_CODE" != "200" ]]; then
  head -c 400 /tmp/token-rank-smoke-me.json >&2 || true
  echo >&2
  fail "打开「我的」返回 HTTP $ME_CODE。演示成员密钥是 demo-alice-key；若已更换，请设置 ALICE_KEY"
fi
python3 -c 'import json,sys
d=json.load(open(sys.argv[1], encoding="utf-8"))
assert d.get("account_id")=="acme-prod", d
print("me", d.get("account_id"))
' /tmp/token-rank-smoke-me.json || fail "「我的」没有返回账号 acme-prod"

echo "ALL CUSTOMER SMOKE CHECKS PASSED"
