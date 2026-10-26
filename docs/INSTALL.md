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

试用通过之后、给别人用之前：

1. 复制配置模板：

```bash
cp .env.example .env
```

2. 用记事本打开 `.env`，把 `TOKEN_RANKINGS_API_KEY=CHANGE_ME` 改成一长串只有你们知道的随机字符。不要用 `CHANGE_ME`，也不要继续用 `dev-token-rankings-change-me`。
3. 打开 `fixtures/users.json`，把 `demo-admin-key`、`demo-alice-key`、`demo-dave-key` 都换成你们自己的用户密钥，并把 `account_id` 改成真实账号名。
4. 生产环境把 `.env` 里的 `RANKINGS_REQUIRE_AUTH` 改成 `1`。这样不贴密钥就看不到排行。
5. 重新启动：

```bash
docker compose up -d --build
```

6. 刷新网页。黄色提醒消失，才算演示密钥已经换掉。
7. 再跑一次验收时，先让终端读到新密钥：

```bash
export TOKEN_RANKINGS_API_KEY=你在.env里填的那一串
bash scripts/customer_smoke.sh
```

`.env` 不要提交到仓库，不要发到聊天软件里。

## 可选：用 Postgres

默认是 SQLite，适合一台电脑上试用。多人同时用、或要做备份时，改用 Postgres：

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d --build
```

打开 <http://localhost:8000/health>，应能看到数据库类型是 `postgres`。

演示库的账号密码是 `token` / `token`，**只能用于本机试用**。放到服务器上之前，请改 `docker-compose.postgres.yml` 里的 `POSTGRES_PASSWORD`，并同步改 `DATABASE_URL`。不要把数据库端口暴露到公网。

更细的自检（给负责安装的同事）：`bash scripts/verify_postgres.sh`。

## 下一步

- 验收清单：[ACCEPTANCE.md](ACCEPTANCE.md)
- 打不开或报错：[TROUBLESHOOTING.md](TROUBLESHOOTING.md)
- 什么可以公开、什么不能：[SECURITY.md](SECURITY.md)
