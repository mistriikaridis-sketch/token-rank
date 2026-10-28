# 安全说明

给准备把这个服务交给同事使用的人。不需要懂代码，但请逐条看完。

下面三列和网页 `/privacy` 一致。

| 内容 | 归类 | 说明 |
|---|---|---|
| Token 计数（输入、输出、缓存读、缓存写、推理） | 会上传 | 只有数字 |
| 模型名、工具名、时间 | 会上传 | 用来归类，并按北京时间切日 |
| 账号、显示名、部门 | 会上传 | 只在企业内部排行里用 |
| 项目目录名 | 会上传 | 例如 chatbot-core，不是完整路径，也不是源文件名 |
| 去重用的 request_id | 会上传 | 同一条再报一次不会叠量 |
| 本机原始日志和 Cursor 的 state.vscdb | 仅本地 | 只在你的电脑上抽出计数，整库不上传 |
| 定时任务和采集配置 | 仅本地 | cron / systemd / launchd / 计划任务 |
| 个人上报密钥（collector.env） | 仅本地 | 文件权限 0600。不要转发，不要提交到 git |
| 代码和源文件内容 | 永不采集 | 不上传 |
| 对话、提示词、回复 | 永不采集 | 含大段 content / prompt 的行会整行跳过 |
| 文件正文、截图 | 永不采集 | 不上传 |
| 密钥、Cookie、钥匙串 | 永不采集 | ProCode 不解密、不打开 sqlite、不读钥匙串、不启动 canoryn |

先看不会发送的内容：

```bash
python -m collector preview
python -m collector once --dry-run
```

`ctr_` 个人上报密钥只能给这一个账号上报用量。它不能清库，也不能让服务器去扫目录。

生产环境把应用绑在 `127.0.0.1:8000`，再用 Caddy 或 nginx 反代。文档里的示例域名是 `tokenrank.online`，只是配置例子。从 0.22.0 升到 0.23.0 见 [INSTALL.md](INSTALL.md)。

## 默认是「先能试用」

| 项目 | 试用默认 | 生产环境应该怎样 |
|---|---|---|
| 看排行要不要登录 | 网页仍要先打开 `/login`。接口在 `RANKINGS_REQUIRE_AUTH=0` 时，脚本可以不带密钥读取 | **改为 `1`**。网页和接口都要登录（Google 或用户密钥） |
| 写入密钥 | `dev-token-rankings-change-me`（或 `.env` 里的 `CHANGE_ME`） | 换成一长串只有你们知道的随机字符 |
| 用户密钥 | `demo-admin-key`、`demo-alice-key`、`demo-dave-key` | 运行 `scripts/bootstrap_prod.sh`，或在自己的名单里全部换掉 |
| 数据库 | 本机 SQLite | 需要多人使用时改 Postgres，并更换数据库密码 |
| 网页提醒 | 仍在使用演示密钥时，顶部出现黄条 | 黄条消失后再交给别人用 |

试用默认是故意放宽的，这样 10 分钟内能跑通。**不要**带着这些默认值上公网或公司正式环境。

生产可以运行 `bash scripts/bootstrap_prod.sh`。它会写入：

```bash
RANKINGS_REQUIRE_AUTH=1
TOKEN_RANKINGS_API_KEY=换成你们自己的长随机串
```

用户名单写到 `fixtures/users.prod.json`（或你指定的路径），里面只有密钥的 SHA-256，明文只在终端打印一次。没有 `--force` 时不会覆盖已有密钥。然后 `docker compose up -d --build`。

## 密钥不要混用

1. **写入密钥**（`TOKEN_RANKINGS_API_KEY`）  
   只给「把用量送进排行榜」的脚本。不要粘贴到浏览器，不要写进前端页面，不要发在群里。

2. **用户密钥**（用户名单里的 `api_key`）  
   给同事登录页面。成员只能看自己的账号；管理员可以看全站。  
   演示值见 `fixtures/users.json`，交付前必须替换。

登录页 `/login` 的「私有部署 / 密钥登录」只保存**用户密钥**，而且只存在这台浏览器里。公开站点的 Google 登录用 httpOnly Cookie，不把 Google 令牌放进浏览器存储。客户端密钥只留在服务器环境变量里。

3. **Google 登录**（公开站点）  
   同事点「使用 Google 登录」。新邮箱默认是成员。要当管理员，把邮箱写进 `GOOGLE_ADMIN_EMAILS`。回调地址是 `https://tokenrank.online/auth/google/callback`。私有部署不填 Google 三项即可。

4. **个人上报密钥**（接入页命令里的 `ctr_…`）  
   只属于一个账号，用来让这台电脑上报用量。它不能清库，也不能让服务器去扫目录。  
   命令里看得到这串密钥，因为同事要复制到自己的电脑。**不要转发**，不要贴到群里。数据库里会留一份明文，方便本人下次打开接入页还能看到同一条命令，所以数据库文件不要外传。

## 不要暴露这些东西

- 不要把填好的 `.env` 提交到 Git，或贴到聊天、邮件、截图里。
- 不要把真实密钥写进仓库。仓库里只应出现 `CHANGE_ME`、`dev-token-rankings-change-me` 和 `demo-*-key` 这类占位。
- 不要在未换密钥、且 `RANKINGS_REQUIRE_AUTH` 仍为 `0` 时，把 8000 端口开放到公网。否则任何人都能看用量，也能用演示写入密钥灌数据。
- 不要把 Postgres 的 5432 端口开放到公网。叠加配置只绑定 `127.0.0.1`。密码来自 `POSTGRES_PASSWORD`，没有默认密码，不要用 `token`。
- 不要把对话原文、源代码、供应商账单密钥送进本服务。采集只接收用量数字。
- 可选的 `LITELLM_MASTER_KEY`、`CURSOR_ADMIN_API_KEY` 属于别的系统的密钥。用到才填写，并同样不要提交。

`.gitignore` 已经忽略 `.env` 和本地数据库文件。

## 黄条是什么意思

只要写入密钥或用户密钥仍是演示值 / `CHANGE_ME`，`/health` 的 `setup.demo_keys_in_use` 为真，网页顶部会提示先换密钥。

这条提示**不会**把密钥本身显示出来。换完并重启后，黄条应消失。若只换了密钥、但生产环境仍是 `RANKINGS_REQUIRE_AUTH=0`，健康检查里仍会留下「生产请设为 1」的文字说明；正式对外前请改成 1。

## 成员隔离

- 成员请求别人的账号时，接口返回 **403**，页面用中文说明「只能查看自己的账号」。
- 这不能代替「生产必须登录」。匿名还能看全榜时，隔离只对已经贴了成员密钥的人生效。所以生产要设 `RANKINGS_REQUIRE_AUTH=1`。

## 本产品故意不做的事

- 不做自研大模型网关（不代理、不转发别人的模型请求）。
- 不做公网模型热度榜，也不去克隆公开排行网站。

用量继续走你们已经在用的网关或工具，本服务只接收汇总后的用量。详见 [NON_GOALS.md](NON_GOALS.md)。

内网试点按 [INSTALL.md](INSTALL.md) 的「企业试点上线清单」逐项做：Postgres、HTTPS 反向代理、换掉演示密钥、`RANKINGS_REQUIRE_AUTH=1`、本机采集。提醒和导出也不要把对话原文送出去。

反代示例（域名只是例子，不会自动改 DNS）：

```caddy
tokenrank.online {
    reverse_proxy 127.0.0.1:8000
}
```

## English

The three lists above are the same as `/privacy`: uploaded token counts and names, local logs and the collector env file, and never-collected source, chat, files, screenshots, secrets, cookies, and keychain. `python -m collector preview` and `python -m collector once --dry-run` print the payload on the laptop and do not send it.

A `ctr_` token writes usage for one account. It cannot clear the database or scan the server. Bind the app to `127.0.0.1:8000` and reverse-proxy `tokenrank.online` only as a documented example. Upgrade steps are in INSTALL.md. License remains MIT.
