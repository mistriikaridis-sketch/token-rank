# 本地工具探测报告

- 生成时间（UTC）：`2026-10-26T10:01:36.218651+00:00`
- HOME：`/workspace/fixtures/probe_home`

## 摘要

| 工具 | 状态 | 解析成功率 |
|---|---|---|
| WorkBuddy | `found` | 42.86% |
| Cursor | `parsed_exports` | 66.67% |
| ProCode | `parsed_exports` | 100.0%（适配仍是 placeholder） |

## WorkBuddy

- status: `found`
- scanned_root: `/workspace/fixtures/probe_home/.workbuddy/projects`
- mapped_rows: **3**
- parse: lines=7 ok=3 skip_total=4 (json_err=0, not_usage=1, missing_model=1, missing_tokens=0, forbidden_content=2)
- content leak check: **pass**（映射行无 content/prompt）

候选路径：
- ✓ `/workspace/fixtures/probe_home/.workbuddy/projects` · jsonl_files=2 lines=7
- · `/workspace/fixtures/probe_home/.workbody/projects`
- · `/workspace/fixtures/probe_home/Library/Application Support/WorkBuddy/projects`

## Cursor

- status: `parsed_exports`
- mapped_rows: **2**
- parse: lines=3 ok=2 skip_total=1 (json_err=0, not_usage=0, missing_model=1, missing_tokens=0, forbidden_content=0)

候选路径：
- ✓ `/workspace/fixtures/probe_home/.cursor/exports` · jsonl_files=0 lines=0
- ✓ `/workspace/fixtures/probe_home/.cursor` · jsonl_files=0 lines=0
- · `/workspace/fixtures/probe_home/Library/Application Support/Cursor`

## ProCode / Canoryn

- status: `parsed_exports`
- mapped_rows: **2**
- parse: lines=2 ok=2 skip_total=0 (json_err=0, not_usage=0, missing_model=0, missing_tokens=0, forbidden_content=0)
- content leak check: **pass**（映射行无 content/prompt）
- notes: 状态仍是 placeholder。Canoryn 没有公开的本机用量接口：聊天和记忆在加密 SQLite，本适配器不解密、不打开数据库、不读钥匙串、不启动 canoryn。今天只收 usage json/jsonl（~/.procode、~/.canoryn、各系统应用数据目录，或 PROCODE_DATA_DIR），以及 POST /api/v1/ingest/procode。蓝图和 discussions 不入库。未知模型保留原名，估价用 default 档。
- adapter_status: `placeholder`（健康检查仍是这一档）
- 未打开的数据库/打包文件: **1**（canoryn.sqlite）
- 跳过 discussions=1 blueprints=1 凭证文件名=1

候选路径：
- ✓ `/workspace/fixtures/probe_home/.procode` · jsonl_files=0 lines=0
- ✓ `/workspace/fixtures/probe_home/.canoryn` · jsonl_files=0 lines=0
- · `/workspace/fixtures/probe_home/.config/procode`
- · `/workspace/fixtures/probe_home/.config/canoryn`
- · `/workspace/fixtures/probe_home/.config/ProCode`
- ✓ `/workspace/fixtures/probe_home/.config/Canoryn` · jsonl_files=0 lines=0
- ✓ `/workspace/fixtures/probe_home/.local/share/procode` · jsonl_files=1 lines=1
- · `/workspace/fixtures/probe_home/.local/share/canoryn`
- · `/workspace/fixtures/probe_home/.local/share/ProCode`
- · `/workspace/fixtures/probe_home/.local/share/Canoryn`
- · `/workspace/fixtures/probe_home/Library/Application Support/ProCode`
- · `/workspace/fixtures/probe_home/Library/Application Support/Canoryn`
- · `/workspace/fixtures/probe_home/Library/Application Support/procode`
- · `/workspace/fixtures/probe_home/Library/Application Support/canoryn`
- · `/workspace/fixtures/probe_home/AppData/Roaming/ProCode`
- · `/workspace/fixtures/probe_home/AppData/Roaming/Canoryn`
- · `/workspace/fixtures/probe_home/AppData/Roaming/procode`
- · `/workspace/fixtures/probe_home/AppData/Roaming/canoryn`
- · `/workspace/fixtures/probe_home/AppData/Local/ProCode`
- · `/workspace/fixtures/probe_home/AppData/Local/Canoryn`
- · `/workspace/fixtures/probe_home/AppData/Local/procode`
- · `/workspace/fixtures/probe_home/AppData/Local/canoryn`

## 闭环建议

1. WorkBuddy：`POST /api/v1/scan/workbuddy?root=...`（需 write API Key）
2. Cursor：本机 `python -m collector`（state.vscdb 只抽 token；或 `~/.cursor/exports`），或 Admin sync / `POST /api/v1/ingest/cursor`
3. ProCode：usage json/jsonl → `python -m collector`，或统一 schema → `POST /api/v1/ingest/procode`。不解密聊天库，不打开 sqlite，不读钥匙串，不启动 canoryn。健康检查里的状态保持 placeholder。

