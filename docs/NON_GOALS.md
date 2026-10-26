# 非目标（刻意不做）

这份短文说明 **Token 消耗排行榜** 明确不做的两件事，以及推荐的替代闭环。避免和 OpenRouter / LiteLLM 抢同一条产品线。

## 1. 不做自研全能网关

**不做**：自建完整 LLM Proxy（路由、限流、密钥池、多厂商协议兼容）。

**原因**：LiteLLM / One API / New API 已经覆盖；重复造会拖垮排行榜主线。

**替代闭环**：

1. 企业流量继续走现有网关（推荐 LiteLLM）。
2. 用 SpendLogs callback / 定时 `POST /api/v1/sync/litellm`，或脚本推 `POST /api/v1/ingest/litellm`。
3. 在 tags / metadata / header 里带上 `tool`、`account_id`、`project_id`、`agent_id`。
4. 本服务只做 **归一入库 + 排行 / 自助 / 告警**。

## 2. 不做公网热度榜

**不做**：爬取或仿造 OpenRouter 式「全网模型热度排行」。

**原因**：那是舆论场产品；企业要的是 **内网真实用量**（人 / 部门 / 项目 / Agent）。

**替代闭环**：

1. 对外选型可外链 [OpenRouter Rankings](https://openrouter.ai/rankings) 或 Artificial Analysis。
2. 对内用本服务的 `/people` `/orgs` `/projects` `/agents` `/me`。
3. 需要「行业对照」时，把公开榜当 **参考链接**，不要把公网 token 混进私有库。

## 相关

- 本地探测：`scripts/probe_local_tools.py`、`docs/probe.md`
- ProCode 占位说明：页面 `/procode`。不解密 Canoryn 聊天库，健康检查保持 `placeholder`
- 能力总览：`docs/CAPABILITY.md`
