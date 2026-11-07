# TokenLens 内部契约

本地单机应用。后端 `python backend/server.py`，监听 `127.0.0.1:8787`，仅本机可访问。
前端 `web/` 静态资源。数据只读，绝不修改用户的 cc-switch / Claude Code 配置。

## 数据源

| 源 | 路径 | 用途 |
|---|---|---|
| CC Switch 代理日志 | `~/.cc-switch/cc-switch.db` → `proxy_request_logs` | 逐请求 token/成本/延迟（图表主数据） |
| CC Switch 供应商 | 同库 `providers` / `model_pricing` | 供应商元信息、余额凭据、价格 |
| Claude Code 会话 | `~/.claude/projects/**/*.jsonl` | 聊天记录 / 会话级用量 |
| Claude Code 历史 | `~/.claude/history.jsonl` | 全局 prompt 历史 |

`proxy_request_logs.created_at` 是 **Unix 秒**。

## HTTP API

统一前缀 `/api`，响应 `application/json; charset=utf-8`。错误：`{"error": {"code","message"}}` + 4xx/5xx。

### GET /api/meta
```json
{
  "app": {"name":"TokenLens","version":"1.0.0"},
  "sources": {
    "cc_switch": {"path":"...","ok":true,"rows":3083,"mtime":1791...},
    "claude":    {"path":"...","ok":true,"sessions":50,"files":50}
  },
  "range": {"min": 1790766548000, "max": 1791723987000},
  "apps": ["claude","codex","opencode"],
  "models": ["deepseek-flash","..."],
  "settings": {"port":8787,"auto_refresh_sec":30}
}
```

### GET /api/overview?from&to&app&model&status&q
`from`/`to` 为 epoch **毫秒**（可选）。返回：
```json
{
  "range": {"from":..., "to":..., "granularity":"hour|day"},
  "totals": {
    "requests":3083,"errors":44,"success_rate":0.986,
    "input_tokens":0,"output_tokens":0,"cache_read_tokens":0,"cache_creation_tokens":0,
    "prompt_tokens":0,"cost_usd":6.73,
    "cache_hit_rate":0.87,
    "avg_latency_ms":0,"p50_latency_ms":0,"p95_latency_ms":0,
    "first_token_avg_ms": null
  },
  "prev": { "requests":.., "cost_usd":.., "cache_hit_rate":.., "avg_latency_ms":.. },
  "series": [{"t":1790766548000,"requests":12,"errors":0,"cost_usd":0.01,
              "input_tokens":0,"output_tokens":0,"cache_read_tokens":0,
              "cache_creation_tokens":0,"avg_latency_ms":1200}],
  "by_app":    [{"key":"claude","name":"Claude Code","requests":1144,"cost_usd":3.0,"tokens":0,"errors":0,
                 "token_share":0.012,"cost_share":0.09,"cache_hit_rate":0.99,"last_ts":1791723987000,
                 "models":[{"model":"deepseek-flash","requests":1120,"tokens":0,"cost_usd":0.8}]}],
  "series_by_app":[{"app":"dsh","name":"DeepSeek Harness",
                    "requests":[0,1,2],"tokens":[0,1000,2000],"cost":[0,0.01,0.02]}],
  "by_model":  [{"key":"deepseek-flash","requests":1120,"cost_usd":..,"input":..,"output":..,"cache_read":..,"cache_creation":..,"errors":..}],
  "by_provider":[{"key":"_session","name":"Claude Code 会话日志","requests":1112,"cost_usd":..}],
  "status_codes":[{"code":200,"count":3039}],
  "latency_buckets":[{"label":"<1s","count":10}]
}
```
`cache_hit_rate = cache_read / (input + cache_read + cache_creation)`，分母 0 时为 null。
`prev` 是同长度前一周期的聚合，用于 KPI 环比。

### GET /api/requests?from&to&app&model&status&q&limit&offset&order
```json
{"total":3083,"rows":[{
  "id":"...","ts":1791723987000,"app":"claude","provider":"_session",
  "provider_name":"Claude Code 会话日志","model":"deepseek-flash",
  "input_tokens":24519,"output_tokens":546,"cache_read_tokens":0,"cache_creation_tokens":0,
  "cost_usd":0.0080109,"latency_ms":3627,"first_token_ms":null,
  "status":200,"error":null,"session_id":"...","streaming":1,"source":"session_log"
}]}
```
`order` ∈ `ts|cost|latency|tokens`，前缀 `-` 表示倒序，默认 `-ts`。

### GET /api/requests/export?<同筛选>  → text/csv 下载

### GET /api/chats?q&limit&offset&source
`source` ∈ `claude|dsh`（省略为全部）。`id` 为 `dsh:<sid>` 时走 DeepSeek Harness 解析。
```json
{"total":164,"rows":[{
  "id":"dsh:92ac8be0-...","source":"dsh","source_label":"DeepSeek Harness",
  "title":"...","project":"~/project",
  "models":["deepseek-flash"],"created":1791...,"updated":1791...,
  "requests":12,"input":0,"output":0,"cache_read":0,"cache_creation":0,"cost_usd":0.12,
  "messages":24
}]}
```

### GET /api/chats/{id}
```json
{"id":"...","title":"...","project":"...","models":["..."],"created":...,"updated":...,
 "usage":{"input":0,"output":0,"cache_read":0,"cache_creation":0},
 "messages":[{
   "role":"user|assistant|system","ts":1791...,"model":"...",
   "usage":{"input":0,"output":0,"cache_read":0,"cache_creation":0},
   "blocks":[{"type":"text","text":"..."},{"type":"thinking","text":"..."},
             {"type":"tool_use","name":"Bash","input":{}},
             {"type":"tool_result","text":"...","is_error":false}]
 }]}
```

### GET /api/chats/{id}/export?format=md|json → 文件下载

### GET /api/balances?refresh=1
```json
{"checked_at":1791...,"items":[{
  "id":"deepseek","name":"deepseek","app_type":"claude","base_url":"https://api.deepseek.com",
  "adapter":"deepseek","ok":true,"balance":12.34,"currency":"CNY",
  "detail":"余额 12.34 CNY（赠金 0）","error":null,"latency_ms":120
}]}
```
凭据来自 `providers.settings_config`，**只在后端内存使用，绝不返回给前端**。

### GET/PUT /api/settings
`{"port":8787,"auto_refresh_sec":30,"balance_refresh_sec":600}`

## 后端模块边界

`server.py` **不认识任何具体数据源**，所有数据都来自 `setup()` 注入的对象。
一个数据源（source）必须提供下面这些方法，返回值结构就是本文档各端点描述的结构：

```python
meta() -> dict
overview(filters) -> dict
list_requests(filters) -> dict
requests_csv(filters) -> str            # 带 UTF-8 BOM
chats_list(filters) -> dict
chat_detail(session_id) -> dict | None
chat_markdown(session_id) -> str | None
# 建议提供
chat_export_json(session_id) -> str | None
export_all_chats() -> bytes             # zip
providers() -> list                     # 不含 settings_config
balances(force=False) -> dict
get_settings() -> dict
save_settings(patch) -> dict
warm_up() -> None
```

`filters` 是同一个 dict：`from / to / app / model / status / q / limit / offset / order / source`，
其中 `from`、`to` 为 epoch **毫秒**。

现有实现：

- `backend/source_local.py`：本机数据，全部委托给 `sources.py`（默认）
- `backend/source_demo.py`：内置示例数据（`--demo`），也是对外的参考实现

- `backend/sources.py`：读取 cc-switch / Claude Code / DS Harness / Antigravity，产出上述结构。
- `backend/balances.py`：**唯一职责**是「从 settings_config 抽凭据 + 按平台查余额」，
  导出 `query_all(provider_rows) -> list[dict]`，每项结构见 `/api/balances` 的 items。
  纯标准库（urllib），超时 ≤8s/家，失败必须降级为 `ok:false` + `error`。
- `backend/server.py`：路由、静态资源、导出。只调用 source 的方法。

### 新增字段

`/api/meta` 增加了一个 `source` 字段，描述当前数据源模式（前端据此调整措辞）：

```json
{"source": {"name": "demo", "read_only": true, "mode": "demo"}}
```

`mode` ∈ `local | demo | cloud`（云端实现自定义即可）。
