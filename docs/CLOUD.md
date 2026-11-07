# 把 TokenLens 搬到云端：给 muse（或其他 AI/工程师）的迁移指南

这份文档的目的很直接：**TokenLens 的界面、图表、交互已经验证过了，不要重写；
你只需要换掉「数据从哪来」这一层，剩下的照抄。**

读完之后你应该能回答三个问题：

1. 这个仪表盘的领域模型是什么？（一条「请求事件」长什么样）
2. 哪些代码可以直接复用，哪些必须重写？
3. 云端版多出来的那些坑（鉴权、多用户、时区、缓存）怎么处理？

---

## 1. 一句话架构

```
             ┌──────────────────────────────────────────────┐
  数据来源    │  source_local.py  本机文件（CC Switch /      │
             │                   ~/.claude / ~/.dsh /…）      │
             │  source_demo.py   内置示例（合成数据）        │
             │  source_cloud.py  ← 你要写的（Postgres/S3）   │
             └───────────────────┬──────────────────────────┘
                                 │  7 个方法，返回纯 JSON
                                 ▼
                  server.py   只做路由 + 静态文件
                                 │
                                 ▼
                  web/        原生 JS + 手写 SVG 图表（照抄）
```

关键设计：`server.py` **不认识任何具体数据源**。它只调用
`setup(source)` 注入进去的对象。所以云端版 = 新写一个 `source_cloud.py`，
其他一行都不用改。

---

## 2. 领域模型：一条请求事件

这是整个项目唯一需要你理解透的数据结构。所有图表、排行、成本、会话还原，
都是对列表里这些事件做聚合得来的。

| 字段 | 类型 | 含义 |
|---|---|---|
| `ts` | int | 事件时间，**毫秒**（Unix epoch ms） |
| `app` | str | 客户端标识，如 `dsh` / `claude` / `codex` |
| `provider` | str | 供应商 id，如 `deepseek` / `anthropic` |
| `model` | str | 模型 id，如 `deepseek-v4-pro` |
| `input` | int | 输入 token（未命中缓存的部分） |
| `output` | int | 输出 token |
| `cache_read` | int | 命中缓存读取的 token |
| `cache_creation` | int | 写入缓存的 token |
| `cost_usd` | float | 该次调用成本，美元 |
| `latency_ms` | int \| null | 端到端延迟；失败请求可以是 null |
| `status` | int \| null | HTTP 状态码；null 表示本地记录无法判定 |
| `session_id` | str | 会话 id，用于把「请求」串成「对话」 |
| `source` | str | 这条记录来自哪种采集方式（见下） |

### 几个必须想清楚的口径

这些是 TokenLens 踩过坑之后定下来的规则，**云端版请保持一致**，
否则数字对不上、用户会怀疑整个仪表盘：

1. **`input` 不包含缓存部分。** 真实 prompt 长度 =
   `input + cache_read + cache_creation`。
   API 里那个 `prompt_tokens` 字段就是这个和。
2. **缓存命中率 = `cache_read / (input + cache_read + cache_creation)`**，
   分母为 0 时返回 `null`（不要返回 0——0 的意思是「一个缓存都没命中」，
   和被测不是一回事）。前端要能区分这两种状态。
3. **成本优先用供应商返回的实测值**；只有拿不到时才用价格表反算。
   价格表反算要按 `input / output / cache_read / cache_creation` 四段分别计价。
4. **去重按会话为单位，不要按请求。** TokenLens 早期同时读了
   「代理日志」和「客户端自己的会话文件」，同一个请求被算了两遍。
   现在的规则是：代理日志覆盖到的会话，原生解析就不再补；没覆盖到的才补。
   云端多条采集链路并存时，**同样的问题一定会再出现**——建议在入库层
   直接用 `(session_id, provider, model, ts 桶)` 做幂等键。
5. **失败的请求也要入库。** 它占请求数、拉低成功率、可能产生少量计费，
   但不应该进延迟分位数（`latency_ms` 置 null）。

---

## 3. 你要实现的数据源接口

新建 `backend/source_cloud.py`，导出 `SOURCE` 对象。七个方法是**必需**的：

```python
class CloudSource:
    name = "cloud"

    def meta(self) -> dict:                  # 数据源状态、覆盖区间、有哪些 app/model
    def overview(self, f: dict) -> dict:     # 首页全部图表数据（一次请求返回）
    def list_requests(self, f: dict) -> dict # 请求明细，分页 + 筛选 + 排序
    def requests_csv(self, f: dict) -> str   # CSV（要带 BOM，Excel 才不乱码）
    def chats_list(self, f: dict) -> dict    # 会话列表
    def chat_detail(self, sid: str) -> dict|None  # 单个会话的完整对话
    def chat_markdown(self, sid: str) -> str|None

    # 可选但强烈建议：
    def chat_export_json(self, sid) -> str|None
    def export_all_chats(self) -> bytes      # zip
    def providers(self) -> list              # 供应商列表（不含凭据！）
    def balances(self, force=False) -> dict  # 账号余额
    def get_settings(self) -> dict
    def save_settings(self, patch) -> dict
    def warm_up(self) -> None                # 可做缓存预热/建索引
```

### 最省力的做法

`backend/source_demo.py` 里的 `DemoSource` 已经把「事件列表 →
overview / 分页 / CSV / 会话还原」全部写好了，而且**不依赖任何数据源**。
你只要把 `generate_events()` 换成「从你的数据库查出来」，其余原样保留：

```python
from source_demo import DemoSource

class CloudSource(DemoSource):
    name = "cloud"

    def events(self):                      # 只需覆盖这一个方法！
        rows = pg_query("SELECT ts, app, provider, model, input, output, "
                        "cache_read, cache_creation, cost_usd, latency_ms, "
                        "status, session_id, source FROM calls "
                        "WHERE tenant = %s ORDER BY ts", (self.tenant,))
        return [to_event(r) for r in rows]
```

> ⚠️ 注意：`DemoSource` 是全量在内存里聚合的，适合「几万条以内」。
> 到了百万级以上，`overview()` 必须下推到 SQL 里做
> （`GROUP BY date_trunc('day', ts)`），否则每次刷新都全表扫描。
> 见第 6 节。

---

## 4. 接口契约（前端只认这些字段）

完整字段定义在 [API.md](API.md)。云端版**保持字段名完全一致**，
`web/` 就一行都不用改。最容易写错的几个：

- `from` / `to` 是**毫秒**时间戳（不是秒，不是 ISO 字符串）
- `overview.by_app[].models` 里每项要有 `model / requests / tokens / cost_usd`
- `overview.series_by_app[]` 是**按桶对齐的三条数组** `requests[] / tokens[] / cost[]`，
  长度必须与 `overview.series[]` 相同（前端按索引对齐画堆叠图）
- `overview.prev` 是**上一个等长窗口**的汇总，用于 KPI 环比
- `latency_buckets` 固定六档：`<1s / 1–3s / 3–5s / 5–10s / 10–30s / >30s`

自检脚本会逐条验证这些：

```bash
python tools/smoke_test.py            # 示例数据
python tools/smoke_test.py --local    # 本机数据
# 换成你的：
python -c "
import sys; sys.path.insert(0,'backend'); sys.path.insert(0,'tools')
import server, smoke_test_lib"       # 或直接在 smoke_test.py 里改 SOURCE
```

**建议：先把 `smoke_test.py` 跑绿，再动前端。** 这一步能省掉 90% 的联调时间。

---

## 5. 可以直接复用 / 必须重写的部分

| 模块 | 处理 |
|---|---|
| `web/js/charts.js`（手写 SVG 图表库） | ✅ 直接复用，零依赖，面积/堆叠/柱状/环形/迷你线都有 |
| `web/css/styles.css`（设计系统） | ✅ 直接复用 |
| `web/js/app.js`（路由/页面/Markdown/导出） | ✅ 直接复用；只有「本地」措辞要按 `meta.source.mode` 调整 |
| `backend/sources.py`（本机文件解析） | ❌ 云端用不上（没有 `~/.claude` 这种东西） |
| `backend/source_demo.py` 的聚合逻辑 | ✅ 直接复用（这是最值钱的一段） |
| `backend/balances.py`（余额适配器） | ⚠️ 谨慎：云端多用户场景下**别**做余额查询 |
| `app.py`（WebView2 桌面窗口） | ❌ 云端不需要，浏览器访问即可 |

---

## 6. 云端一定会遇到、本地版本不存在的坑

### 6.1 聚合下推到数据库

本地版每次刷新都全量读一遍文件，几千到几万行没问题。
云端要按这个思路改：

```sql
-- 日桶序列（overview.series）
SELECT date_trunc('day', ts) AS bucket,
       count(*)                AS requests,
       sum(input_tokens)        AS input_tokens,
       ...
FROM calls
WHERE tenant_id = $1 AND ts >= $2 AND ts < $3
GROUP BY 1 ORDER BY 1;
```

注意 **`date_trunc` 用的是数据库时区**。用户在中国但库在 UTC 时，
「今天」的桶会差 8 小时，图表上会出现一天没数据、另一天翻倍。
要么统一 `SET TIME ZONE`，要么显式 `date_trunc('day', ts AT TIME ZONE 'Asia/Shanghai')`。

### 6.2 缓存

本地版有一层 4 秒的内存索引缓存（`INDEX_TTL`）。
云端多实例部署时，`overview` 这类重查询一定要加 **Redis 或进程内 TTL 缓存**，
键里带上 `tenant + 时间范围 + 筛选条件`。否则 20 个人同时打开首页就是 20 次重聚合。

### 6.3 鉴权与多租户（本地版完全没有）

- 本地版靠 `127.0.0.1` 保证安全；云端**必须**自己做登录。
- 每个查询都要带 `tenant_id` 过滤，**不要信任前端传的 tenant**，
  从会话/JWT 里取。
- `provider` 表的 `settings_config` 里可能有 API 密钥。
  本地版是「只在内存用、绝不返回前端」；云端版要在数据库层面就加密或干脆不存。

### 6.4 余额接口

`/api/balances` 会拿真密钥去请求官方接口。云端多用户场景：
**默认关掉**，或者改成用户自己填 key 存在加密保险箱里按需查。
这部分不建议交给 AI 自动生成。

### 6.5 数据体量与分页

`list_requests` 本地版每页最多 500 条。云端建议：
- 分页字段改成游标（`ts` + `id`），别用 `OFFSET`（深翻页会越来越慢）
- 明细页默认只返回最近 24h

### 6.6 采集（这是云端版真正的工作量）

本地版是「被动读别人写好的文件」。云端要**主动**采集，三条路：

1. **网关/代理拦截**（最通用，Helicone 就是这么干的）：所有客户端指向你的代理，
   你在转发时记录 usage 后入库。好处是 Cursor / 桌面客户端也能覆盖。
2. **各家 API 的 usage 账单接口**：精度高但滞后，适合做对账。
3. **自研 Agent 自己的调用日志**：如果 muse 本身就是 agent 平台，
   直接在 LLM 调用那一层加一行 `emit_request_event(...)` 最干净。

**强烈建议第 3 条 + 第 1 条组合**：自研的走埋点，第三方的走代理。

---

## 7. 上线清单

- [ ] `smoke_test.py` 全绿（换成云端数据源后）
- [ ] 所有查询都带 tenant 过滤，且 tenant 来自鉴权信息而非前端参数
- [ ] `/api/balances` 已关闭或做了严格鉴权
- [ ] `overview` 有缓存，且缓存键覆盖全部筛选维度
- [ ] 时区明确（`date_trunc` 与前端展示时区一致）
- [ ] 前端资源有 `Cache-Control`（现在本地版是 `no-store`，云端要改，不然每次刷新都全量下载）
- [ ] 加了限流（首页一次请求会触发多次聚合）
- [ ] 有健康检查端点（`/api/meta` 即可）
- [ ] 数据库连接池 + 超时
- [ ] 埋了「数据滞后」指标（CC Switch 那种滞后问题，云端同样会有）

---

## 8. 如果 muse 是 AI Agent，这份文档怎么用

把这几份文件喂给它即可，按顺序：

1. `docs/CLOUD.md`（本文）—— 讲清楚要改哪一层
2. `docs/API.md` —— 字段契约，一字不改地实现
3. `backend/source_demo.py` —— 让它照着写 `source_cloud.py`
4. `tools/smoke_test.py` —— 让它拿这个当验收标准

最有保障的工作顺序：

```
先让 smoke_test.py 在「假数据源」上通过
      ↓
再换成「真实数据库查询」，仍然要求 smoke_test.py 通过
      ↓
最后才动前端/部署
```

这样即使 AI 写歪了，也会在自检这一步暴露，而不是上线后才发现数字不对。
