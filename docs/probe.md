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
- **本机采集**（`python -m collector`）会读 `state.vscdb` 里 `bubbleId:*` 的 token 与模型名，以及 `~/.cursor/exports` 一类 usage JSON。SQL 不取出对话正文，也不读 `cursorAuth` 的 access token / cookie
- 新版 Cursor 常把本地气泡 token 写成 0。这种行会跳过，不拿字符数冒充。把仪表盘导出的 `filtered-usage-events.json`（不要带 prompt）放到 `~/.cursor/exports/`

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

## ProCode / Canoryn（聊天库仍不解密）

Canoryn 的公开文档写明：对话和记忆是加密 SQLite，密钥在 macOS 钥匙串，`canoryn` 命令会连接正在运行的 App。**没有**公开的本机用量 API。探测和采集都不解密、不打开 sqlite、不读钥匙串、不启动 `canoryn`。健康检查 `adapters[]` 里 ProCode 的 `status` 仍为 **`placeholder`**，`blocked` 为 `encrypted_sqlite`、`keychain`、`canoryn_cli`。页面说明见 **`/procode`**。

今天能解析的是 usage 形状的 json/jsonl。目录见 `app/adapters/procode.py` 的 `PROCODE_HOME_RELATIVE`（含 Linux/macOS/Windows 的常见大小写，以及 XDG / APPDATA）。Linux 和 Windows 上的这些目录是给操作者**放置导出**的，不是 Canoryn 的安装位置。Canoryn 应用本身在 macOS。

明确跳过、且不打开的内容：

- `*.sqlite`、`*.sqlite3`、`*.db`、`*.cryn`、`*.crynpack`（只在 diagnostics 里计数）
- `discussions/`（仓库评论导出）
- `blueprints/`（工作流 JSON，可能含提示词）
- 文件名像凭证的 json（`api_key`、`secret`、`keychain`、`access_token` 等）

认这些导出形状：顶层或 `usage` / `usageEvents` 里的 `model` / `modelName`，`prompt_tokens` / `inputTokens`，`userEmail`，`projectPath` 或 `cwd`（只留最后一级目录名）。未知模型保留原名，估价用 `default` 档。

样例：`fixtures/procode_events.json`、`fixtures/procode_sample/`（含 `exports/token-usage.json` 和必须跳过的 sqlite / discussions / blueprints）、`fixtures/probe_home/`。

**闭环**：目录里放 usage 文件后 `python -m collector once --tool procode`（`--tool canoryn` 相同），或把同一份字段 `POST /api/v1/ingest/procode`。这不是一条新的模型网关。真机打勾见 [ACCEPTANCE.md](ACCEPTANCE.md) 的「真机采集验收」。

---

## 探测结论怎么回流

1. 记下实际路径、脱敏字段表、与 fixture 的 diff。
2. 改 `app/adapters/workbuddy.py` / `cursor_admin.py` 别名（ProCode 仍只收 usage json/jsonl，不解密）。
3. 更新 `fixtures/`（禁止提交真实对话）。
4. 回归：`probe_local_tools.py` + 重复 ingest → `skipped`；个人/项目/Agent 榜能拆开。

另见：`docs/NON_GOALS.md`（不做自研网关 / 不做公网热度榜）。
