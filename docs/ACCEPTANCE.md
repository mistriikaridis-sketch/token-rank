# 验收清单

交给购买方之前，逐项打勾。不需要读代码。服务按 [INSTALL.md](INSTALL.md) 启动后，在仓库目录执行命令。

下面的演示密钥只在你**还没换密钥**时有效。已经换成自己的密钥时，把命令里的密钥换成 `.env` 和用户名单里的新值。

## 1. 健康检查

浏览器打开 <http://localhost:8000/health>，或：

```bash
curl -s http://localhost:8000/health
```

- [ ] 能打开，且 `"status"` 为 `"ok"`
- [ ] `"version"` 是 `0.17.0`（或更新）
- [ ] 若仍是演示密钥，`"setup"."demo_keys_in_use"` 为 `true`，网页顶部有黄色提醒

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

给负责安装的同事的一键检查：`bash scripts/verify_rbac.sh`。

## 5. 客户冒烟脚本

```bash
bash scripts/customer_smoke.sh
```

- [ ] 最后一行是 `ALL CUSTOMER SMOKE CHECKS PASSED`
- [ ] 命令的退出码是 0（失败时是非 0，不能当成通过）

脚本会依次请求：健康检查、导入 `fixtures/litellm_spend_logs.json`、个人榜、Agent 榜、以及 Alice 的「我的」。

## 6. 交付前的安全项

正式交给对方使用时再打勾。本机试用可以先留空。

- [ ] `.env` 里的 `TOKEN_RANKINGS_API_KEY` 已不是 `CHANGE_ME` 或 `dev-token-rankings-change-me`
- [ ] `fixtures/users.json`（或你们自己的用户名单）里没有 `demo-admin-key` / `demo-alice-key` / `demo-dave-key`
- [ ] 生产环境 `RANKINGS_REQUIRE_AUTH=1`
- [ ] 刷新网页后，黄色「演示密钥」提醒已经消失
- [ ] 没有把 `.env`、数据库文件或真实密钥提交到仓库

说明见 [SECURITY.md](SECURITY.md)。
