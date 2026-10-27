# 验收清单

交给购买方之前，逐项打勾。不需要读代码。服务按 [INSTALL.md](INSTALL.md) 启动后，在仓库目录执行命令。

下面的演示密钥只在你**还没换密钥**时有效。已经换成自己的密钥时，把命令里的密钥换成 `.env` 和用户名单里的新值。

## 1. 健康检查

浏览器打开 <http://localhost:8000/health>，或：

```bash
curl -s http://localhost:8000/health
```

- [ ] 能打开，且 `"status"` 为 `"ok"`
- [ ] `"version"` 是 `0.22.0`
- [ ] 若仍是演示密钥，`"setup"."demo_keys_in_use"` 为 `true`，网页顶部有黄色提醒
- [ ] 交付之后 `"setup"."demo_keys_in_use"` 为 `false`，且 `"setup"."rankings_require_auth"` 为 `true`

## 2. 导入一份示例

```bash
export TOKEN_RANKINGS_API_KEY=dev-token-rankings-change-me
curl -s -X POST http://localhost:8000/api/v1/ingest/litellm \
  -H "Authorization: Bearer $TOKEN_RANKINGS_API_KEY" \
  -H 'Content-Type: application/json' \
  -d @fixtures/litellm_spend_logs.json
```

- [ ] 返回里有 `accepted` 或 `skipped`，两者相加大于 0
- [ ] 再执行一次，`accepted` 为 0 且 `skipped` 大于 0（同一条数据不会加两次）

## 3. 能看到「我的」和 Agent

- [ ] 浏览器打开 <http://localhost:8000/me>
- [ ] 在「首次配置」粘贴 `demo-alice-key`，账号选 `acme-prod`
- [ ] 页面上能看到这个账号的用量，而不是一直停在「请选择账号」
- [ ] 打开 <http://localhost:8000/agents>，能看到 Agent 列表（导入示例之后不应是空白）

## 4. 成员只能看自己的账号

Alice 的演示密钥是 `demo-alice-key`，她的账号是 `acme-prod`。Dave 的账号是 `dave@acme.com`。

```bash
curl -s -o /tmp/alice.json -w '%{http_code}\n' \
  -H 'Authorization: Bearer demo-alice-key' \
  'http://localhost:8000/api/v1/rankings/projects?window=all&account_id=dave%40acme.com'
```

- [ ] 上一行打印 `403`
- [ ] 网页上会显示中文说明：只能查看自己的账号
- [ ] Alice 看自己的项目榜时，只有 `acme-prod`，不会出现 Dave 的账号

成员密钥也不能替别人写入用量（同样是 403，不是 401）：

```bash
curl -s -o /tmp/alice-write.json -w '%{http_code}\n' \
  -X POST 'http://localhost:8000/api/v1/usage' \
  -H 'Authorization: Bearer demo-alice-key' \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-4o-mini","prompt_tokens":1,"completion_tokens":1,"request_id":"accept-member-write","account_id":"dave@acme.com","source":"accept"}'
```

- [ ] 上一行打印 `403`

给负责安装的同事的一键检查：`bash scripts/verify_rbac.sh`。跨账号读写的 403 也包含在 `scripts/customer_smoke.sh` 里。

## 5. 客户冒烟脚本

试用环境：

```bash
bash scripts/customer_smoke.sh
```

交付环境（已经跑过 `scripts/bootstrap_prod.sh` 并重启）：

```bash
export TOKEN_RANKINGS_API_KEY=你的写入密钥
export ALICE_KEY=示例成员密钥
export ALICE_ACCOUNT=sample-member
export OTHER_ACCOUNT=sample-other
SMOKE_REQUIRE_AUTH=1 bash scripts/customer_smoke.sh
```

- [ ] 最后一行是 `ALL CUSTOMER SMOKE CHECKS PASSED`
- [ ] 命令的退出码是 0（失败时是非 0，不能当成通过）
- [ ] 试用模式会检查健康检查、导入、个人榜、Agent 榜、「我的」、成员读/写别人账号时的 403
- [ ] `SMOKE_REQUIRE_AUTH=1` 时，不带密钥看排行返回 401，且 `/health` 的 `setup.demo_keys_in_use` 为 `false`
- [ ] 本机安装了 Python 依赖时，脚本还会做一次内存里的 AUTH=1 探针，确认 `demo_keys_in_use=false`

## 6. 交付前的安全项

正式交给对方使用时再打勾。本机试用可以先留空。

- [ ] 已运行 `bash scripts/bootstrap_prod.sh`，或等价地换掉全部密钥
- [ ] `.env` 里的 `TOKEN_RANKINGS_API_KEY` 已不是 `CHANGE_ME` 或 `dev-token-rankings-change-me`
- [ ] 生产名单不是 `fixtures/users.json`。`fixtures/users.prod.json`（或仓库外的名单）里没有 `demo-admin-key` / `demo-alice-key` / `demo-dave-key`
- [ ] 生产环境 `RANKINGS_REQUIRE_AUTH=1`
- [ ] `/health` 里 `setup.demo_keys_in_use` 为 `false`
- [ ] 刷新网页后，黄色「演示密钥」提醒已经消失
- [ ] Postgres 若启用：密码来自 `POSTGRES_PASSWORD`，不是 `token`；5432 只监听 `127.0.0.1`
- [ ] 没有把 `.env`、`fixtures/users.prod.json`、数据库文件或真实密钥提交到仓库

说明见 [SECURITY.md](SECURITY.md)。内网试点再按 [INSTALL.md](INSTALL.md) 的「企业试点上线清单」核对 Postgres、HTTPS、密钥、登录、采集、账号×项目拆分、提醒和 FinOps 导出。

## 真机采集验收

给 Mateo 在一台装了开发工具的电脑上打勾。不需要读代码。排行榜可以在这台电脑上，也可以在内网另一台。同一份命令也写在 [collector/INSTALL.md](../collector/INSTALL.md)（服务上是 `/docs/collector.md`）。

不要把写入密钥贴进网页「首次配置」。网页上贴的是用户密钥。

### 1. 安装或指到排行榜

本机试跑：

```bash
docker compose up -d --build
export RANKINGS_URL=http://127.0.0.1:8000
```

已经有服务时，只改地址，例如 `export RANKINGS_URL=https://rankings.example.internal`。

```bash
curl -sS "$RANKINGS_URL/health"
```

- [ ] `"status"` 为 `"ok"`
- [ ] `"version"` 是 `0.22.0`
- [ ] `adapters` 里 ProCode 的 `"status"` 仍是 `placeholder`
- [ ] `blocked` 含 `encrypted_sqlite`、`keychain`、`canoryn_cli`
- [ ] `notes` 写明不解密、不打开数据库、不读钥匙串、不启动 canoryn

### 2. 写入密钥，每个工具 preview 和 once

```bash
export TOKEN_RANKINGS_API_KEY=你的写入密钥
python -m collector install --url "$RANKINGS_URL" --account-id mateo@acme.com --participant Mateo

for tool in workbuddy claude_code codex continue cursor procode; do
  echo "==== $tool ===="
  python -m collector preview --tool "$tool"
  python -m collector once --tool "$tool"
done
python -m collector preview --tool canoryn --json
```

- [ ] `workbuddy`、`claude_code`、`codex`、`continue`、`cursor`、`procode` 都跑完，没有连接错误
- [ ] `canoryn` 和 `procode` 扫的是同一处。JSON 里 `tools` 只有 `procode`
- [ ] 本机没有该工具的目录时是 0 行。这是正常的，不是失败
- [ ] ProCode 的说明仍是 placeholder，并写明不解密

### 3. 私有榜能看到账号 × 项目

浏览器打开 `$RANKINGS_URL/projects`。有采集数据之后，每一行是一个账号上的一个项目，不是把同一个人收成一个总数。

```bash
curl -sS -H "Authorization: Bearer 成员或管理员密钥" \
  "$RANKINGS_URL/api/v1/rankings/projects?window=30d&hide_unlabeled=true"
curl -sS -H "Authorization: Bearer 管理员密钥" \
  "$RANKINGS_URL/api/v1/rankings?window=30d&group_by=account_project"
```

- [ ] `totals.breakdown` 是 `account_id × project_id`
- [ ] 同一人至少两个项目时，榜上是两行
- [ ] 用成员密钥去请求别人的 `account_id`，HTTP 状态是 403

个人榜上同一个人跨账号时，行下面有拆分。筛选条会写明当前账号和项目。

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

- [ ] `/api/v1/alerts` 返回 200。页面 `/alerts` 能打开。成员密钥只评估自己的账号
- [ ] CSV 第一行含 `account_id,project_id,model,tool`
- [ ] 不认识的模型名原样还在。估价用 default 档时，JSON 里 `estimate` 为 `true`，`price_tier` 为 `default`。这不是发票

旧地址 `/api/v1/alerts/low-usage` 仍然只做低用量。试点用 `/api/v1/alerts`。

### 5. 隐私

```bash
python -m collector preview --json > /tmp/preview.json
```

- [ ] 输出里没有提示词、回复、密钥、Cookie
- [ ] 没有 `/Users/...` 或 `/home/某用户` 这种家目录全路径。目录显示成 `~/...`
- [ ] ProCode 的 `diagnostics` 只有计数和文件名（例如 `canoryn.sqlite`），没有数据库正文，也没有钥匙串

### 6. 第二次 once 不叠量

```bash
python -m collector once --tool cursor
python -m collector once --tool cursor
```

有 Cursor 用量时：

- [ ] 第二次每个有数据的工具 `accepted` 为 0，`skipped` 大于 0

这台电脑还没有用量时：

- [ ] 两次都打印「没有用量可上报」，榜上的数字不变

`workbuddy`、`claude_code`、`codex`、`continue`、`procode` 同样再跑一次，规则一样。

### 7. Cursor 气泡是 0/0 时改用导出

当前 Cursor 经常把本地气泡的 input 和 output 都写成 0。这种行会跳过，不会用字数冒充 token。

- [ ] `python -m collector preview --tool cursor` 若是 0 行，而本机又有 `state.vscdb`，先确认是不是全 0 被跳过
- [ ] 从 Cursor 仪表盘导出用量，文件名用 `filtered-usage-events.json`，不要把 prompt 放进去
- [ ] 放到 `~/.cursor/exports/`
- [ ] 再 preview，应出现行。`project_id` 只是目录名或对话 id 的前缀

`CURSOR_ADMIN_API_KEY` 只放在跑排行榜的那台机器上，不要写进 `collector.json`。

## 7. 接入、我的用量、可见范围

浏览器打开这些地址，页面是中文：

- <http://localhost:8000/onboard>
- <http://localhost:8000/privacy>
- <http://localhost:8000/me>
- <http://localhost:8000/settings>

贴上 `demo-alice-key` 之后：

```bash
curl -s -H 'Authorization: Bearer demo-alice-key' \
  http://localhost:8000/api/v1/onboarding
curl -s http://localhost:8000/api/v1/privacy
curl -s -H 'Authorization: Bearer demo-alice-key' \
  'http://localhost:8000/api/v1/me/dashboard?window=today'
curl -s http://localhost:8000/api/v1/pricing/formula
curl -s -H 'Authorization: Bearer demo-alice-key' \
  http://localhost:8000/api/v1/anomalies
```

- [ ] 接入页能选 macOS/Linux 和 Windows，命令可以复制，并写着「勿转发」
- [ ] 没贴用户密钥时，命令里**没有**服务器写入密钥
- [ ] 贴了成员密钥后，命令里是 `ctr_` 开头的个人上报密钥，而且写明不要转发
- [ ] 隐私页能看到「会上传 / 仅本地 / 永不采集」，其中代码、对话、文件、密钥、截图在「永不采集」
- [ ] 页面写了 `python -m collector preview` 和 `python -m collector once --dry-run`
- [ ] 「我的」能切换今日、昨日、近 7 天、近 30 天；没有数据时有空状态，而不是报错
- [ ] 能看到预估费用、缓存说明，以及「列表价估算，不是实扣账单」
- [ ] 公式里有输入、输出、缓存读、缓存写，并能打开 `/api/v1/pricing`
- [ ] 设置里能选全公司 / 本部门 / 仅自己。没有城市榜
- [ ] 设备列表能改名、隐藏、停用。停用后再上报，这种工具的新用量不再累加
- [ ] 诊断表单可以提交「目录在不在、preview 几行」这种结构说明；贴上密钥样式会被拒绝
- [ ] 提醒页能看到「异常用量待审」的说明。没有突增时列表是空的

个人上报密钥不能调用清库：

```bash
# 把下面的密钥换成接入页命令里的 ctr_...
curl -s -o /tmp/ctr-clear.json -w '%{http_code}\n' \
  -X POST 'http://localhost:8000/api/v1/admin/clear?scope=all' \
  -H 'Authorization: Bearer ctr_替换成你的个人上报密钥'
```

- [ ] 上一行不是 `200`

### 8. 常见失败

| 看到什么 | 原因 | 怎么处理 |
|---|---|---|
| `缺少 TOKEN_RANKINGS_API_KEY` | 没设写入密钥 | `export TOKEN_RANKINGS_API_KEY=...`，或 `python -m collector install --save-key` |
| `HTTP 401` | 密钥不对，或把网页上的用户密钥当成了写入密钥 | 用服务 `.env` 里的 `TOKEN_RANKINGS_API_KEY` |
| `连不上 http://...` | `RANKINGS_URL` 错了，或服务没起来 | 先 `curl -sS "$RANKINGS_URL/health"` |
| 0 行，且写着没找到目录 | 这台电脑没有该工具的数据目录 | 正常。没装的工具不必造目录 |
| ProCode 目录在，但仍是 0 行 | 里面只有加密库、蓝图或评论，没有 usage json | 看 `diagnostics.unopened_db_seen`。不要解密，也不要启动 `canoryn`。状态保持 `placeholder` |
| preview 里出现家目录全路径 | 采集器版本过旧 | 升到 `0.22.0` |
| 项目榜是空的，但 preview 有行 | 还没 once，或窗口选得太窄，或勾了隐藏未标注 | 先 once，窗口改 `30d` 或 `all`，核对 `project_id` |
