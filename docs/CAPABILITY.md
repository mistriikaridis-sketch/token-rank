# Token 消耗排行榜 · 能力总览

客户验收请用 [ACCEPTANCE.md](ACCEPTANCE.md)（当前版本见仓库 `VERSION`，**0.21.0**）。下面是能力索引，不是购买方清单。内网试点清单在 [INSTALL.md](INSTALL.md)。

面向 Mateo 的一页验收清单。代码：`/workspace/token-rankings` · 许可证 MIT · 本地：`http://localhost:8000`

## 产品定位

企业可私有部署的 **LLM Token / 成本排行榜**：多工具 usage 归一入库，按人 / 组织 / 项目 / Agent 拆开看；叙事是「消耗反映 AI 工作流是否真重构」，不是公网热度榜。

## 页面

| 路径 | 能力 |
|---|---|
| `/people` | 个人榜：主力工具/模型；每人下面按账号 × 项目拆开，可筛账号和项目 |
| `/orgs` | 组织/部门榜：总量、人均，并带账号 × 项目拆分 |
| `/projects` | 账号 × 项目拆分（不是合计） |
| `/agents` | 按账号 × agent_id 拆开，并列出项目 |
| `/me` | 个人自助：Agent/项目 Top、工具分布、周报文案/分享图、FinOps CSV |
| `/alerts` | 闲置、用量过低、费用突增、账号费用阈值（中文） |
| `/forum` | 企业内部交流区：发帖/列表/点赞；匿名只读 |
| `/procode` | ProCode 占位说明 + ingest 指引 |
| 顶栏「贴 Key」 | 用户 Key → localStorage；排行/me/export/alerts 带 Header |

## API（摘）

- Ingest：`/api/v1/usage`、`/ingest/{litellm,openai-usage,cursor,workbuddy,claude_code,codex,continue,procode}`
- Scan：`/scan/{workbuddy,claude_code,codex,continue}`（本地目录）
- Sync：`/sync/{litellm,cursor}`（未配置 → 503，不挡主站）
- 排行：`/rankings`、`/rankings/{people,orgs,projects,agents}`
- 自助：`/me`、`/whoami`、`/alerts`（试点规则）、`/alerts/low-usage`（仅低用量）
- 交流区：`/forum/posts`、`/forum/posts/{id}/like`、`/forum/seed`
- 导出：`/export/usage.csv|.json`（鉴权 + 审计）；`grain=finops` 按账号、项目、模型、工具汇总
- 管理：`/admin/clear`、`/admin/audit`、`/seed`
- 探测文档：`/docs/probe.md` · 非目标：`/docs/NON_GOALS.md`
- 本地探测脚本：`scripts/probe_local_tools.py`（假落盘 `fixtures/probe_home`）

## 采集与口径

- 工具：LiteLLM、Cursor（Admin 或本机）、WorkBuddy、Claude Code、Codex、Continue、ProCode（+ 通用 OpenAI usage）
- 归一：tool 小写 slug、`(source, request_id)` 幂等、seed 默认隐藏
- 维度：`account_id` / `project_id` / `agent_id`；未标注默认可隐藏
- 成本：`estimate` / `cursor_charged` / `litellm_spend` / 混合；UI 标明「列表价估算，不是发票」。`pricing.json` 覆盖 OpenAI / Anthropic / Google / Meta / Mistral / xAI / Cohere，以及 DeepSeek、通义千问、月之暗面 Kimi、智谱 GLM、豆包、百川、MiniMax、零一万物。未知模型名原样入库，只把估价落到 `default` 档
- 本机采集：`python -m collector`（见 [collector/INSTALL.md](../collector/INSTALL.md)，服务上是 `/docs/collector.md`）。WorkBuddy（含 Workbody）、Claude Code、Codex、Continue、Cursor、ProCode（含 Canoryn 目录）。只上传 Token 数字。Cursor 不读 cookie；ProCode 不解密聊天库
- 可信度：`/health` 含工具覆盖、agent_id 占比、近 1h 并发近似

## 安全与部署

- 写接口：`TOKEN_RANKINGS_API_KEY`（**勿**写入前端默认）
- 用户 RBAC：`USERS_FILE` → admin / member（member 强制本账号）
- 浏览器只贴 **用户 Key**（如 `demo-alice-key`）；提供清除
- DB：默认 SQLite；`DATABASE_URL` → Postgres（compose overlay）
- 试点提醒：`LOW_USAGE_THRESHOLD_TOKENS`、`ALERT_SPEND_SPIKE_RATIO`、`ALERT_SPEND_SPIKE_MIN_USD`、`ALERT_SPEND_THRESHOLD_USD`、`ALERT_ACCOUNT_THRESHOLDS`；Webhook 仍是 `LOW_USAGE_WEBHOOK_URL` + `notify=true`

## Demo 验收三步

```bash
cd /workspace/token-rankings
sudo docker compose up -d --build
# 顶栏贴 demo-alice-key → /me 应锁定 acme-prod；/alerts 仅自己
# 不贴 Key → /alerts 看全员；打开 /docs/probe.md
```

## 刻意未做（见 docs/NON_GOALS.md）

- 自研全能网关 → 对接 LiteLLM
- 公网热度榜 → 外链 OpenRouter rankings
- ProCode **深**适配（解密 Canoryn 聊天库）→ 只收 usage json/jsonl，页面 `/procode` 仍标 placeholder
- 全功能社区（楼中楼/关注）
- 用 Cursor 登录 cookie 拉仪表盘。本地气泡计数为 0 时，改放 usage 导出，或在服务端用 Admin API

（`/forum` 已是最小发帖闭环。开发者本机采集见 `collector/INSTALL.md`。）
