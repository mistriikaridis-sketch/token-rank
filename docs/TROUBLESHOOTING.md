# 遇到问题

按你看到的现象对照。命令都在仓库文件夹里执行。

## 浏览器打不开 http://localhost:8000

1. 确认 Docker 正在运行。
2. 看容器是否起来：

```bash
docker compose ps
```

状态不是运行中时，看日志：

```bash
docker compose logs --tail 80 token-rankings
```

3. 8000 端口被别的程序占用时，先停掉那个程序，或改 `docker-compose.yml` 里的 `"8000:8000"`（冒号左边是你电脑上的端口）。
4. 改完配置后重新启动：`docker compose up -d --build`。

## 页面顶部一直有黄色提醒

这是正常的安全提示：服务还在用演示密钥，或排行榜还没要求登录。

处理办法见 [SECURITY.md](SECURITY.md)。全部换成你们自己的密钥，并把生产环境的 `RANKINGS_REQUIRE_AUTH` 设为 `1` 之后，刷新页面，黄条会消失。

本机试用可以暂时留着黄条。

## 提示「密钥不正确」或「还没有提供密钥」（401）

页面上的中文大意是：这次请求没有带对密钥。

- 在网页「首次配置」里粘贴的是**用户密钥**（演示环境是 `demo-alice-key`），不是采集用的写入密钥。
- 导入示例的命令用的是**写入密钥**（演示环境是 `dev-token-rankings-change-me`，或你在 `.env` 里设置的那一串）。两种密钥不能混用。
- 已经改过 `.env` 时，终端里要重新 `export TOKEN_RANKINGS_API_KEY=...`，再跑导入或 `scripts/customer_smoke.sh`。
- 生产环境开了 `RANKINGS_REQUIRE_AUTH=1` 之后，不贴密钥就看不了排行。先在「首次配置」粘贴用户密钥。

## 提示「只能查看自己的账号」（403）

这是成员隔离，不是故障。

- 成员密钥只能看自己的账号。Alice 不能打开 Dave 的数据。
- 需要看全公司时，使用管理员密钥（演示环境是 `demo-admin-key`），不要把管理员密钥发给每个同事。
- 若提示「没有绑定账号」，请管理员检查用户名单里该成员是否填写了 `account_id`。

## 导入示例失败

- HTTP 401：写入密钥和服务器不一致。见上面的 401。
- HTTP 503：服务器没有配置 `TOKEN_RANKINGS_API_KEY`。在 `.env` 里填上，然后 `docker compose up -d --build`。
- 找不到文件：确认当前目录是仓库根目录，且存在 `fixtures/litellm_spend_logs.json`。

## 排行榜是空的

- 演示人设默认不出现在榜上。请先按 [INSTALL.md](INSTALL.md) 导入一份示例，或等真实用量进来。
- 时间选在「今天」但示例数据是前一天时，改选「近 7 天」或让验收脚本使用全部时间（脚本已经这样做）。
- 勾了「隐藏未标注」且数据没有项目名时，项目榜会空。可以先取消勾选看一下。
- 贴了成员密钥时，只能看到该成员自己的账号。换账号或换管理员密钥再看。

## 「我的」一直让我选择账号

1. 先导入示例，否则没有账号可选。
2. 在「首次配置」粘贴用户密钥并选择账号，点「保存并查看我的」。
3. 成员密钥会自动锁在自己的账号上，不能改成别人的。

## Postgres 起不来

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml logs --tail 80 postgres
```

- 5432 端口已被占用：改 `docker-compose.postgres.yml` 里的端口，或停掉本机另一个 Postgres。
- 第一次改过密码又启动失败：旧数据卷里还是旧密码。试用环境可以删掉卷重来（会清空试用数据）：

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml down -v
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d --build
```

不要在存有正式数据的机器上执行 `down -v`。

## 冒烟脚本失败

`bash scripts/customer_smoke.sh` 会打印 `FAIL:` 和原因，并以非 0 退出。

- 打不开 `/health`：服务没起来，先看本节第一段。
- 导入返回不是 200：写入密钥不对。
- 排行打不开：同样先看密钥；生产环境必须带密钥。

脚本会读取环境变量 `TOKEN_RANKINGS_API_KEY`。没设置时，会尝试使用仓库里的 `.env`，再不行就用演示值 `dev-token-rankings-change-me`。
