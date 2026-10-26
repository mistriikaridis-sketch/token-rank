# 变更记录

## 0.20.0

- 本机采集器增加 Cursor 与 ProCode（Canoryn 目录也认）。`preview` / `once` / `upload` / `service` 与原来的四个工具一样走现有 ingest。
- Cursor：读 `state.vscdb` 里 `bubbleId:*` 的 token 与模型名，以及 `exports/` 和文件名含 `usage` 的 JSON。不读对话，不读 cookie。本地计数全是 0 的气泡跳过，可改放仪表盘导出的 usage JSON。
- ProCode：只读各系统常见目录里的 usage json/jsonl。不解密聊天库，不读钥匙串，不调用 `canoryn` 命令。
- 预览里的目录把家目录收成 `~/`。未知模型仍入库，估价用 `default` 档，并标成估算。
- 多账号、多项目按日志里的邮箱和项目目录名（或对话 id 前缀）拆开。同一 `(source, request_id)` 再报一次不叠量。
