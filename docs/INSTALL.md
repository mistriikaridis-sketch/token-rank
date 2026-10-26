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

1. 打开首页的 **首次配置**。
2. 用户密钥粘贴 `demo-alice-key`（这是演示用的，正式环境要换掉）。
3. 账号选择 `acme-prod`。
4. 打开 [我的](http://localhost:8000/me) 和 [Agent](http://localhost:8000/agents)。

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

```caddy
rankings.example.internal {
    reverse_proxy 127.0.0.1:8000
}
```

nginx：

```nginx
server {
    listen 443 ssl;
    server_name rankings.example.internal;
    # ssl_certificate     /path/fullchain.pem;
    # ssl_certificate_key /path/privkey.pem;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
    }
}
```

## 下一步

- 开发者本机采集（Token 数字，不含对话）：[collector/INSTALL.md](../collector/INSTALL.md)（服务上的地址是 `/docs/collector.md`）
- 验收清单：[ACCEPTANCE.md](ACCEPTANCE.md)
- 打不开或报错：[TROUBLESHOOTING.md](TROUBLESHOOTING.md)
- 什么可以公开、什么不能：[SECURITY.md](SECURITY.md)
