# 供应商余额探测报告（backend/balances.py）

> 探测时间：2026-10-20 23:40–23:55（Asia/Shanghai）
> 环境：Windows 10/11 + Python 3.14.7，纯标准库 `urllib.request`（无第三方依赖）
> 数据源：`~/.cc-switch/cc-switch.db` 的 `providers` / `provider_endpoints`，
> **只读打开**（`sqlite3.connect("file:...?mode=ro", uri=True)`，CC Switch 运行中安全）。
> 密钥处理：探测脚本从 `settings_config` 取真实密钥实发请求，但输出与本文**不含任何密钥或密钥片段**；
> 返回体只保留结构与字段名，数值示例会随账户消费变动。

## 一、结论总表

19 个 provider 去重后共 6 组「base_url + 密钥」实际发起了网络查询，其余直接降级。

| # | provider（name / app_type） | base_url（配置里） | adapter | 结论 | 依据 |
|---|---|---|---|---|---|
| 1 | deepseek / opencode | `https://api.deepseek.com` | deepseek | ✅ 可用 | `GET /user/balance` 200 |
| 2 | deepseek / claude | `https://api.deepseek.com/anthropic` | deepseek | ✅ 可用（与 #1 同一密钥） | 归一化到根域后 `GET /user/balance` 200 |
| 3 | deepseek / claude-desktop | `https://api.deepseek.com` | deepseek | ❌ 密钥被拒 | `GET /user/balance` 401（该条配置里是另一把已失效的密钥） |
| 4 | Kimi / opencode | `https://api.moonshot.cn/v1` | moonshot | ✅ 可用 | `GET /v1/users/me/balance` 200 |
| 5 | SiliconFlow / opencode | `https://api.siliconflow.cn/v1` | siliconflow | ❌ 接口已下线 | `GET /v1/user/info` 410 + `code=20092`（带有效密钥） |
| 6 | One API ×7（opencode/claude/claude-desktop/codex/gemini/openclaw/hermes） | `https://www.oneapi.work` | oneapi | ❌ 不可行 | 只开放推理接口；余额接口全部 404；面板接口 401「未登录」 |
| 7 | modelscope / opencode | `https://api-inference.modelscope.cn/v1` | unsupported | ❌ 无此能力 | 全部候选路径 404，平台不暴露计费余额 |
| 8 | cheap / codex | `https://api.cheaperinference.com/v1` | relay | ❌ 不可行 | 余额候选全 404（Next.js 站点），推理接口 200 |
| 9 | Claude Official / Claude Desktop Official / OpenAI Official / Google Official / Grok Official | 无 | unsupported | ❌ 未配置密钥（按契约不发请求） | `category=official`，`settings_config` 里无 key |

汇总：**成功 3 条**（DeepSeek 官方 ×2 + Kimi ×1），失败 16 条，其中 5 条是官方订阅制（无 API 余额概念、也没配 key）。
实际耗时约 4.5–9 秒（19 家串行，中转站内部并行探测；网络波动时偏慢，单家硬上限 8 秒）。

## 二、逐平台明细

### 1. DeepSeek 官方 ✅ 可用

- 接口：`GET https://api.deepseek.com/user/balance`（`GET /v1/user/balance` 同样 200）
- 请求头：`Authorization: Bearer <key>`、`Accept: application/json`
- 返回体结构（数值已脱敏为占位）：

```json
{"is_available": true,
 "balance_infos": [{"currency": "CNY",
                    "total_balance": "<总余额>",
                    "granted_balance": "<赠金>",
                    "topped_up_balance": "<充值余额>"}]}
```

- 解析：取 `balance_infos[0]`，`total_balance` 作为 balance，币种取 `currency`（实测为 CNY）。
- 实测细节：
  - 有 3 条 DeepSeek 配置，但**只有两把不同的密钥**；`opencode` 与 `claude` 共用一把（200），
    `claude-desktop` 是另一把，被平台判为无效：`401 Authentication Fails ... is invalid`。
    → 说明「同平台不同 provider 必须按密钥分别查询」，不能只按域名合并。
  - `claude` 那条的 base_url 带 `/anthropic` 后缀（`https://api.deepseek.com/anthropic`），
    直接拼 `/user/balance` 会 404，必须先归一化到根域。适配器里两个候选路径都试。
- 演示值：`总余额 ¥8.97（赠金 ¥0.00，充值 ¥8.97）`（金额随消费实时变化）。

### 2. Moonshot / Kimi ✅ 可用

- 接口：`GET https://api.moonshot.cn/v1/users/me/balance`
- 返回体结构：

```json
{"code": 0, "scode": "0x0", "status": true,
 "data": {"available_balance": "<可用余额>",
          "voucher_balance": "<赠金余额>",
          "cash_balance": "<现金余额>"}}
```

- 解析：`data.available_balance`；**大陆站 `api.moonshot.cn` 单位是 CNY**，国际站 `api.moonshot.ai` 才是 USD
  （官方接口同一个返回结构、币种跟着站点走，见 [CodexBar 的 Moonshot 说明](https://github.com/steipete/CodexBar/blob/main/docs/moonshot.md#1)）。
- 实测细节：`/v1/user/balance`、`/user/balance` 都是 404 —— CC Switch 的
  `meta.usage_script` 里那段「general 模板」用 `{{baseUrl}}/user/balance` 是错的（对 Kimi 不成立），不要照抄。
- 演示值：`可用余额 ¥14.85（赠金 ¥14.85，现金 ¥0.00）`。

### 3. SiliconFlow ❌ 接口已于 2026 年初下线

- 曾经的标准接口：`GET https://api.siliconflow.cn/v1/user/info`，返回
  `{"data": {"balance": ..., "chargeBalance": ..., "totalBalance": ...}}`（官方 OpenAPI 里仍留着这段定义）。
- 现在的真实行为（用有效密钥实测）：

| 请求 | 状态 | 返回 |
|---|---|---|
| `GET /v1/user/info`（有效 key） | **410** | `{"code":20092,"message":"This endpoint is deprecated and is no longer available.","data":null}` |
| `GET /v1/user/info`（无 key） | 401 | `{"code":30014,"message":"Token is invalid."}` |
| `/v1/user/info` POST、`/user/info`、`/v1/user/balance`、`/v1/account/info`、`/v1/account/balance`、`/v1/dashboard/billing/*`、`/v1/me` | 404 / 405 | — |
| `GET /v1/models`（有效 key） | 200 | 模型列表正常 → 说明密钥有效、网络通，是接口本身没了 |

- 官方公告（社区转发）：`user/info` 将于 2026 年初下线，`balance`、`total_balance` 字段先停用并返回 0，
  **替代接口「届时另行提供」**——截至探测时替代接口尚未公开。参见
  [LINUX DO 转发通知](https://linux.do/t/topic/1361671)、[SiliconFlow OpenAPI 定义](https://raw.githubusercontent.com/siliconflow/siliconcloud/main/openapi.yaml)。
- 结论：**不可行**，原因不是密钥/网络，而是平台下线了余额接口且未给替代。
  适配器保留实现，等官方恢复即可用；当前如实返回 `余额接口已下线（HTTP 410，code 20092）`。

### 4. One API 系中转站（`www.oneapi.work`）❌ 不可行（sk- 密钥拿不到余额）

这个站不是标准 New-API/One-API 面板，而是「Next.js 前端 + 自建网关」，实测证据：

| 请求（带有效 sk- 密钥） | 状态 | 返回要点 |
|---|---|---|
| `GET /v1/models` | 200 | 正常返回模型与价格 |
| `GET /api/health` | 200 | `{"status":"ok","timestamp":...,"models":{...}}`（公开健康检查） |
| `GET /api/dashboard` | **401** | `{"error":"未登录"}` ← 面板接口要**登录态**，不认 Bearer |
| `GET /api/keys` | **401** | `{"error":"未登录"}` |
| `GET /api/user/self` | 404 | 这是标准 New-API 的路径，本站没有 |
| `GET /api/status` | 404 | 不是标准 One-API 部署 |
| `GET /v1/dashboard/billing/subscription` | 404 | 网关明确回 `Unknown API path ... Available: /v1/chat/completions, /v1/models, /v1/images/*, /v1/messages, /v1/responses` |
| `GET /v1/dashboard/billing/usage` | 404 | 同上 |
| `GET /v1/user/balance`、`GET /user/balance` | 404 | 同上 / HTML 404 |
| `POST /api/user/self` 之类的面板兑换 | — | 面板接口统一 401「未登录」 |

- 结论：**用 `sk-` 密钥查不到余额**。该站把余额放在控制台账号体系后面
  （`/api/dashboard` 等接口只认登录 Cookie/JWT），API 网关本身只暴露推理路径，没有任何余额/额度端点。
  要拿余额，需要用户**先在浏览器登录 oneapi.work 控制台**，再把会话 Cookie（或面板 access_token）
  交给程序——这超出了「只读 CC Switch 配置」的边界，TokenLens 不应索取控制台登录凭据。
- 额外发现：7 条 One API 配置里有 2 条（`openclaw`、`hermes`）的 `apiKey` 是空字符串，
  即使站点支持也查不了，代码直接返回 `未配置密钥`。
- CC Switch 自己的 `meta.usage_script`（general 模板）也是 `{{baseUrl}}/user/balance`，
  对本站实测 404；且这几条配置的 `usage_script.enabled = false`，说明它也没在用。

### 5. ModelScope ❌ 无余额能力

- `GET https://api-inference.modelscope.cn/v1/models` 200（模型列表正常，密钥有效）。
- `GET /v1/user/info`、`/v1/account/balance`、`/api/v1/accounts`、`/api/v1/user` 全部 404。
- ModelScope 推理 API 是「社区模型调用 + 免费额度」模式，没有对外开放的账户余额/账单端点。
- 结论：**不可行**，如实降级为 `该平台未提供余额接口（ModelScope 推理 API 无计费端点）`，不发请求。

### 6. cheaperinference（`api.cheaperinference.com`）❌ 无公开余额接口

- `GET /v1/models` 200（密钥有效，但该主机较慢，单次 1–4 秒）。
- 余额候选全部 404，且返回的是 **Next.js 404 页面（HTML）**，不是 JSON：
  `/v1/user/balance`、`/user/balance`、`/v1/dashboard/billing/subscription`、`/api/user/self`、
  `/api/status`、`/api/v1/me`、`/api/credits`、`/v1/usage`。
- 站点前端是 Next.js（`ci-theme`），bundle 里能翻到的后端路由只有
  `/api/billing/checkout-session/…`、`/api/errors/report`、`/api/marketing/attribution`，**没有余额路由**；
  余额是账户级数据、走 Stripe 充值 + 登录态，属于服务端渲染，`sk-` 密钥碰不到。
- 结论：**不可行**。代码按通用中转站探测（5 条候选路径并行，约 1–2 秒），失败后如实报
  `未找到可用余额接口（/v1/dashboard/billing/subscription→404、/api/user/self→404、…）`。

### 7. 官方订阅制（5 条）❌ 未配置密钥

`Claude Official` / `Claude Desktop Official` / `OpenAI Official` / `Google Official` / `Grok Official`：
`category = "official"`，`settings_config` 里只有空的 `{"env":{}}` / `{"auth":{},"config":""}`。
按契约**不发请求**，直接 `ok: false, error: "未配置密钥"`（官方订阅是网页/CLI 额度，本来也没有 API 余额接口）。

## 三、适配器与归一化规则

| adapter | 触发条件 | 请求路径 | 币种 |
|---|---|---|---|
| `deepseek` | host 含 `deepseek.com` | 根域 `/user/balance`，带 `/anthropic` 等后缀时先试原路径再退根域 | CNY（取返回 `currency`） |
| `moonshot` | host 含 `moonshot` | `{base}/users/me/balance`（base 以 `/v1` 结尾）或根域 `/v1/users/me/balance` | `.cn` → CNY，`.ai` → USD |
| `siliconflow` | host 含 `siliconflow` | `/v1/user/info`（已 410，保留待恢复） | CNY |
| `openrouter` | host 含 `openrouter` | `/api/v1/credits`，balance = `total_credits - total_usage` | USD |
| `oneapi` | host 含 `oneapi` / `one-api` / `newapi` | 面板/计费候选路径并行探测 | 按返回 |
| `relay` | 其余未知 host | 同上（通用中转站探测） | 按返回 |
| `unsupported` | ModelScope、或抽不到 base_url | 不发请求 | — |

`settings_config` 的 5 种形态统一由 `extract_credentials()` 处理（env / npm options / 扁平 key / codex TOML / provider_endpoints 兜底），
实测 19 条全部能正确抽出 base_url 与密钥（5 条官方除外）。

## 四、自测与复现

```bat
cd D:\code\tokenlens
python backend\balances.py
```

输出（2026-10-20 实测，金额脱敏为实际值；表格列：name / app_type / adapter / ok / balance / currency / detail / error）：

```text
TokenLens 供应商余额查询（只读 cc-switch 配置，密钥不落盘）
name                     app_type        adapter      ok     balance  currency  detail                                      error                                                                     
------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
deepseek                 claude-desktop  deepseek     False  -        -         -                                           密钥无效或已过期（401）
deepseek                 claude          deepseek     True   8.97     CNY       总余额 ¥8.97（赠金 ¥0.00，充值 ¥8.97）      -
deepseek                 opencode        deepseek     True   8.97     CNY       总余额 ¥8.97（赠金 ¥0.00，充值 ¥8.97）      -
cheap                    codex           relay        False  -        -         -                                           未找到可用余额接口（/v1/dashboard/billing/subscription→404、/api/user/sel…
Kimi                     opencode        moonshot     True   14.85    CNY       可用余额 ¥14.85（赠金 ¥14.85，现金 ¥0.00）  -
modelscope               opencode        unsupported  False  -        -         -                                           该平台未提供余额接口（ModelScope 推理 API 无计费端点）
SiliconFlow              opencode        siliconflow  False  -        -         -                                           余额接口已下线（HTTP 410，code 20092）
Claude Desktop Official  claude-desktop  unsupported  False  -        -         -                                           未配置密钥
Claude Official          claude          unsupported  False  -        -         -                                           未配置密钥
OpenAI Official          codex           unsupported  False  -        -         -                                           未配置密钥
Google Official          gemini          unsupported  False  -        -         -                                           未配置密钥
Grok Official            grokbuild       unsupported  False  -        -         -                                           未配置密钥
One API                  hermes          oneapi       False  -        -         -                                           未配置密钥
One API                  openclaw        oneapi       False  -        -         -                                           未配置密钥
One API                  opencode        oneapi       False  -        -         -                                           中转站面板接口需登录态（401），API 密钥无法查询余额（/v1/dashboard/billin…
One API                  claude-desktop  oneapi       False  -        -         -                                           中转站面板接口需登录态（401），API 密钥无法查询余额（/v1/dashboard/billin…
One API                  claude          oneapi       False  -        -         -                                           中转站面板接口需登录态（401），API 密钥无法查询余额（/v1/dashboard/billin…
One API                  codex           oneapi       False  -        -         -                                           中转站面板接口需登录态（401），API 密钥无法查询余额（/v1/dashboard/billin…
One API                  gemini          oneapi       False  -        -         -                                           中转站面板接口需登录态（401），API 密钥无法查询余额（/v1/dashboard/billin…
------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
共 19 家 / 成功 3 / 失败 16 / 总耗时 8618 ms
数据库：~/.cc-switch/cc-switch.db（只读 mode=ro）
说明：api_key 仅在内存中用于请求，本表与返回结构不含任何密钥或密钥片段。
```

单条成功返回样例（结构来自 `/api/balances` 的 items，密钥不出现）：

```json
{"id": "a2613a4c-6046-481e-9dc3-bc074c15c240", "name": "deepseek",
 "app_type": "claude", "base_url": "https://api.deepseek.com/anthropic",
 "adapter": "deepseek", "ok": true, "balance": 8.97, "currency": "CNY",
 "detail": "总余额 ¥8.97（赠金 ¥0.00，充值 ¥8.97）", "error": null, "latency_ms": 473}
```

## 五、实现要点与坑（给 server.py / 前端）

1. **按密钥去重，不是按域名**：DeepSeek 3 条配置共用域名但只有 2 把密钥，其中 1 把已失效；
   去重键是 `(base_url, api_key)`，中转站额外按 `origin` 归一（`…/v1` 与裸地址算同一家）。
2. **单家预算 8 秒**：每个适配器拿一个 deadline，串行 19 家实测总耗时约 4.5–9 秒（多数 5 秒上下，随网络波动）；
   中转站的 5 条候选路径用 `ThreadPoolExecutor` 并行发，耗时 ≈ 最慢一条，而不是相加。
3. **失败绝不抛异常**：`query_all` 最外层 try/except，单家适配器异常也降级为 `ok:false`；
   入参 `None` / 空 / 非 list / 坏 JSON 全部安全。
4. **密钥零外泄**：api_key 只在 `_request()` 里拼 `Authorization` 头；返回结构、错误文案、
   表格输出、本文件都不含密钥或片段（`detail`/`error` 只用状态码和平台名）。
5. **不要相信 CC Switch 的 `usage_script` 模板**：它对 Kimi/One API 用 `{{baseUrl}}/user/balance`，
   实测 404；模板只是候选，必须以真实探测为准。
6. **中转站基本都要控制台登录态**：如果产品以后想支持，必须在设置页让用户自己填
   「面板 access_token / Cookie」，并且只留在本机内存/本地配置；不能用 `sk-` 密钥硬试。
7. **SiliconFlow 会自己恢复**：官方说替代接口另行通知；届时把 `/v1/user/info` 换成新路径即可，
   适配器和解析逻辑已就位（410 目前按「已下线」如实展示）。
8. **不要再探测 `/api/user/self` 之外的账号接口**：`/api/dashboard`、`/api/keys` 会 401「未登录」，
   每次多打一次就多几百毫秒；本模块只把它作为「需要登录态」的判据，不做登录。
9. **金额精度**：统一转 float，展示保留 2 位小数；Kimi 的 `available_balance` 原始值有 5 位小数，
   `balance` 字段返回原始 float，`detail` 里四舍五入到分。
