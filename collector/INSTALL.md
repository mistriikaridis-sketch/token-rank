# 本机采集器

许可证：**MIT**。服务跑起来之后，这份说明也可以从 `/docs/collector.md` 读到。给装了 AI 编程工具的开发机用。它定期读本机用量日志，只把 **Token 数字** 报到你们自己的 Token 消耗排行榜，让这个人出现在私有榜上。

这不是公网热度榜，也不是模型网关。Cursor 的用量继续走服务端 Admin API（本机 Cursor 采集不做）。ProCode 仍是占位。

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
| `tool` / `source` | `workbuddy` / `claude_code` / `codex` / `continue` |

**不会**上报：提示词、回复、工具参数、源代码、源文件名、截图。含大段 `content` / `prompt` 的 WorkBuddy 行整行跳过。

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

默认每 30 分钟一轮。只扫一种工具：`--tool workbuddy`（`workbody` 同样认）。

## 它会读哪些目录

没在配置或环境变量里指定时，存在才扫：

| 工具 | 默认目录 |
|---|---|
| WorkBuddy | `~/.workbuddy/projects`、`~/.workbody/projects`、`~/Library/Application Support/WorkBuddy/projects` |
| Claude Code | `~/.claude/projects` |
| Codex | `~/.codex/sessions` |
| Continue | `~/.continue/dev_data` |

环境变量 `WORKBUDDY_PROJECTS_DIR`、`CLAUDE_PROJECTS_DIR`、`CODEX_SESSIONS_DIR`、`CONTINUE_DATA_DIR` 会盖过上面的默认位置。

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

第一次每个工具的 `accepted` 应大于 0。第二次应为 `accepted=0` 且 `skipped` 大于 0（同一条不叠量）。预览输出里不应出现 `SECRET` 或家目录全路径。

只跑测试、不起服务：

```bash
python -m unittest discover -s tests -v
```
