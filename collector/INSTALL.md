# 本机采集器

许可证：**MIT**。服务跑起来之后，这份说明也可以从 `/docs/collector.md` 读到。给装了 AI 编程工具的开发机用。它定期读本机用量日志，只把 **Token 数字** 报到你们自己的 Token 消耗排行榜，让这个人出现在私有榜上。

这不是公网热度榜，也不是模型网关。本机采集覆盖 WorkBuddy、Claude Code、Codex、Continue、Cursor、ProCode（含 Canoryn 目录）。Cursor 的服务端 Admin API 仍然可以单独用。采集器不读浏览器 cookie，也不把管理员密钥写进配置。

## 会上报什么

每条用量只有这些字段：

| 字段 | 说明 |
|---|---|
| `model` | 日志里的模型名。对得上价目表时收成规范名 |
| `prompt_tokens` / `completion_tokens` / `total_tokens` | Token 计数 |
| `cache_read_tokens` / `cache_write_tokens` / `reasoning_tokens` | 适配器能解析到才有 |
| `account_id` / `participant` / `team` | 账号、显示名、部门 |
| `project_id` | **目录名**（例如 `chatbot-core`），不是完整路径 |
| `agent_id` / `agent_name` | 日志里如果有 |
| `request_id` / `timestamp` | 用来去重、排序 |
| `tool` / `source` | `workbuddy` / `claude_code` / `codex` / `continue` / `cursor` / `procode` |

**不会**上报：提示词、回复、工具参数、源代码、源文件名、截图、Cookie、访问令牌。含大段 `content` / `prompt` / `messages` 的行整行跳过。预览里的目录会把家目录收成 `~/...`，不打印家目录全路径。

费用是公开列表价**估算**（`cost_basis=estimate`），不是发票，也不是云账单实扣。「我的」按输入、输出、缓存读、缓存写计算。榜上默认只按输入和输出，可以打开「含缓存」。不认识的模型用 default 档，页面标「估算」。

## 模型支持是什么意思

- **入库**：只要日志里有模型名和 Token，就会上报。不认识的名字原样保留，不会改成 `unknown`，也不会丢掉。
- **计价**：`pricing.json` 给国内外主流模型做了列表价估算，包括 OpenAI、Anthropic、Google、Meta、Mistral、xAI、Cohere，以及 DeepSeek、通义千问、月之暗面 / Kimi、智谱 GLM、豆包、百川、MiniMax、零一万物。
- **对不上价目表**：事件照样计数，估价用 `default` 档（每百万 Token 输入 1 美元、输出 3 美元）。页面上仍标成估算，不是发票。

## 准备

需要 Python 3.11+，在仓库根目录执行。排行榜先按 [docs/INSTALL.md](../docs/INSTALL.md) 跑起来。

```bash
export RANKINGS_URL=http://127.0.0.1:8000
export TOKEN_RANKINGS_API_KEY=你的写入密钥
```

`TOKEN_RANKINGS_API_KEY` 是服务端写入密钥，不是网页「贴 Key」里的用户密钥。

可选：让日志里没有公司账号的机器，统一归到一个人。

```bash
export COLLECTOR_ACCOUNT_ID=alice@acme.com
export COLLECTOR_PARTICIPANT=爱丽丝
```

配置了 `account_id` 之后，会上报这个账号，覆盖日志里的邮箱。`identity_map` 可以按日志里的邮箱分别映射，写在配置文件里（见下）。

## 命令

```bash
# 写下配置（默认不把密钥写进 json）
python -m collector install \
  --url http://127.0.0.1:8000 \
  --account-id alice@acme.com \
  --participant 爱丽丝 \
  --interval 30

# 密钥单独放在 ~/.config/token-rankings/collector.env（权限 0600）
python -m collector install --save-key

# 只看将要上报的数字，不发送
python -m collector preview

# 用仓库里的样例，不读你的家目录
python -m collector preview --fixtures --json

# 扫描并上报
python -m collector once

# 先把预览存下来，核对后再发
python -m collector preview --out /tmp/collector-preview.json
python -m collector upload --file /tmp/collector-preview.json

# 和 preview 一样，不发送
python -m collector once --dry-run

# 检查连接：未上报 / 密钥无效 / 时钟偏差 / 无本地日志，并列出检测到和未检测到的工具
python -m collector diagnose
```

## 从网页接入

排行榜打开后，浏览器访问 `/onboard`（如何接入）。

1. 先在页面上粘贴**用户密钥**（不是上面的写入密钥）。
2. 选择 macOS / Linux 或 Windows，复制命令。命令里是**个人上报密钥**，只能写入你自己的账号。
3. 页面上有红字：**勿转发**。这串密钥不要发到群里。
4. 在仓库根目录运行命令。它会先 `preview` 再 `once`。
5. 回到网页点「再检查连接」。

服务器上的 `TOKEN_RANKINGS_API_KEY` 仍然给管理员批量导入。个人上报密钥不能清库，也不能在服务器上扫描目录。

不想自己敲命令时，接入页有一段可以复制给 Cursor / Claude 的提示词。那一段只让它运行采集命令。采集器只上报 Token 数字。

默认每 30 分钟一轮。只扫一种工具：`--tool workbuddy`（`workbody` 同样认）、`--tool cursor`、`--tool procode`（`canoryn` 同样认）。

## 它会读哪些目录

没在配置或环境变量里指定时，存在才扫：

| 工具 | 默认目录 |
|---|---|
| WorkBuddy | `~/.workbuddy/projects`、`~/.workbody/projects`、`~/Library/Application Support/WorkBuddy/projects` |
| Claude Code | `~/.claude/projects` |
| Codex | `~/.codex/sessions` |
| Continue | `~/.continue/dev_data` |
| Cursor | 见下表。存在才读 |
| ProCode / Canoryn | 见下表。存在才读 |

环境变量 `WORKBUDDY_PROJECTS_DIR`、`CLAUDE_PROJECTS_DIR`、`CODEX_SESSIONS_DIR`、`CONTINUE_DATA_DIR`、`CURSOR_DATA_DIR`、`PROCODE_DATA_DIR` 会盖过该工具的默认位置（只扫你指定的那一个目录）。

### Cursor（Linux / macOS / Windows）

目录在，就会扫。没有单独的开关。

| 系统 | 默认目录 |
|---|---|
| Linux | `~/.cursor`、`~/.config/Cursor`。若设置了 `XDG_CONFIG_HOME`，也看 `$XDG_CONFIG_HOME/Cursor` |
| macOS | 上面两个，再加 `~/Library/Application Support/Cursor` |
| Windows | 用户目录下的 `.cursor`，再加上 `%APPDATA%\Cursor`、`%LOCALAPPDATA%\Cursor` |

读哪两类文件：

1. `User/globalStorage/state.vscdb`。表 `cursorDiskKV`，键是 `bubbleId:*`。只用 SQL 取出 token 数和模型名。不读对话正文，不读 `cursorAuth` 里的 access token 或 cookie。优先 `globalStorage`，避免和工作区副本叠在一起。
2. 文件名里带 `usage` 的 `.json` / `.jsonl`，以及 `exports/` 目录里的用量 JSON。仪表盘导出的 `filtered-usage-events.json` 放到 `~/.cursor/exports/` 即可。不要把 prompt 放进这个文件。

本地气泡的 input 和 output 如果都是 0（当前 Cursor 经常这样），这一行会跳过，不会用字数冒充 token。这时用上面的导出 JSON。`CURSOR_ADMIN_API_KEY` 只放在跑排行榜的那台机器上，不要写进 `collector.json`。

两边如果都有同一段用量，request_id 并不相同，可能会各记一笔。平时留一种来源就好：要么本机库里的非 0 计数，要么 `exports/` 里的导出。

导出里的 `userEmail` 会拆成不同账号。本机库通常只有当前登录邮箱。不同对话会拆成不同 `project_id`（对话 id 的前缀，或导出里的项目目录名，不保留完整路径）。

换目录：`export CURSOR_DATA_DIR=/你的目录`，或在配置里写 `roots.cursor`（字符串数组）。

### ProCode / Canoryn（Linux / macOS / Windows）

公开文档没有本机用量接口。Canoryn 是 macOS 应用：聊天和记忆在加密 SQLite，密钥在系统钥匙串，`canoryn` 命令会连到正在运行的 App（必要时还会把它拉起来）。采集器**不解密、不打开 sqlite / db / cryn、不读钥匙串、不启动 canoryn**。健康检查里的状态保持 **`placeholder`**，直到有公开 schema。

今天能收的只有 usage 形状的 `.json` / `.jsonl`：要有模型名和 token 数。字段与 `POST /api/v1/ingest/procode` 相同，也认驼峰写法（`inputTokens`、`outputTokens`、`userEmail`、`usageEvents`、`token-usage.json`）。带大段 `prompt` / `content` / `messages` 的行整行跳过。`project`、`projectPath`、`cwd` 如果是完整路径，只留最后一级目录名。未知模型保留原名，估价用 `default` 档，并标成估算。

不会读的东西：

| 看到什么 | 怎么处理 |
|---|---|
| `*.sqlite`、`*.sqlite3`、`*.db`、`*.cryn`、`*.crynpack` | 只计数，不打开。预览里的 `unopened_db_seen` 会写个数 |
| `discussions/` | Canoryn 的评论导出，不是用量，整目录跳过 |
| `blueprints/` | 工作流 JSON，可能含提示词，整目录跳过 |
| 文件名里带 `secret`、`cookie`、`credential`、`password`、`keychain`、`api_key`、`access_token` | 不打开 |
| `README.json`、`package.json` | 不打开 |

`preview --json` 里 `tools.procode.diagnostics` 会说明读了多少文件、跳过了多少数据库、以及上面这句「不解密」。适配状态仍是 `placeholder`。

| 系统 | 默认目录 |
|---|---|
| Linux | `~/.procode`、`~/.canoryn`、`~/.config/procode`、`~/.config/canoryn`、`~/.config/ProCode`、`~/.config/Canoryn`、`~/.local/share/procode`、`~/.local/share/canoryn`（`ProCode` / `Canoryn` 大小写也认）。设置了 `XDG_CONFIG_HOME` / `XDG_DATA_HOME` 时，也看对应目录。这些是**放导出文件**的位置，不是 Canoryn 的安装目录 |
| macOS | 上面这些，再加 `~/Library/Application Support/ProCode`、`Canoryn`，以及小写的 `procode`、`canoryn` |
| Windows | `%APPDATA%` 和 `%LOCALAPPDATA%` 下的 `ProCode`、`Canoryn`、`procode`、`canoryn`，以及用户目录下的 `.procode`、`.canoryn` |

这些目录里还没有 usage 文件时，结果就是 0 行。这是当前的正常状态，不是采集器坏了。可以把 usage JSON 落到其中任一目录，或继续直接 POST。这不是一条新的模型网关。

换目录：`export PROCODE_DATA_DIR=/你的目录`，或配置 `roots.procode`。

样例：`fixtures/procode_sample/`（含 `exports/token-usage.json`、被跳过的 `canoryn.sqlite`、`discussions/`、`blueprints/`）。假家目录：`fixtures/probe_home/`。说明见 [docs/probe.md](../docs/probe.md)。

### 预览里不会出现什么

`preview` 把家目录收成 `~/...`。输出里没有提示词、密钥、Cookie、源文件名。不认识的模型名原样保留，费用按 `default` 档估算，并写明这是估算，不是发票。同一条用量再跑一次 `once` 不会叠量。

配置文件：`~/.config/token-rankings/collector.json`（Windows 在 `%APPDATA%\token-rankings\`）。`identity_map` 示例：

```json
{
  "account_id": "acme-prod",
  "participant": "未映射",
  "identity_map": {
    "alice@acme.com": {"account_id": "acme-prod", "participant": "爱丽丝"},
    "bob@acme.com": {"account_id": "acme-prod", "participant": "鲍勃", "team": "平台研发"}
  }
}
```

对得上的行用映射；其余行用全局 `account_id`。

## 定时

间隔默认 30 分钟。下面都会调用 `python -m collector once`。

### Linux（systemd 用户定时器）

```bash
python -m collector service --os linux
systemctl --user daemon-reload
systemctl --user enable --now token-rankings-collector.timer
systemctl --user status token-rankings-collector.timer
```

没有图形会话时，用户定时器可能不起。可改用下面的 cron。确认要自动启用时才加 `--enable`。

### macOS（launchd）

```bash
python -m collector service --os macos
launchctl load -w ~/Library/LaunchAgents/com.tokenrankings.collector.plist
```

### 可移植 cron

不依赖 systemd / launchd。先看将写入的那一行：

```bash
python -m collector service --os cron --print
```

确认路径之后：

```bash
python -m collector service --os cron
crontab -l
# 把 ~/.local/state/token-rankings/collector.cron 里的那一行粘进 crontab -e
```

30 分钟的形式是：

```cron
*/30 * * * * cd /path/to/token-rankings && /usr/bin/python3 -m collector once >> ~/.local/state/token-rankings/collector.log 2>&1
```

密钥放在环境里，或事先 `install --save-key`。cron 的环境很干净，推荐 `--save-key`，让进程自己读 `collector.env`。

### Windows

```bash
python -m collector service --os windows
```

会写出 `register-task.ps1`，里面有 `schtasks /Create /SC MINUTE /MO 30`。在本机核对 Python 路径后再执行。

## 真机采集验收

给 Mateo 在一台装了开发工具的电脑上打勾。完整清单和失败对照表也在 [docs/ACCEPTANCE.md](../docs/ACCEPTANCE.md) 的「真机采集验收」（服务上是 `/docs/ACCEPTANCE.md`）。下面是同一组命令。

排行榜可以跑在这台电脑上，也可以是内网另一台。不要把写入密钥贴进网页。

### 1. 排行榜

```bash
docker compose up -d --build
export RANKINGS_URL=http://127.0.0.1:8000
# 已经有服务时改成那台的地址，例如 https://tokenrank.online（示例域名）
curl -sS "$RANKINGS_URL/health"
```

- [ ] `"status"` 为 `"ok"`
- [ ] `"version"` 是 `0.23.0`
- [ ] `adapters` 里 `id` 为 `procode` 的 `"status"` 仍是 `placeholder`，`blocked` 含 `encrypted_sqlite`、`keychain`、`canoryn_cli`，`notes` 写明不解密

### 2. 写入密钥，六个工具各跑一遍

```bash
export TOKEN_RANKINGS_API_KEY=你的写入密钥
export RANKINGS_URL=http://127.0.0.1:8000
python -m collector install --url "$RANKINGS_URL" --account-id mateo@acme.com --participant Mateo

for tool in workbuddy claude_code codex continue cursor procode; do
  echo "==== $tool ===="
  python -m collector preview --tool "$tool"
  python -m collector once --tool "$tool"
done
python -m collector preview --tool canoryn --json
```

- [ ] 每个工具都跑完。`canoryn` 和 `procode` 是同一个适配器
- [ ] 本机没有该工具目录时是 0 行，这是正常的
- [ ] ProCode 那一行仍说明不解密，状态是 placeholder

### 3. 私有榜上的账号 × 项目

浏览器打开 `$RANKINGS_URL/projects`。每一行应是账号和项目，不是只有一个总数。

```bash
curl -sS -H "Authorization: Bearer 成员或管理员密钥" \
  "$RANKINGS_URL/api/v1/rankings/projects?window=30d&hide_unlabeled=true"
curl -sS -H "Authorization: Bearer 管理员密钥" \
  "$RANKINGS_URL/api/v1/rankings?window=30d&group_by=account_project"
```

- [ ] 返回里的 `totals.breakdown` 是 `account_id × project_id`
- [ ] 同一人有两个项目时分成两行
- [ ] 用成员密钥请求别人的 `account_id` 得到 403

### 4. 提醒和 FinOps 导出

```bash
curl -sS -H "Authorization: Bearer 成员或管理员密钥" \
  "$RANKINGS_URL/api/v1/alerts?window=7d"
curl -sS -H "Authorization: Bearer 成员或管理员密钥" \
  "$RANKINGS_URL/api/v1/export/usage.csv?window=30d&grain=finops" -o /tmp/finops.csv
head -1 /tmp/finops.csv
curl -sS -H "Authorization: Bearer 成员或管理员密钥" \
  "$RANKINGS_URL/api/v1/export/usage.json?window=30d&grain=finops"
```

- [ ] `/api/v1/alerts` 返回 200，成员密钥只看到自己的账号
- [ ] CSV 表头含 `account_id,project_id,model,tool`
- [ ] 未知模型仍是原名；估价落到 default 档时 `estimate` 为 `true`，`price_tier` 为 `default`

### 5. 隐私

```bash
python -m collector preview --json > /tmp/preview.json
```

- [ ] 文件里没有提示词、密钥、Cookie
- [ ] 没有家目录全路径，目录显示成 `~/...`
- [ ] ProCode 的 diagnostics 里没有钥匙串内容，也没有 sqlite 文件正文

### 6. 再跑一次 once

```bash
python -m collector once --tool cursor
python -m collector once --tool cursor
```

- [ ] 有数据时，第二次 `accepted` 为 0，`skipped` 大于 0
- [ ] 没有数据时，两次都是「没有用量可上报」，不会叠量

### 7. Cursor 本地气泡是 0/0

本机 `state.vscdb` 里的气泡如果 input 和 output 都是 0，preview 会跳过，不会用字数冒充 token。

- [ ] 把仪表盘导出的 `filtered-usage-events.json`（不要带 prompt）放到 `~/.cursor/exports/`
- [ ] 再 `python -m collector preview --tool cursor`，应出现行
- [ ] `project_id` 只是目录名或对话 id 前缀

### 8. 常见失败

| 看到什么 | 原因 | 怎么处理 |
|---|---|---|
| `缺少 TOKEN_RANKINGS_API_KEY` | 没设写入密钥 | `export TOKEN_RANKINGS_API_KEY=...`，或 `install --save-key` |
| `HTTP 401` | 密钥不对，或把网页上的用户密钥当成了写入密钥 | 用服务 `.env` 里的 `TOKEN_RANKINGS_API_KEY` |
| `连不上 http://...` | `RANKINGS_URL` 错了，或服务没起来 | 先 `curl -sS "$RANKINGS_URL/health"` |
| 0 行，且写着没找到目录 | 这台电脑没有该工具的数据目录 | 正常。ProCode 需要自己放 usage json |
| ProCode 目录在，但仍是 0 行 | 里面只有加密库、蓝图或评论，没有 usage json | 看 diagnostics 的 `unopened_db_seen`。不要解密，也不要启动 canoryn |
| preview 里出现 `/Users/...` 或 `/home/某用户` | 采集器版本过旧 | 升到 `0.23.0` |

## 用样例自测（不需要真的装 WorkBuddy）

排行榜已在本机 8000 端口时：

```bash
export RANKINGS_URL=http://127.0.0.1:8000
export TOKEN_RANKINGS_API_KEY=dev-token-rankings-change-me
python -m collector preview --fixtures
python -m collector once --fixtures
python -m collector once --fixtures
```

第一次每个工具（含 `cursor` 和 `procode`）的 `accepted` 应大于 0。第二次应为 `accepted=0` 且 `skipped` 大于 0（同一条不叠量）。预览输出里不应出现 `SECRET`、`/Users/...` 或家目录全路径。样例在 `fixtures/cursor_sample` 和 `fixtures/procode_sample`。

只跑测试、不起服务：

```bash
python -m unittest discover -s tests -v
```
