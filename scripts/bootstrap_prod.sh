#!/usr/bin/env bash
# 生成交付用写入密钥和用户名单。
# 没有 --force 时不会覆盖已经写好的密钥。明文用户密钥只在生成时打印一次。
# 不要使用 dev-token-rankings-change-me、CHANGE_ME、demo-admin-key、demo-alice-key、demo-dave-key。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FORCE=0
ENV_FILE="$ROOT/.env"
USERS_FILE="$ROOT/fixtures/users.prod.json"

usage() {
  cat <<'EOF'
用法: bash scripts/bootstrap_prod.sh [--force] [--env-file PATH] [--users-file PATH]

生成：
  - 写入密钥，写入 .env 的 TOKEN_RANKINGS_API_KEY
  - RANKINGS_REQUIRE_AUTH=1
  - 用户名单（管理员 + 两名示例成员）。文件里只存 SHA-256，明文只打印一次

没有 --force 时：
  - 已有的非演示写入密钥不会被改掉
  - 已存在的用户名单文件不会被改掉
  演示值（CHANGE_ME、dev-token-rankings-change-me）会换成新密钥。

不要把 .env 或 users.prod.json 提交到仓库。
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --force) FORCE=1; shift ;;
    --env-file)
      [[ $# -ge 2 ]] || { echo "缺少 --env-file 的路径" >&2; exit 2; }
      ENV_FILE="$2"; shift 2 ;;
    --users-file)
      [[ $# -ge 2 ]] || { echo "缺少 --users-file 的路径" >&2; exit 2; }
      USERS_FILE="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; usage >&2; exit 2 ;;
  esac
done

export BOOTSTRAP_FORCE="$FORCE"
export BOOTSTRAP_ENV_FILE="$ENV_FILE"
export BOOTSTRAP_USERS_FILE="$USERS_FILE"
export BOOTSTRAP_ROOT="$ROOT"

python3 - <<'PY'
import hashlib
import json
import os
import secrets
import stat
import sys
from pathlib import Path

force = os.environ.get("BOOTSTRAP_FORCE") == "1"
env_path = Path(os.environ["BOOTSTRAP_ENV_FILE"]).expanduser()
users_path = Path(os.environ["BOOTSTRAP_USERS_FILE"]).expanduser()
root = Path(os.environ["BOOTSTRAP_ROOT"])
example = root / ".env.example"

DEMO_SERVICE = {"dev-token-rankings-change-me"}
PLACEHOLDER_EXACT = {"change_me", "changeme", "change-me", "todo", "secret", "xxx"}
PLACEHOLDER_FRAGMENTS = (
    "change-me",
    "change_me",
    "changeme",
    "replace-me",
    "your-key",
    "your_key",
)
DEMO_USER_PATHS = {
    "/app/fixtures/users.json",
    "fixtures/users.json",
    str(root / "fixtures" / "users.json"),
}
CONTAINER_USERS = "/run/token-rankings/users.json"


def is_placeholder(value: str) -> bool:
    key = (value or "").strip().lower()
    if not key:
        return True
    if key in PLACEHOLDER_EXACT or key in DEMO_SERVICE:
        return True
    return any(fragment in key for fragment in PLACEHOLDER_FRAGMENTS)


def parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        values[key] = val
    return values


def upsert(text: str, updates: dict[str, str]) -> str:
    lines = text.splitlines()
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        raw = line.strip()
        if raw and not raw.startswith("#") and "=" in line:
            key = line.split("=", 1)[0].strip()
            if key in updates:
                if key not in seen:
                    out.append(f"{key}={updates[key]}")
                    seen.add(key)
                continue
        out.append(line)
    for key, val in updates.items():
        if key not in seen:
            if out and out[-1] != "":
                out.append("")
            out.append(f"{key}={val}")
            seen.add(key)
    body = "\n".join(out)
    if not body.endswith("\n"):
        body += "\n"
    return body


def new_key() -> str:
    return "trk_" + secrets.token_urlsafe(32)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def write_secret(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


existing_env = env_path.read_text(encoding="utf-8") if env_path.is_file() else ""
if not existing_env and example.is_file():
    existing_env = example.read_text(encoding="utf-8")
env_values = parse_env(existing_env)

current_write = (env_values.get("TOKEN_RANKINGS_API_KEY") or "").strip()
write_key = current_write
write_generated = False
if force or is_placeholder(current_write):
    write_key = new_key()
    write_generated = True

current_users_env = (env_values.get("USERS_FILE") or "").strip()
users_env_value = current_users_env
if force or not current_users_env or current_users_env in DEMO_USER_PATHS or current_users_env.endswith("/fixtures/users.json"):
    users_env_value = CONTAINER_USERS

updates = {
    "TOKEN_RANKINGS_API_KEY": write_key,
    "RANKINGS_REQUIRE_AUTH": "1",
    "USERS_FILE": users_env_value,
}
new_env = upsert(existing_env, updates)
if new_env != existing_env or not env_path.is_file():
    write_secret(env_path, new_env)
else:
    os.chmod(env_path, stat.S_IRUSR | stat.S_IWUSR)

users_generated = False
printed_users: list[tuple[str, str, str]] = []
if users_path.is_file() and not force:
    pass
else:
    admin = new_key()
    member = new_key()
    other = new_key()
    roster = [
        {
            "api_key_sha256": sha256_hex(admin),
            "role": "admin",
            "name": "管理员",
        },
        {
            "api_key_sha256": sha256_hex(member),
            "role": "member",
            "account_id": "sample-member",
            "name": "示例成员",
        },
        {
            "api_key_sha256": sha256_hex(other),
            "role": "member",
            "account_id": "sample-other",
            "name": "示例成员乙",
        },
    ]
    write_secret(users_path, json.dumps(roster, ensure_ascii=False, indent=2) + "\n")
    users_generated = True
    printed_users = [
        ("管理员", "（全站）", admin),
        ("成员", "sample-member", member),
        ("成员", "sample-other", other),
    ]

print("Token 消耗排行榜 · 生产引导")
print("这不是大模型网关，也不是公开热度榜。下面的密钥只给你们自己的内网用量。")
print()
if write_generated:
    print("== 写入密钥（只打印这一次，已写入 .env）==")
    print(f"TOKEN_RANKINGS_API_KEY={write_key}")
    print("只给导入脚本使用。不要贴进浏览器，不要发到群里。")
else:
    print("写入密钥已存在，未改写。需要轮换时加 --force。")
print()
if users_generated:
    print("== 用户密钥（只打印这一次；文件里只存 SHA-256）==")
    for role, account, key in printed_users:
        print(f"{role} {account}: {key}")
    print(f"名单文件: {users_path}")
    print("请立刻抄下来。丢失后只能 --force 重新生成，旧密钥会作废。")
else:
    print(f"用户名单已存在，未改写: {users_path}")
    print("明文不会再次打印。丢失请加 --force，并作废旧密钥。")
print()
print("不要使用这些演示值：")
print("  dev-token-rankings-change-me   CHANGE_ME")
print("  demo-admin-key   demo-alice-key   demo-dave-key")
print("  fixtures/users.json 只用于试用，交付时不要再挂它。")
print()
print("Docker 请同时做两件事（只反代/挂载本应用，不要做成网关）：")
print("  1. .env 里 USERS_FILE 现为容器内路径:")
print(f"       USERS_FILE={users_env_value}")
print("  2. 在 docker-compose.yml 的 token-rankings.volumes 取消注释：")
print(f"       - {users_path}:/run/token-rankings/users.json:ro")
print("     若名单不在默认路径，把冒号左边换成你的文件。")
print("  然后: docker compose up -d --build")
print()
print("本机不用 Docker 时，启动前导出（.env 不会被 uvicorn 自动读取）：")
print(f"  export USERS_FILE={users_path}")
print("  export TOKEN_RANKINGS_API_KEY=<上面的写入密钥>")
print("  export RANKINGS_REQUIRE_AUTH=1")
print()
print("确认演示密钥黄条消失：")
print("  curl -sS http://localhost:8000/health | python3 -c 'import json,sys; d=json.load(sys.stdin); s=d.get(\"setup\") or {}; assert s.get(\"demo_keys_in_use\") is False, s; assert s.get(\"rankings_require_auth\") is True, s; print(\"demo_keys_in_use=false\")'")
print("  再打开 http://localhost:8000 。顶部不应再出现黄色「演示密钥仍在使用」。")
print("  若黄条还在：写入密钥或用户名单仍是演示值，或容器仍挂着 fixtures/users.json。")
print("  只换了密钥但 RANKINGS_REQUIRE_AUTH 仍是 0 时，黄条会消失，健康检查里仍会提示生产要设为 1。本脚本已写成 1。")

if not write_key or is_placeholder(write_key):
    print("写入密钥生成失败", file=sys.stderr)
    sys.exit(1)
PY
