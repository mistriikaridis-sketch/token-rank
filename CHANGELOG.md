# 变更记录

## 0.22.0

- 接入页「如何接入」：选择 macOS/Linux 或 Windows，复制带个人上报密钥的命令，再检查上次上报。命令旁写明勿转发。另有一段可复制的提示词，让 Cursor / Claude 代跑安装。采集器仍是本仓库这一个，不依赖别人的客户端。
- 隐私页：会上传 / 仅本地 / 永不采集。`preview` 和 `once --dry-run` 写在页面上。另有一段可复制的安全审计提示词。
- 「我的」增加用量卡：区间消耗、列表价估算、企业内排名、连续活跃、设备数和上次同步。有近半年的日历热力、工具和模型占比、缓存费用、按小时明细。日界默认北京时间，可选第二个时区只做对照。
- 设置：可见范围改为全公司 / 本部门 / 仅自己（不是公开榜，也没有城市榜）。可选组织标签。设备可改名、隐藏、停用。停用后这种工具的新上报不再累加。
- 费用公式写在「我的」和设置里：输入、输出、缓存读、缓存写。价目表仍是 `/api/v1/pricing`。排行榜上的费用仍只按输入和输出，避免历史数字突然变化。
- 异常用量待审：今天达到近两周中位数的 3 倍且不少于 5 万 Token 时标出，给费用核对用。诊断表单只收日志结构，不收对话。
- 个人上报密钥只写入自己的账号，不能清库，也不能在服务器上扫描目录。

## 0.21.1

- ProCode / Canoryn 仍不解密、不打开 sqlite、不读钥匙串、不启动 `canoryn`。公开文档没有本机用量接口，健康检查继续标 `placeholder`，并写明 `blocked`。
- 多认几处常见目录和导出文件名（含 `token-usage.json`、`usageEvents`、驼峰 token 字段）。`discussions/` 与 `blueprints/` 跳过。预览写明读了多少行、有多少数据库文件没打开。
- 增加真机采集验收清单：安装、六个工具的 preview/once、账号 × 项目、提醒、FinOps 导出、隐私、第二次 once、Cursor 0/0 时改放 `~/.cursor/exports`。

## 0.21.0

- 排行按账号和项目拆开：个人榜、部门榜带账号 × 项目明细；Agent 榜按账号 × 助手分行；`/api/v1/rankings?group_by=account_project` 同时返回两个维度。筛选和空状态写明当前账号、项目。
- 试点提醒：闲置、用量过低、费用突增、按账号的费用阈值。页面为中文。配置见 `docs/INSTALL.md` 的企业试点上线清单。`/api/v1/alerts/low-usage` 仍可用。
- 导出增加 `grain=finops`（CSV / JSON），按账号、项目、模型、工具汇总。未知模型保留原名，default 档估价标 `estimate`。
- 安装文档补充私有部署清单：Postgres、HTTPS 反向代理、换掉演示密钥、`RANKINGS_REQUIRE_AUTH`、本机采集。

## 0.20.0

- 本机采集器增加 Cursor 与 ProCode（Canoryn 目录也认）。`preview` / `once` / `upload` / `service` 与原来的四个工具一样走现有 ingest。
- Cursor：读 `state.vscdb` 里 `bubbleId:*` 的 token 与模型名，以及 `exports/` 和文件名含 `usage` 的 JSON。不读对话，不读 cookie。本地计数全是 0 的气泡跳过，可改放仪表盘导出的 usage JSON。
- ProCode：只读各系统常见目录里的 usage json/jsonl。不解密聊天库，不读钥匙串，不调用 `canoryn` 命令。
- 预览里的目录把家目录收成 `~/`。未知模型仍入库，估价用 `default` 档，并标成估算。
- 多账号、多项目按日志里的邮箱和项目目录名（或对话 id 前缀）拆开。同一 `(source, request_id)` 再报一次不叠量。
