# 真机 / 假落盘探测说明（WorkBuddy / Cursor / ProCode）

本仓库**不要求**在开发/CI 环境真装 Cursor、WorkBuddy 或 ProCode。用 `fixtures/probe_home/` 模拟家目录落盘，跑通 **probe → parse → scan/ingest** 闭环；上线前再在真实电脑核对一次。

## 一键探测（推荐）

```bash
cd /workspace/token-rankings   # 或你的克隆路径

# 用假落盘样本（CI / 本环境无真软件时）
python scripts/probe_local_tools.py \
  --home fixtures/probe_home \
  --out /tmp/token-rankings-probe

# 看报告
cat /tmp/token-rankings-probe.md

# 真机（有安装时改成真实 $HOME）
python scripts/probe_local_tools.py --home "$HOME" --out ~/token-rankings-probe
```

报告含：路径是否存在、jsonl 行数、usage 解析成功率、缺字段/大 content 跳过计数、内容泄漏检查。

## 通用原则

1. **只抽 usage**：不上传对话原文、代码片段、截图里的 prompt。
2. **含大字段的 content/prompt → 整行跳过**（计入 `skipped_forbidden_content`），不只是悄悄删字段后入库。
3. **统一口径**：`model`、`prompt_tokens`、`completion_tokens`、可选 cache/reasoning、`account_id`、`project_id`、`agent_id`、`request_id`、`tool`、`source`、`timestamp`。
4. **幂等**：同一 `(source, request_id)` 推两次不得叠量。
5. **对照 fixture**：先假落盘跑通，再拿真机一条脱敏事件 diff 字段名。

---

## WorkBuddy（本地 jsonl）

### 路径

- 默认：`~/.workbuddy/projects/**/*.jsonl`（`WORKBUDDY_PROJECTS_DIR` 可覆盖）
- 假落盘：`fixtures/probe_home/.workbuddy/projects/`
- 兼容口述「Workbody」目录名（探测脚本也会扫 `~/.workbody/projects`）

### 闭环

```bash
export TOKEN_RANKINGS_API_KEY=dev-token-rankings-change-me

# 探测
python scripts/probe_local_tools.py --home fixtures/probe_home --out /tmp/wb-probe

# 扫描入库 —— Docker Compose 已挂载假落盘到 /probe/workbuddy/projects
curl -s -X POST "http://localhost:8000/api/v1/scan/workbuddy?root=/probe/workbuddy/projects" \
  -H "Authorization: Bearer $TOKEN_RANKINGS_API_KEY" | jq .

# 宿主机直接跑服务时也可用：
# curl ... "?root=$(pwd)/fixtures/probe_home/.workbuddy/projects"

# 或只推已解析事件
curl -s -X POST http://localhost:8000/api/v1/ingest/workbuddy \
  -H "Authorization: Bearer $TOKEN_RANKINGS_API_KEY" \
  -H 'Content-Type: application/json' \
  -d @fixtures/workbuddy_events.json
```

扫描响应含 `parse_stats`（成功率、forbidden_content / missing_model 等计数）。

### 字段变体（适配器已收紧）

| 含义 | 别名示例 |
|---|---|
| 模型 | `model` / `model_name` / `modelName` / `model_id` |
| Token | 顶层或 `usage` / `token_usage` 内：`prompt_tokens`/`input_tokens`/`input`/`prompt` 等 |
| 账号 | `account_id` / `account` / `email` / `userEmail` / `owningUser` |
| 项目 | `project_id` / `projectId` / `project` / 目录名 / `cwd` |
| 请求 | `request_id` / `requestId` / `callId` / `eventId` / `id` |
| Agent | `agent_id` / `bot_id` / `automationId` / `cloudAgentId` |

---

## Cursor（Admin 优先；本地 export 对照）

### 推荐路径

- **Admin API**：`POST https://api.cursor.com/teams/filtered-usage-events`
- 本服务：`POST /api/v1/sync/cursor` 或离线 JSON → `POST /api/v1/ingest/cursor`
- **不要**把浏览器 cookie / `state.vscdb` 当生产源

### 假落盘

- `fixtures/probe_home/.cursor/exports/filtered-usage-events.json`（含 `owningUser`、`timestampMs`、`token_usage` snake_case、缺 model 负例）

### 闭环

```bash
python scripts/probe_local_tools.py --home fixtures/probe_home --out /tmp/cur-probe

curl -s -X POST http://localhost:8000/api/v1/ingest/cursor \
  -H "Authorization: Bearer $TOKEN_RANKINGS_API_KEY" \
  -H 'Content-Type: application/json' \
  -d @fixtures/probe_home/.cursor/exports/filtered-usage-events.json
```

额外字段别名：`owningUser`、`timestampMs`、`charged_cents`、`token_usage.input_tokens`、`conversation_id`、`eventId`。

---

## ProCode / Canoryn（占位）

公开用量 schema **不足**，不做深适配。探测脚本会扫 `~/.canoryn*`、`~/.procode*` 并写入报告；页面说明见 **`/procode`**。

**闭环**：经 LiteLLM / 网关打 `tool=procode`（header 或 tags）→ 统一 usage → `POST /api/v1/ingest/procode`。样例：`fixtures/procode_events.json`。健康检查 `adapters[]` 里 ProCode 状态为 **`placeholder`**。

---

## 探测结论怎么回流

1. 记下实际路径、脱敏字段表、与 fixture 的 diff。
2. 改 `app/adapters/workbuddy.py` / `cursor_admin.py` 别名（ProCode 仍走统一 schema）。
3. 更新 `fixtures/`（禁止提交真实对话）。
4. 回归：`probe_local_tools.py` + 重复 ingest → `skipped`；个人/项目/Agent 榜能拆开。

另见：`docs/NON_GOALS.md`（不做自研网关 / 不做公网热度榜）。
