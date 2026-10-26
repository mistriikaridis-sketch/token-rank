# 变更记录

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
