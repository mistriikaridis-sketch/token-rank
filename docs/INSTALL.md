# 安装

给不写代码的同事。装好 Docker 之后，大约 10 分钟可以在自己电脑上看到排行榜。

本产品只统计企业内部的用量。它**不是**大模型网关，也**不是**公网热度榜。

## 你需要准备

- 已安装 [Docker Desktop](https://www.docker.com/products/docker-desktop/)（或本机已有 Docker）
- 本仓库已下载到一个文件夹里

## 一条命令先跑起来（试用）

在仓库文件夹里打开终端，执行：

```bash
docker compose up -d --build
```

然后用浏览器打开：<http://localhost:8000>

这一步使用的是**演示密钥**，方便你马上看到页面。页面顶部会出现黄色提醒。试用可以先不管；交给别人用、或放到公司服务器上之前，必须按下面「换掉所有演示密钥」做完。

## 导入一份示例，确认有数

仍在仓库文件夹里：

```bash
export TOKEN_RANKINGS_API_KEY=dev-token-rankings-change-me
curl -s -X POST http://localhost:8000/api/v1/ingest/litellm \
  -H "Authorization: Bearer $TOKEN_RANKINGS_API_KEY" \
  -H 'Content-Type: application/json' \
  -d @fixtures/litellm_spend_logs.json
```

成功时会看到 `"accepted"` 大于 0（再执行一次会变成 `"skipped"`，这是正常的，表示同一条不会重复计算）。

回到网页：

1. 打开 [登录](http://localhost:8000/login)。本机试用没有 Google 时，展开 **私有部署 / 密钥登录**。
2. 用户密钥粘贴 `demo-alice-key`（这是演示用的，正式环境要换掉），点「用密钥登录」。
3. 还没有用量时会进入 [接入](http://localhost:8000/onboard)。在仓库根目录运行命令。命令会上报一次，并接上采集器原来的定时任务。每日自动同步用量。
4. 再打开 [我的](http://localhost:8000/me) 和 [Agent](http://localhost:8000/agents)。

检查是否全部通过：

```bash
bash scripts/customer_smoke.sh
```

最后一行应是 `ALL CUSTOMER SMOKE CHECKS PASSED`。任何失败都会让这条命令以非 0 退出。

## 换掉所有演示密钥

试用通过之后、给别人用之前，在仓库目录执行：

```bash
bash scripts/bootstrap_prod.sh
```

脚本可以重复执行。没有 `--force` 时，不会覆盖已经生成的写入密钥和用户名单。

它会做这些事：

1. 生成写入密钥，写入 `.env` 的 `TOKEN_RANKINGS_API_KEY`。
2. 把 `RANKINGS_REQUIRE_AUTH` 设为 `1`。
3. 生成 `fixtures/users.prod.json`：一名管理员、两名示例成员（`sample-member`、`sample-other`）。文件里只有 SHA-256，明文只在终端打印一次。
4. 打印如何把这份名单挂进容器。不要改仓库里的 `fixtures/users.json`，那是演示账号。形状示例见 `fixtures/users.example.json`，也可以把名单放在仓库外再挂载。

不要使用 `dev-token-rankings-change-me`、`CHANGE_ME`、`demo-admin-key`、`demo-alice-key`、`demo-dave-key`。

然后在 `docker-compose.yml` 里取消注释这一行（只读挂载，不是网关）：

```yaml
- ./fixtures/users.prod.json:/run/token-rankings/users.json:ro
```

确认 `.env` 里是 `USERS_FILE=/run/token-rankings/users.json`，再重启：

```bash
docker compose up -d --build
```

确认黄色「演示密钥」提醒已经消失：

```bash
curl -sS http://localhost:8000/health | python3 -c 'import json,sys; d=json.load(sys.stdin); s=d["setup"]; assert s["demo_keys_in_use"] is False, s; assert s["rankings_require_auth"] is True, s; print("demo_keys_in_use=false")'
```

再打开 <http://localhost:8000>。顶部不应再出现黄条。然后：

```bash
export TOKEN_RANKINGS_API_KEY=你在终端里抄下的写入密钥
export ALICE_KEY=示例成员的用户密钥
export ALICE_ACCOUNT=sample-member
export OTHER_ACCOUNT=sample-other
SMOKE_REQUIRE_AUTH=1 bash scripts/customer_smoke.sh
```

`.env` 和 `fixtures/users.prod.json` 不要提交到仓库，不要发到聊天软件里。丢失用户密钥时只能 `bash scripts/bootstrap_prod.sh --force`，旧密钥会作废。

## 可选：用 Postgres

默认是 SQLite，适合一台电脑上试用。多人同时用、或要做备份时，改用 Postgres。

先在 `.env` 或当前终端里设置这些变量。密码没有默认值，**不要用 `token` 当密码**（`token` 只是可选的数据库角色名）：

| 变量 | 是否必填 | 说明 |
|---|---|---|
| `POSTGRES_PASSWORD` | 必填 | 长随机密码，只用字母和数字 |
| `POSTGRES_USER` | 可选 | 角色名，默认 `token` |
| `POSTGRES_DB` | 可选 | 库名，默认 `token_rankings` |

```bash
export POSTGRES_PASSWORD='换成你们自己的长随机密码'
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d --build
```

数据库端口只绑定在 `127.0.0.1:5432`，别的机器连不上。不要改成对所有网卡开放。

打开 <http://localhost:8000/health>，应能看到数据库类型是 `postgres`。

更细的自检（给负责安装的同事）：`bash scripts/verify_postgres.sh`。

## 可选：HTTPS 反向代理

只把浏览器流量转到本应用的 8000 端口。这里不要做模型转发或密钥池。

Caddy：

应用端口在 `docker-compose.yml` 里绑定 `127.0.0.1:8000`，不要改成对所有网卡开放。下面的域名只是示例，不代表已经把 DNS 指到你的机器。

```caddy
tokenrank.online {
    reverse_proxy 127.0.0.1:8000
}
```

nginx：

```nginx
server {
    listen 443 ssl;
    server_name tokenrank.online;
    # ssl_certificate     /path/fullchain.pem;
    # ssl_certificate_key /path/privkey.pem;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
    }
}
```

## 企业试点上线清单

给要在公司内网正式试用的同事。自己在笔记本上看页面，从上面「一条命令先跑起来」做完即可，这一节可以先不打勾。

做完再交给别人用。每一项都要能在浏览器或命令输出里核对。更细的说明就在上面各节，以及 [SECURITY.md](SECURITY.md)。

- [ ] **换掉演示密钥。** 在仓库目录执行 `bash scripts/bootstrap_prod.sh`。没有 `--force` 时不会覆盖已经生成的密钥。不要再使用 `dev-token-rankings-change-me`、`CHANGE_ME`、`demo-admin-key`、`demo-alice-key`、`demo-dave-key`。用户名单用脚本生成的 `fixtures/users.prod.json`（或仓库外的文件），不要改 `fixtures/users.json`。
- [ ] **排行必须登录。** `.env` 里 `RANKINGS_REQUIRE_AUTH=1`。重启后打开 `/health`，`setup.rankings_require_auth` 为 `true`，`setup.demo_keys_in_use` 为 `false`，网页顶部黄条消失。再跑 `SMOKE_REQUIRE_AUTH=1 bash scripts/customer_smoke.sh`。
- [ ] **数据库用 Postgres。** 设置长随机的 `POSTGRES_PASSWORD`（不要用 `token` 当密码）。执行 `docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d --build`。`/health` 里数据库类型是 `postgres`。端口只留在 `127.0.0.1:5432`。自检：`bash scripts/verify_postgres.sh`。
- [ ] **HTTPS 只做反向代理。** 用上面的 Caddy 或 nginx，把浏览器流量转到本机 `127.0.0.1:8000`。示例域名写 `tokenrank.online`。不要在这层转发模型请求，也不要做密钥池。证书用公司自己的。应用端口保持绑在 `127.0.0.1`。
- [ ] **本机采集。** 每台开发机按 [collector/INSTALL.md](../collector/INSTALL.md) 安装（服务上的同一份是 `/docs/collector.md`）。覆盖 WorkBuddy、Claude Code、Codex、Continue、Cursor、ProCode。只上传 Token 数字。多账号、多项目靠日志里的邮箱和项目目录拆开，不要只报一个总数。
- [ ] **真机采集验收。** 在装了开发工具的电脑上按 [ACCEPTANCE.md](ACCEPTANCE.md) 的「真机采集验收」打勾：排行榜地址、六个工具的 preview/once、账号 × 项目、提醒与 FinOps 导出、隐私、第二次 once、Cursor 的 `~/.cursor/exports` 回退。ProCode 仍是 placeholder，不解密聊天库。
- [ ] **确认拆开可见。** 打开 [项目榜](http://localhost:8000/projects)。每一行是「账号 × 项目」，顶部写着按账号 × 项目拆开。再用成员密钥请求别人的 `account_id`，应得到 403。个人榜同一人跨账号时，行下面有拆分，不是只有合计。
- [ ] **提醒。** 打开 `/alerts`。规则是闲置、用量过低、费用突增、账号费用阈值。配置见下一节。成员密钥只评估自己的账号。
- [ ] **FinOps 导出。** 用用户密钥（不要用写入密钥）下载：

```bash
curl -s -H "Authorization: Bearer 成员或管理员密钥" \
  'http://localhost:8000/api/v1/export/usage.csv?window=30d&grain=finops' \
  -o finops.csv
```

表头含 `account_id,project_id,model,tool`。JSON 把 `grain=finops` 加在 `/api/v1/export/usage.json` 上。未知模型保留原名；估价落到 default 档时 `estimate` 为 `true`。

`.env` 和 `fixtures/users.prod.json` 不要提交，不要发到聊天里。

### 提醒怎么配

写在 `.env`，改完执行 `docker compose up -d --build`（用了 Postgres 叠加时带上那两个 `-f`）。

| 变量 | 作用 | 不设置时 |
|---|---|---|
| `LOW_USAGE_THRESHOLD_TOKENS` | 低于该 Token 数算用量过低 | `10000` |
| `ALERT_SPEND_SPIKE_RATIO` | 相对上一等长窗口的倍数 | `2` |
| `ALERT_SPEND_SPIKE_MIN_USD` | 低于该金额不报突增 | `1` |
| `ALERT_SPEND_THRESHOLD_USD` | 单个账号费用上限（美元） | 不启用，除非请求里带 `spend_threshold_usd` |
| `ALERT_ACCOUNT_THRESHOLDS` | 按 `account_id` 覆盖 | 不覆盖 |
| `LOW_USAGE_WEBHOOK_URL` | 点「推送」或 `notify=true` 时 POST | 不外发 |

按账号覆盖的例子（一行）：

```bash
ALERT_ACCOUNT_THRESHOLDS={"acme-prod":{"min_tokens":5000,"spend_usd":20}}
```

`window=all` 没有上一窗，不评估费用突增。用量高只是提醒去拆项目，不等于这个人更会用。长期为零通常说明采集或工作流还没接上。

页面 `/alerts` 会显示同一段中文说明。旧地址 `/api/v1/alerts/low-usage` 仍然只做低用量，试点请用 `GET /api/v1/alerts`。

## 从 0.23.0 升到 0.24.0

在仓库目录：

```bash
git pull
docker compose up -d --build
```

启动时会自己补上 Google 用户和浏览器会话两张表，不用手工改数据库。表里不存 Google 的令牌。

公开站点要在 Google Cloud 控制台建一个 OAuth 客户端。步骤和占位环境变量见 [README](../README.md) 的「在 Google Cloud 里创建 OAuth 客户端」。回调地址是 `https://tokenrank.online/auth/google/callback`。私有部署可以不填 `GOOGLE_CLIENT_ID`、`GOOGLE_CLIENT_SECRET`、`GOOGLE_REDIRECT_URI`，登录页只用用户密钥。

登录后如果这个账号还没有用量，会进入「接入」。安装命令会调用原来的 `python -m collector service`，每天自动同步用量。

## 从 0.22.0 升到 0.23.0

在仓库目录：

```bash
git pull
docker compose up -d --build
```

启动时会自己补上连接诊断用的表，不用手工改数据库。榜上的费用默认仍只按输入和输出，数字不会因为这次升级突然把缓存加进去。需要含缓存时，在榜上勾选「含缓存」。

定价表版本和更新日期在 <http://localhost:8000/api/v1/pricing/formula> 的 `table_version`、`updated_at`。不认识的模型会入库，费用用默认档，页面标「估算」。

本机浏览器仍打开 <http://localhost:8000>。给同事用时，用反向代理把 `tokenrank.online` 转到 `127.0.0.1:8000`。这一步只改你自己的代理配置，仓库不会替你改 DNS。

## 下一步

- 开发者本机采集（Token 数字，不含对话）：[collector/INSTALL.md](../collector/INSTALL.md)（服务上的地址是 `/docs/collector.md`）
- 验收清单：[ACCEPTANCE.md](ACCEPTANCE.md)
- 打不开或报错：[TROUBLESHOOTING.md](TROUBLESHOOTING.md)
- 什么可以公开、什么不能：[SECURITY.md](SECURITY.md)
