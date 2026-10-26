# Token 消耗排行榜

许可证：**MIT** · 版本 **0.18.0**

## 客户 10 分钟跑通

给不写代码的同事。装好 Docker 后，在本仓库目录执行：

```bash
docker compose up -d --build
```

浏览器打开 <http://localhost:8000>。

页面顶部如果出现黄条，说明还在用**演示密钥**。自己试用可以先不管；交给别人用之前，按 [安全说明](docs/SECURITY.md) 全部换掉，并把生产环境的 `RANKINGS_REQUIRE_AUTH` 设为 `1`。

导入一份自带示例（让榜上出现数据）：

```bash
export TOKEN_RANKINGS_API_KEY=dev-token-rankings-change-me
curl -s -X POST http://localhost:8000/api/v1/ingest/litellm \
  -H "Authorization: Bearer $TOKEN_RANKINGS_API_KEY" \
  -H 'Content-Type: application/json' \
  -d @fixtures/litellm_spend_logs.json
```

回到网页，在 **首次配置** 里粘贴用户密钥 `demo-alice-key`，账号选 `acme-prod`，然后看 [我的](http://localhost:8000/me) 和 [Agent](http://localhost:8000/agents)。

确认跑通：

```bash
bash scripts/customer_smoke.sh
```

看到 `ALL CUSTOMER SMOKE CHECKS PASSED` 才算通过。失败时脚本以非 0 退出。若你已经把 `.env` 里的写入密钥换成了自己的值，先 `export TOKEN_RANKINGS_API_KEY=那一串` 再跑脚本。

交给别人用之前运行 `bash scripts/bootstrap_prod.sh`（见 [安装说明](docs/INSTALL.md)）。没有 `--force` 时不会覆盖已有密钥。交付后再跑一次 `SMOKE_REQUIRE_AUTH=1 bash scripts/customer_smoke.sh`，确认 `demo_keys_in_use=false`。

| 文档 | 内容 |
|---|---|
| [docs/INSTALL.md](docs/INSTALL.md) | 安装：Docker 一条命令、可选 Postgres、换掉全部演示密钥 |
| [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md) | 验收：健康检查、导入一份示例、看到 /me 与 /agents、成员只能看自己的账号 |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | 打不开、401、403、榜是空的 |
| [docs/SECURITY.md](docs/SECURITY.md) | 试用默认不强制登录；生产请设 `RANKINGS_REQUIRE_AUTH=1`；不要公开哪些东西 |

本产品只做企业内部用量排行。**不做**自研大模型网关，也**不做**公网热度榜。见 [docs/NON_GOALS.md](docs/NON_GOALS.md)。

试用默认使用 SQLite。需要 Postgres 时：

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d --build
```

---

## 给实施同事

企业内 LLM **Token / 估算成本排行榜**。暗色 + 琥珀金 UI：

- **个人榜** `/people`：按参与者排行（主力工具 / 主力模型 / Token / 估算成本）；可链到「我的 Agents」
- **组织榜** `/orgs`：按部门排行（总量 / 人均 / 人数）；可按 **工具** 筛选
- **项目榜** `/projects`：按 **账号 × 项目** 拆开看，证明不是总量黑盒
- **Agent 榜** `/agents`：按 **agent_id** 看团队里每个 agent 大概烧多少；可用账号下拉看「我的 agents」
- **我的** `/me`：选账号看自己的 Agent / 项目 Top、工具分布、本周文案 / 分享图
- **提醒** `/alerts`：低消耗告警（建设性文案）；member 只看自己
- **贴 Key**：顶栏把用户 API Key 存 localStorage，排行 /me /export /alerts /forum 自动带 Header（勿存 ingest Key）
- **交流区** `/forum`：企业内部最小讨论（发帖 / 列表 / 点赞）；匿名只读；禁止贴 Key/对话原文
- **ProCode** `/procode`：占位适配说明；`POST /api/v1/ingest/procode`

许可证：**MIT** · 版本 **0.18.0**（客户文档见文首「客户 10 分钟跑通」）

## 本地探测

```bash
python scripts/probe_local_tools.py --home fixtures/probe_home --out /tmp/token-rankings-probe
```

详见 `docs/probe.md` · 非目标 `docs/NON_GOALS.md`。

## 产品原则（口径）

1. **统一口径**：LiteLLM、Cursor Admin、WorkBuddy、Claude Code、Codex CLI、Continue 等 usage **归一到同一 schema** 再入库（`model`、`prompt/completion/cache/reasoning tokens`、`participant`、`team`、`tool`、`account_id`、`project_id`、**`agent_id` / `agent_name`（可选）**、`request_id`、`source`、`timestamp`）。
2. **工具名归一**：入库一律小写 slug（`cursor` / `workbuddy` / `claude_code` / `codex` / `continue` / …）；UI 筛选用友好名（Cursor / WorkBuddy / Claude Code / Codex / Continue）。
3. **幂等去重**：`(source, request_id)` 唯一；重复 ingest **不叠量**（`accepted=0` / `skipped` 增加）。
4. **seed 与真流量分源**：演示数据 `source=seed`，排行默认 **隐藏**（`include_seed=true` 才看）；`SEED_ON_START` 默认 `0`。
5. **可按项目拆开**：同一账号跨多个 `project_id`；见 `GET /api/v1/rankings/projects`。
5b. **可按 Agent 拆开**：同一人的 agent 团队里每个 `agent_id` 单独统计；见 `GET /api/v1/rankings/agents?account_id=`。
6. **采集不存对话原文**：WorkBuddy / Claude Code / Codex CLI / Continue 适配器只抽 usage；`content` / `prompt` / `completion` / `response_item` 对话正文不入库。
7. **写接口鉴权**：`TOKEN_RANKINGS_API_KEY` 保护 ingest / sync / scan / admin / seed。
7b. **轻量 RBAC**：`USERS_FILE` / `USERS_JSON` 映射用户 API Key → `admin`（全站）或 `member`（仅自己的 `account_id`）。带 member Key 访问排行 /me / 导出时强制过滤；`RANKINGS_REQUIRE_AUTH=1` 时排行 GET 也必须带 Key。
7c. **导出审计**：`GET /api/v1/export/usage.csv|.json` 需鉴权，写入 `audit_log`；`GET /api/v1/admin/audit` 仅 admin。
7d. **数据库**：默认 SQLite（一键 demo）；生产设 `DATABASE_URL=postgresql://…` 真切 Postgres（`usage_events` / `audit_log` / 幂等唯一索引齐全）。
8. **成本口径可见**：`cost_basis` 区分 `estimate` / `cursor_charged` / `litellm_spend` / `invoice`；UI 徽章标注，混合时标明「含估算」。
9. **未标注可隐藏**：无 `project_id` / `agent_id` 归「未标注」，排行默认 `hide_unlabeled=true`。
10. **个人自助**：`/me?account_id=` 看自己的 Agent / 项目 / 工具分布，可复制周报文案或生成分享图。
11. **浏览器贴用户 Key**：`localStorage` 存 member/admin Key；请求自动带 `Authorization`；提供清除；**不要**把 `TOKEN_RANKINGS_API_KEY` 写进前端默认。
12. **低消耗提醒**：`GET /api/v1/alerts/low-usage`；窗口内 tokens < 阈值则告警；叙事：消耗过低可能说明 AI 工作流尚未真正重构。可选 `LOW_USAGE_WEBHOOK_URL` + `notify=true`。
13. **交流区（最小）**：`/forum` 发帖需用户 Key；匿名只读；点赞；禁止密钥；非全功能社区。
14. **真机探测**：见 [`docs/probe.md`](docs/probe.md)（亦可通过 `/docs/probe.md` 读取）；不要求本环境真装 Cursor / WorkBuddy。

## 快速开始

```bash
cd /workspace/token-rankings
docker compose up -d --build
# compose 默认开发用 key（务必在共享环境改掉）
export TOKEN_RANKINGS_API_KEY=dev-token-rankings-change-me
# 灌真实对照 fixture（非 seed）——写接口必须带 key
curl -s -X POST http://localhost:8000/api/v1/ingest/litellm \
  -H "Authorization: Bearer $TOKEN_RANKINGS_API_KEY" \
  -H 'Content-Type: application/json' -d @fixtures/litellm_spend_logs.json
curl -s -X POST http://localhost:8000/api/v1/ingest/cursor \
  -H "X-API-Key: $TOKEN_RANKINGS_API_KEY" \
  -H 'Content-Type: application/json' -d @fixtures/cursor_usage_events.json
curl -s -X POST http://localhost:8000/api/v1/ingest/workbuddy \
  -H "Authorization: Bearer $TOKEN_RANKINGS_API_KEY" \
  -H 'Content-Type: application/json' -d @fixtures/workbuddy_events.json
curl -s -X POST http://localhost:8000/api/v1/ingest/claude_code \
  -H "Authorization: Bearer $TOKEN_RANKINGS_API_KEY" \
  -H 'Content-Type: application/json' -d @fixtures/claude_code_events.json
curl -s -X POST http://localhost:8000/api/v1/ingest/codex \
  -H "Authorization: Bearer $TOKEN_RANKINGS_API_KEY" \
  -H 'Content-Type: application/json' -d @fixtures/codex_events.json
curl -s -X POST http://localhost:8000/api/v1/ingest/continue \
  -H "Authorization: Bearer $TOKEN_RANKINGS_API_KEY" \
  -H 'Content-Type: application/json' -d @fixtures/continue_events.json
curl -s -X POST http://localhost:8000/api/v1/ingest/litellm \
  -H "Authorization: Bearer $TOKEN_RANKINGS_API_KEY" \
  -H 'Content-Type: application/json' -d @fixtures/agent_team_spend_logs.json
```

- 个人榜：<http://localhost:8000/people>
- 组织榜：<http://localhost:8000/orgs>
- 项目榜：<http://localhost:8000/projects>
- Agent 榜：<http://localhost:8000/agents>（例：`/agents?account_id=acme-prod`）
- **我的**：<http://localhost:8000/me>（例：`/me?account_id=acme-prod`）
- **提醒**：<http://localhost:8000/alerts>
- **交流区**：<http://localhost:8000/forum>
- 真机探测说明：<http://localhost:8000/docs/probe.md>
- API 文档：<http://localhost:8000/docs>
- 健康 / 可信度：<http://localhost:8000/health>


### 交流区（最小闭环）

```bash
# 匿名列表
curl -s http://localhost:8000/api/v1/forum/posts | head
# 发帖（用户 Key，非 ingest Key）
curl -s -X POST http://localhost:8000/api/v1/forum/posts \
  -H "Authorization: Bearer demo-alice-key" \
  -H 'Content-Type: application/json' \
  -d '{"title":"本周 Agent 消耗分享","body":"implementer 占了一半，准备拆并发。"}'
# 点赞
curl -s -X POST http://localhost:8000/api/v1/forum/posts/1/like
```

已知限制：无评论楼中楼、无编辑删除、无附件、无全文搜索；点赞可重复；不做公网热度。

需要本地 demo 人设数据时：

```bash
curl -s -X POST 'http://localhost:8000/api/v1/seed?force=true' \
  -H "Authorization: Bearer $TOKEN_RANKINGS_API_KEY"
# 排行仍默认不展示 seed；要看加 &include_seed=true
```




## 浏览器贴用户 Key

顶栏 **贴 Key** → 输入 `demo-alice-key` / `demo-admin-key` 等 **用户** Key（见 `fixtures/users.json`）。

- 仅存本机 `localStorage`（键名 `token_rankings_user_api_key`）
- 排行、`/me`、导出、提醒请求自动带 `Authorization: Bearer …`
- Member 自动只看自己的 `account_id`；清除按钮可删掉本地 Key
- **禁止**把服务端 `TOKEN_RANKINGS_API_KEY`（ingest 用）写进前端默认值或提交到仓库前端代码

## 低消耗提醒

```bash
# 公开/admin：评估全员；带 member Key：只看自己
curl -s 'http://localhost:8000/api/v1/alerts/low-usage?window=7d&threshold_tokens=10000' \
  -H "Authorization: Bearer demo-alice-key" | jq .
# 若配置了 LOW_USAGE_WEBHOOK_URL，加 &notify=true 才会 POST；未配置则跳过
```

页面：`/alerts`。文案强调建设性复盘，而非羞辱排名。

## 真机探测

上机核对 Cursor Admin / WorkBuddy 本地 jsonl 的路径与字段，对照 `fixtures/`，见 **[docs/probe.md](docs/probe.md)**。

## 写接口 API Key

环境变量 **`TOKEN_RANKINGS_API_KEY`**（`docker-compose.yml` 已带开发用示例 `dev-token-rankings-change-me`）：

| 状态 | 写接口（ingest / sync / scan / admin/clear / seed） | 排行 GET / 静态页 |
|---|---|---|
| 未配置 | **503** + `api_key_not_configured` | 可用（除非 `RANKINGS_REQUIRE_AUTH=1`） |
| 错误或缺失 Header | **401** | 匿名可用；带错 Key → **401** |
| `Authorization: Bearer <key>` 或 `X-API-Key: <key>` | 正常（service key；admin 用户 Key 也可写） | 按角色作用域过滤 |

```bash
# 错误 key → 401
curl -s -o /tmp/out.json -w '%{http_code}\n' -X POST http://localhost:8000/api/v1/usage \
  -H 'Authorization: Bearer wrong-key' -H 'Content-Type: application/json' \
  -d '{"model":"gpt-4o-mini","prompt_tokens":1,"completion_tokens":1}'

# 正确 key → 200
curl -s -o /tmp/out.json -w '%{http_code}\n' -X POST http://localhost:8000/api/v1/usage \
  -H "Authorization: Bearer $TOKEN_RANKINGS_API_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"gpt-4o-mini","prompt_tokens":1,"completion_tokens":1,"request_id":"demo-1"}'
```

脚本 `scripts/scan_workbuddy.py` / `scripts/scan_claude_code.py` / `scripts/scan_codex.py` / `scripts/scan_continue.py` / `scripts/sync_litellm.py` 会读取同一环境变量并自动带 Header。

## 轻量 RBAC（admin / member）

配置文件示例：`fixtures/users.json`（compose 默认 `USERS_FILE=/app/fixtures/users.json`）。

| Key（演示） | 角色 | account_id | 可见范围 |
|---|---|---|---|
| `demo-admin-key` | admin | — | 全站 |
| `demo-alice-key` | member | `acme-prod` | 仅 acme-prod |
| `demo-dave-key` | member | `dave@acme.com` | 仅 dave@acme.com |
| `TOKEN_RANKINGS_API_KEY` | admin（service） | — | 全站 + 写接口 |

```bash
# member 只能看到自己的账号；要别人的 account_id → 403
curl -s -H 'Authorization: Bearer demo-alice-key' \
  'http://localhost:8000/api/v1/rankings/projects?window=all' | jq '.items[].account_id' | sort -u
# → "acme-prod"

curl -s -o /tmp/x.json -w '%{http_code}\n' -H 'Authorization: Bearer demo-alice-key' \
  'http://localhost:8000/api/v1/rankings/projects?account_id=dave@acme.com'
# → 403

# 导出需鉴权，并写审计
curl -s -H 'Authorization: Bearer demo-alice-key' \
  'http://localhost:8000/api/v1/export/usage.csv?window=7d' -o /tmp/alice.csv

# 仅 admin 可查审计
curl -s -H 'Authorization: Bearer demo-admin-key' \
  'http://localhost:8000/api/v1/admin/audit?limit=20' | jq .

# 一键验证（docker 起来后）
bash scripts/verify_rbac.sh
```

生产必须：`RANKINGS_REQUIRE_AUTH=1`，并改掉全部演示 Key（`.env` 的 `TOKEN_RANKINGS_API_KEY` 与 `fixtures/users.json`）。网页「首次配置」只把**用户** Key 存在本机浏览器；仍在使用演示 Key 时顶部会出黄条。匿名看全量只适合本机试用。详见 [docs/SECURITY.md](docs/SECURITY.md)。

## 数据库（SQLite demo / Postgres 生产）

| 模式 | 怎么开 | `/health.database.backend` |
|---|---|---|
| **SQLite（默认）** | `docker compose up -d --build` | `sqlite` |
| **Postgres（生产建议）** | `docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d --build` | `postgres` |

- 表：`usage_events` + `audit_log`；幂等唯一索引 `(source, request_id)`（`request_id` 非空）。
- 未设置 `DATABASE_URL` → SQLite（`DATA_DIR/rankings.db`），一键体验不变。
- 设置 `DATABASE_URL=postgresql://user:pass@host:5432/dbname`（或 `postgres://`）→ 启动时 DDL 分支建表/索引，读写走 Postgres。
- 驱动：`psycopg[binary]`；业务 SQL 仍用 `?` 占位，层内适配。

```bash
# 默认 SQLite（一键体验，勿改坏）
docker compose up -d --build
curl -s http://localhost:8000/health | jq '.database'
# → backend=sqlite

# Postgres 叠加（生产建议；不改坏默认 compose）
# 必填 POSTGRES_PASSWORD。不要用 token 当密码。5432 只绑定 127.0.0.1。
export POSTGRES_PASSWORD='换成你们自己的长随机密码'
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d --build
curl -s http://localhost:8000/health | jq '.database'
# → backend=postgres, connected=true
bash scripts/verify_postgres.sh   # ingest 幂等 + 排行 + RBAC

# 也可只起库、进程外挂 DATABASE_URL（密码与 POSTGRES_PASSWORD 相同，不要用 token）：
# docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d postgres
# DATABASE_URL=postgresql://token:你的密码@127.0.0.1:5432/token_rankings uvicorn app.main:app --port 8000
```

生产建议：**用 Postgres**（并发、备份、多副本）；SQLite 仅适合单机 demo / 轻量试用。备份卷 `rankings-pg` 或 `pg_dump`。overlay 只把 Postgres 映射到宿主机 `127.0.0.1:5432`。

## 幂等 / 清库

```bash
# 同一 fixture 再推一次 → accepted=0，skipped>0
curl -s -X POST http://localhost:8000/api/v1/ingest/litellm \
  -H "Authorization: Bearer $TOKEN_RANKINGS_API_KEY" \
  -H 'Content-Type: application/json' -d @fixtures/litellm_spend_logs.json

# 只删演示 seed
curl -s -X POST 'http://localhost:8000/api/v1/admin/clear?scope=seed' \
  -H "Authorization: Bearer $TOKEN_RANKINGS_API_KEY"
# 或清空全部
curl -s -X POST 'http://localhost:8000/api/v1/admin/clear?scope=all' \
  -H "X-API-Key: $TOKEN_RANKINGS_API_KEY"
# 容器外脚本：python scripts/clear_db.py seed|all
```

## 对接 LiteLLM

旁路推送（推荐）或配置 `LITELLM_BASE_URL` / `LITELLM_MASTER_KEY` 后 `POST /api/v1/sync/litellm`。  
tag `tool:Cursor` → 归一为 `cursor`；`request_id` 用于去重。

### 带上 agent_id（Agent 榜）

在 LiteLLM `request_tags` 或 `metadata` 里打上 agent 身份，排行会按 agent 拆开：

```bash
# tags 写法（推荐）
request_tags: ["tool:Cursor", "project:agent-lab", "agent_id:agent-planner", "agent_name:规划官"]

# 或 metadata
metadata: { "agent_id": "agent-planner", "agent_name": "规划官" }

# 也认：agent: / bot_id: / automationId: / cloudAgentId:
```

```bash
curl -s -X POST http://localhost:8000/api/v1/ingest/litellm \
  -H "Authorization: Bearer $TOKEN_RANKINGS_API_KEY" \
  -H 'Content-Type: application/json' -d @fixtures/agent_team_spend_logs.json
curl -s 'http://localhost:8000/api/v1/rankings/agents?window=7d&account_id=acme-prod' | jq .
curl -s 'http://localhost:8000/api/v1/me?account_id=acme-prod&window=7d' | jq '.overview,.share_text'
```

个人页 `/me` 会提示如何带 API Key 上报 `agent_id`（LiteLLM tags / Cursor `cloudAgentId` / `POST /api/v1/usage`）。

## 对接 Cursor Admin

`POST /api/v1/ingest/cursor`（fixture）或 `CURSOR_ADMIN_API_KEY` + `POST /api/v1/sync/cursor`。  
`chargedCents` → `cost_usd`（÷100），`cost_basis=cursor_charged`；无 `request_id` 时用合成键去重。

Cloud Agent 字段 **`cloudAgentId`**（或 `agent_id` / `bot_id` / `automationId`）→ 入库 `agent_id`；`agentName` / `agent_name` → `agent_name`。这样个人可在 Agent 榜按账号过滤，看每个 Cloud Agent 烧多少。

## 对接 WorkBuddy

`POST /api/v1/ingest/workbuddy` 或扫描 `WORKBUDDY_PROJECTS_DIR`；`tool=workbuddy`，不落对话原文。

## 对接 Claude Code（本地 jsonl）

Claude Code 把会话写在 `~/.claude/projects/<sanitized-cwd>/<session>.jsonl`。只解析 **`type=assistant` + `message.usage`**（`input_tokens` / `output_tokens` / `cache_read_input_tokens` / `cache_creation_input_tokens`）。

- **去重键**：`request_id = message.id`（同一 API 调用会写多行 jsonl，流式块共享同一 `message.id`；适配器保留 `output_tokens` 最大的那条）
- **`tool=claude_code`**；`cwd` → `project_id`（目录名）
- **禁止入库对话正文**（`content` 在映射前剥离）
- 可选 `CLAUDE_ACCOUNT_ID` 给本地扫描补账号

```bash
# 推送 fixture（含同 message.id 双行 → 入库 1 条）
curl -s -X POST http://localhost:8000/api/v1/ingest/claude_code \
  -H "Authorization: Bearer $TOKEN_RANKINGS_API_KEY" \
  -H 'Content-Type: application/json' -d @fixtures/claude_code_events.json
# 再推一次 → accepted=0，skipped>0
curl -s -X POST http://localhost:8000/api/v1/ingest/claude_code \
  -H "Authorization: Bearer $TOKEN_RANKINGS_API_KEY" \
  -H 'Content-Type: application/json' -d @fixtures/claude_code_events.json

# 扫描本地目录（示例用仓库内 fixture）
curl -s -X POST 'http://localhost:8000/api/v1/scan/claude_code?root=/app/fixtures/claude_code_sample' \
  -H "Authorization: Bearer $TOKEN_RANKINGS_API_KEY"
# 或：CLAUDE_PROJECTS_DIR=./fixtures/claude_code_sample python scripts/scan_claude_code.py
```


## 对接 Codex CLI（本地 rollout jsonl）

Codex CLI 把会话写在 `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`（可用 `CODEX_SESSIONS_DIR` 覆盖）。

- **优先** `type=token_usage_record`（带 `response_id`）；同文件若再出现 `event_msg/token_count` **忽略**，避免双计。仅当整文件没有 record 时，才回退 `token_count.last_token_usage`（旧日志）。
- **去重键**：`request_id = response_id`（会话 resume 会把旧 response 重写进新 rollout，跨文件去重）。
- **缓存拆分**：`cached_input_tokens` **已含在** `input_tokens` 内。入库时 `prompt_tokens = input - cached`，`cache_read_tokens = cached`；**切勿**把两者相加。
- **`tool=codex`**；`session_meta.cwd` → `project_id`（目录名）
- **禁止入库对话正文**（`response_item` / content 不解析入库）
- 可选 `CODEX_ACCOUNT_ID` 给本地扫描补账号

```bash
# 推送 fixture（token_usage_record + 同量 token_count → 只入 record）
curl -s -X POST http://localhost:8000/api/v1/ingest/codex \
  -H "Authorization: Bearer $TOKEN_RANKINGS_API_KEY" \
  -H 'Content-Type: application/json' -d @fixtures/codex_events.json
# 再推一次 → accepted=0，skipped>0
curl -s -X POST http://localhost:8000/api/v1/ingest/codex \
  -H "Authorization: Bearer $TOKEN_RANKINGS_API_KEY" \
  -H 'Content-Type: application/json' -d @fixtures/codex_events.json

# 扫描本地目录（示例用仓库内 fixture）
curl -s -X POST 'http://localhost:8000/api/v1/scan/codex?root=/app/fixtures/codex_sample' \
  -H "Authorization: Bearer $TOKEN_RANKINGS_API_KEY"
# 或：CODEX_SESSIONS_DIR=./fixtures/codex_sample python scripts/scan_codex.py
```


## 对接 Continue（tokensGenerated）

Continue 默认把开发数据写在 `~/.continue/dev_data/{schema}/tokensGenerated.jsonl`（可用 `CONTINUE_DATA_DIR` 覆盖）。也可在 `~/.continue/config.yaml` 配 HTTP destination，直推本服务。

- **事件**：只吃 `tokensGenerated`（`model` / `provider` / `promptTokens` / `generatedTokens` / `timestamp`）；`autocomplete` 等其它事件跳过
- **去重键**：显式 `request_id`，否则对 `(timestamp, userId, model, provider, tokens, profile)` 做稳定 hash → `continue:<sha>`
- **`tool=continue`**；`userId` → `account_id`；`selectedProfileId` → `project_id`（可被显式 `project_id` 覆盖）；可选 `agent_id` / `agent_name`
- **禁止入库** prompt / completion / code 正文（映射前剥离）
- 可选 `CONTINUE_ACCOUNT_ID` 给本地扫描补账号

### config.yaml HTTP destination 示例

把 Continue 的 usage **直接 POST** 到本服务（`apiKey` 即 `TOKEN_RANKINGS_API_KEY`）：

```yaml
name: Local Continue
version: 1.0.0
schema: v1
data:
  - name: Token Rankings
    destination: http://localhost:8000/api/v1/ingest/continue
    schema: "0.2.0"
    level: noCode
    apiKey: dev-token-rankings-change-me
    events:
      - tokensGenerated
```

Continue 会 POST 信封 `{ "name": "tokensGenerated", "data": { model, provider, promptTokens, generatedTokens, timestamp, userId, ... }, "schema": "0.2.0", "level": "noCode", "profileId": "..." }`；本接口原样接受。

```bash
# 推送 fixture（HTTP 信封 + 扁平行；同 request_id 双行 → 入库 1 条；autocomplete 跳过）
curl -s -X POST http://localhost:8000/api/v1/ingest/continue \
  -H "Authorization: Bearer $TOKEN_RANKINGS_API_KEY" \
  -H 'Content-Type: application/json' -d @fixtures/continue_events.json
# 再推一次 → accepted=0，skipped>0
curl -s -X POST http://localhost:8000/api/v1/ingest/continue \
  -H "Authorization: Bearer $TOKEN_RANKINGS_API_KEY" \
  -H 'Content-Type: application/json' -d @fixtures/continue_events.json

# 扫描本地目录（示例用仓库内 fixture）
curl -s -X POST 'http://localhost:8000/api/v1/scan/continue?root=/app/fixtures/continue_sample' \
  -H "Authorization: Bearer $TOKEN_RANKINGS_API_KEY"
# 或：CONTINUE_DATA_DIR=./fixtures/continue_sample python scripts/scan_continue.py
```

## 排行 API

```bash
curl -s 'http://localhost:8000/api/v1/rankings/people?window=today&tool=cursor' | jq .
curl -s 'http://localhost:8000/api/v1/rankings/orgs?window=7d&tool=workbuddy' | jq .
curl -s 'http://localhost:8000/api/v1/rankings/projects?window=7d&hide_unlabeled=true' | jq .
curl -s 'http://localhost:8000/api/v1/rankings/agents?window=7d&account_id=acme-prod' | jq .
curl -s 'http://localhost:8000/api/v1/me?account_id=acme-prod&window=7d' | jq .
curl -s -H 'Authorization: Bearer demo-alice-key' \
  'http://localhost:8000/api/v1/export/usage.csv?window=7d' -o /tmp/alice.csv
curl -s 'http://localhost:8000/health' | jq '{version,rbac,database,tools_connected}'
# 查看含 seed：&include_seed=true；显示未标注：&hide_unlabeled=false
```

`window`：`today` | `yesterday` | `7d` | `30d` | `90d` | `all`

## 环境变量

| 变量 | 默认 | 用途 |
|---|---|---|
| `TOKEN_RANKINGS_API_KEY` | compose 示例 `dev-token-rankings-change-me` | 写接口鉴权（service admin）；未配置则写接口 503 |
| `USERS_FILE` / `USERS_JSON` | compose: `/app/fixtures/users.json` | 用户 Key → role + account_id |
| `RANKINGS_REQUIRE_AUTH` | `0`（试用） | **生产设为 `1`**：排行 GET 也必须带用户 Key |
| `DATABASE_URL` | —（SQLite） | `postgresql://…` 时真切 Postgres；见 `docker-compose.postgres.yml` |
| `SEED_ON_START` | `0` | 启动灌 demo（`source=seed`） |
| `LITELLM_BASE_URL` / `LITELLM_MASTER_KEY` | — | LiteLLM sync |
| `CURSOR_ADMIN_API_KEY` / `CURSOR_API_BASE` | — | Cursor Admin sync |
| `WORKBUDDY_PROJECTS_DIR` | — | WorkBuddy jsonl 根目录 |
| `CLAUDE_PROJECTS_DIR` | `~/.claude/projects` | Claude Code jsonl 根目录 |
| `CLAUDE_ACCOUNT_ID` | — | 本地扫描时补默认账号 |
| `CODEX_SESSIONS_DIR` | `~/.codex/sessions` | Codex CLI rollout jsonl 根目录 |
| `CODEX_ACCOUNT_ID` | — | 本地扫描时补默认账号 |
| `CONTINUE_DATA_DIR` | `~/.continue/dev_data` | Continue tokensGenerated.jsonl 根目录 |
| `CONTINUE_ACCOUNT_ID` | — | 本地扫描时补默认账号 |

## 项目结构

```
app/main.py                 # FastAPI + rankings + ingest/sync/admin + /me + export
app/credibility.py           # cost_basis 标签、健康可信度指标
app/auth.py                 # 写 Key + 轻量 RBAC（admin/member）
app/tools.py                # 工具 slug 归一 + 友好名
app/ingest.py               # LiteLLM / OpenAI usage 归一
app/adapters/cursor_admin.py
app/adapters/workbuddy.py
app/adapters/claude_code.py
app/adapters/codex.py
app/adapters/continue_dev.py
app/db.py                   # SQLite / Postgres 双后端 + audit_log + 幂等索引
app/seed.py                 # source=seed 演示数据
scripts/clear_db.py
scripts/customer_smoke.sh   # 客户验收：health + 一份 fixture + 排行，失败则非 0
scripts/verify_rbac.sh      # member 隔离 + 导出审计自检
scripts/verify_postgres.sh  # Postgres 路径：ingest + 排行 + RBAC
fixtures/users.json         # 演示 admin/alice/dave
static/index.html
docker-compose.yml          # 默认 SQLite 一键体验
docker-compose.postgres.yml # 可选 Postgres 叠加
```
