# 本地工具探测报告

- 生成时间（UTC）：`2026-10-26T01:18:58.435149+00:00`
- HOME：`/workspace/token-rankings/fixtures/probe_home`

## 摘要

| 工具 | 状态 | 解析成功率 |
|---|---|---|
| WorkBuddy | `found` | 42.86% |
| Cursor | `parsed_exports` | 66.67% |
| ProCode | `paths_present_no_schema` | n/a（占位） |

## WorkBuddy

- status: `found`
- scanned_root: `/workspace/token-rankings/fixtures/probe_home/.workbuddy/projects`
- mapped_rows: **3**
- parse: lines=7 ok=3 skip_total=4 (json_err=0, not_usage=1, missing_model=1, missing_tokens=0, forbidden_content=2)
- content leak check: **pass**（映射行无 content/prompt）

候选路径：
- ✓ `/workspace/token-rankings/fixtures/probe_home/.workbuddy/projects` · jsonl_files=2 lines=7
- · `/workspace/token-rankings/fixtures/probe_home/.workbody/projects`
- · `/workspace/token-rankings/fixtures/probe_home/Library/Application Support/WorkBuddy/projects`

## Cursor

- status: `parsed_exports`
- mapped_rows: **2**
- parse: lines=3 ok=2 skip_total=1 (json_err=0, not_usage=0, missing_model=1, missing_tokens=0, forbidden_content=0)

候选路径：
- ✓ `/workspace/token-rankings/fixtures/probe_home/.cursor/exports` · jsonl_files=0 lines=0
- ✓ `/workspace/token-rankings/fixtures/probe_home/.cursor` · jsonl_files=0 lines=0
- · `/workspace/token-rankings/fixtures/probe_home/Library/Application Support/Cursor`

## ProCode / Canoryn

- status: `paths_present_no_schema`
- notes: 公开用量 schema 不足。请用 POST /api/v1/ingest/procode 推送统一 usage，或经 LiteLLM tags/header 标注 tool=procode。

候选路径：
- ✓ `/workspace/token-rankings/fixtures/probe_home/.canoryn` · jsonl_files=0 lines=0
- ✓ `/workspace/token-rankings/fixtures/probe_home/.procode` · jsonl_files=0 lines=0
- ✓ `/workspace/token-rankings/fixtures/probe_home/.procode/logs`

## 闭环建议

1. WorkBuddy：`POST /api/v1/scan/workbuddy?root=...`（需 write API Key）
2. Cursor：Admin sync 或 `POST /api/v1/ingest/cursor` 推送 export JSON
3. ProCode：统一 schema → `POST /api/v1/ingest/procode`；见 `/procode` 与 `docs/NON_GOALS.md`

