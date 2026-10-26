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

费用是公开列表价**估算**（`cost_basis=estimate`），不是发票，也不是云账单实扣。估算只按 prompt 与 completion；缓存 Token 会入库，但不会按缓存单价再加一遍。

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
```

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

Canoryn 的聊天在加密 SQLite 里，密钥在系统钥匙串。采集器不解密、不读钥匙串，也不调用 `canoryn` 命令（避免把 App 拉起来）。

只读 usage 形状的 `.json` / `.jsonl`（要有模型名和 token 数，字段与 `POST /api/v1/ingest/procode` 相同）。带大段 `prompt` / `content` / `messages` 的行整行跳过。`project` 如果是完整路径，只留最后一级目录名。`.sqlite` 一律不打开。

| 系统 | 默认目录 |
|---|---|
| Linux | `~/.procode`、`~/.canoryn`、`~/.config/procode`、`~/.config/canoryn`、`~/.local/share/procode`、`~/.local/share/canoryn`。设置了 `XDG_CONFIG_HOME` / `XDG_DATA_HOME` 时，也看对应目录 |
| macOS | 上面这些，再加 `~/Library/Application Support/ProCode` 和 `~/Library/Application Support/Canoryn` |
| Windows | `%APPDATA%\ProCode`、`%APPDATA%\Canoryn`、`%LOCALAPPDATA%\ProCode`、`%LOCALAPPDATA%\Canoryn`，以及用户目录下的 `.procode`、`.canoryn` |

这些目录里还没有 usage 文件时，结果就是 0 行。可以把已经打好 `tool=procode` 的 usage JSON 落到其中任一目录，或继续直接 POST。这不是一条新的模型网关。

换目录：`export PROCODE_DATA_DIR=/你的目录`，或配置 `roots.procode`。

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
