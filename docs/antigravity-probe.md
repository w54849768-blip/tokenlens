# Antigravity 会话库 Token 用量逆向记录

> 目标：从 Google Antigravity IDE 的本地 SQLite 会话库里解析出每次模型生成的 token 用量。
> 结论：**解出来了**。用量不在标准 Gemini `UsageMetadata` 里，而在 `steps.metadata`
> 的 `field 9` 嵌套 message 中；已用 Antigravity 自己写出的 transcript 日志逐条核对，
> 当前机器上 **9/9 完全一致**。
>
> 实现：`backend/antigravity.py`，导出 `scan() -> list[dict]`，纯标准库、只读、不抛异常。

---

## 1. 数据源

| 路径（相对 `~/.gemini/antigravity`） | 大小 / 行数 | 说明 |
|---|---|---|
| `conversations/37c8ec57-….db` | 753,664 B | 主会话库（本次唯一一个 db） |
| `conversations/37c8ec57-….db-wal` | 0 B | WAL 已空（数据已 checkpoint 进主库） |
| `conversation_summaries.db` | 61,440 B / 1 行 | 只有标题、预览、时间；**不含 token** |
| `brain/<cascade>/.system_generated/logs/transcript_full.jsonl` | 42,216 B | **不是 SQLite**，但含 `input_tokens/cache_read_tokens/output_tokens`，本次用作交叉验证基准 |
| `agyhub_summaries_proto.pb` | 344 B | 会话摘要 protobuf，只有标题/时间，无 token |
| `executor_metadata`（库内表） | 8,302 B / 1 行 | 执行器/模型配置，无 token |

`conversations/*.db` 表结构（`sqlite_master` 实测）：

```
trajectory_meta(trajectory_id, cascade_id, trajectory_type, source)           rows=1
steps(idx, step_type, status, has_subtrajectory, metadata, error_details,
      permissions, task_details, render_info, step_payload, step_format)     rows=18
gen_metadata(idx, data, size)                                                rows=9
executor_metadata(idx, data)                                                 rows=1
parent_references(idx, data)                                                 rows=0
trajectory_metadata_blob(id, data)                                           rows=1
battle_mode_infos(idx, data)                                                 rows=0

conversation_summaries.db:
conversation_summaries(conversation_id, title, preview, step_count,
                       last_modified_time, workspace_uris, status, source,
                       project_id, agent_name, parent_conversation_id,
                       nesting_depth, battle_id, winning_conversation_id,
                       not_fully_idle, killed, last_user_input_time,
                       last_user_input_step_index, app_data_dir, raw_summary,
                       group_id)                                             rows=1
```

`steps` 的取值分布（本机样本）：

* `step_type`：`14` = 用户输入，`15` = 模型生成（PLANNER_RESPONSE），`132` = 工具调用；
* 18 个 step 里，9 个 `15` 带 token 用量，9 个 `132` 不带；
* 除 `idx/step_type/status/step_count/size` 等整型列外，其余列都是 protobuf 二进制。

**只读访问方式**：`sqlite3.connect("file:…?mode=ro", uri=True)`，全程不写、不加锁。

---

## 2. 探测方法

1. 用 `sqlite3` 只读打开，统计每张表每列的大小（`steps.step_payload` 合计 82 KB、
   `gen_metadata.data` 合计 115 KB，是主要信息载体）。
2. 自写极简 protobuf 解析器（`struct`/手写 varint，不用 protobuf 库）：
   * 解析 tag/wire type，支持 varint / length-delimited / 32bit / 64bit；
   * 对 length-delimited 字段递归尝试当嵌套 message 解析，同时识别可读 UTF-8 文本；
   * 把所有 blob 展开成「字段号 / 线型 / 值 / 嵌套深度」树（共 57 个 blob、229 KB）。
3. 按「usage 三元组」筛候选：嵌套 message 内同时有 3 个整型字段、且满足
   `总额 = 两项之和`、量级 10²~10⁶。
   * 共扫出 45 个候选，绝大多数是配置噪声（`8192/1024/200000`、`4096/2048`、
     `1/20/100` 这类模型参数）；
   * **没有**任何一个候选满足任务里给的先验判据 `field3 == field1 + field2`
     （标准 Gemini `UsageMetadata` 布局），说明这个产品没有直接落标准用量结构。
4. 逐个读 `steps.metadata` 的嵌套 message，发现 `field 9` 是每次模型生成独有、
   且 9 条取值连续、量级合理的结构。
5. **决定性验证**：把 `field 9` 的取值和 `transcript_full.jsonl` 里的
   `input_tokens / cache_read_tokens / output_tokens` 逐条比对 → 9/9 完全一致，
   映射关系由此钉死（见第 4 节对照表）。

> 说明：`transcript_full.jsonl` 只是**外部证据**，`scan()` 不读它（任务数据源是 SQLite）；
> 它证明的是「db 里这几个字段到底对应什么」，而不是给 `scan()` 提供数据。

---

## 3. 切出来的字段树

### 3.1 `steps.metadata`（以 `idx=1`，350 B 为例）

```
field 1  len(12)  message Timestamp {1: 1792494577, 2: 453109000}   ← 步骤创建时间(UTC)
field 3  varint   2
field 6  len(12)  message Timestamp {...}
field 7  len(11)  message Timestamp {...}
field 8  len(11)  message Timestamp {...}
field 9  len(118) message UsageLike {          ← ★ 用量就在这里
           field 1   varint 1318                ← 常量，语义未确证
           field 2   varint 7603                ← ★ 输入 token
           field 3   varint 429                 ← ★ 输出 token
           field 5   （缺省；命中缓存时才出现）     ← ★ 缓存命中输入 token
           field 6   varint 24                  ← 常量，语义未确证
           field 7   len(40) text 'bot-ff657a4c-…'      请求端 bot id
           field 8   len(33) message {1:'sessionID', 2:'-3750763034362895579'}
           field 9   varint 371                 ← 输出 token 的二级拆分
           field 10  varint 58                  ← 输出 token 的二级拆分（9+10 == 3）
           field 11  len(23) text '8kvXau_ROau20-kPpYrpyQ0'   请求 token
         }
field 11 varint   1318
field 12 len(36)  text '<uuid>'
field 20 len(78)  message {1: trajectory_id, 2: step_idx, 4: cascade_id}
field 26 len(35)  message ...                  ← 子步骤耗时/时间戳
field 32 len(11)  message Timestamp ...
```

### 3.2 `gen_metadata.data`（以 `idx=0`，1,270 B 为例）

```
field 2  len(2)   bytes
field 4  len(36)  text '<uuid>'
field 8  len(520) bytes
field 1  len(702) message {
           field 3   varint 1318
           field 4   len(118) message UsageLike { ... }   ← ★ 与 steps.metadata.field9 同一份数据
           field 9   message {...}                        ← 上下文窗口配置（含 256000 等值）
           field 11/12  message（耗时/时间）
           field 17  message {...}
           field 19  len(18) text 'gemini-3.8-flash-n'     ← ★ 模型名字符串
           field 20  重复 message {1: key, 2: value}        ← request_id / cascade_id / model_enum 等
         }
```

`field 20` 的 key/value 里可读到：`trajectory_id`、`cascade_id`、`root_cascade_id`、
`request_id`（形如 `<trajectory_id>-<生成序号>`）、`last_step_index`、
`model_enum=MODEL_PLACEHOLDER_M318`、`used_claude=false` 等。

> `gen_metadata` 最后一行（`idx=8`，104,830 B）是个「会话快照」：`field 1` 里塞了
> 全部消息、工具定义（12 个 tool schema）和系统提示词（identity/artifacts/…）。
> 它的 `field 1.field 4` 仍是最后一次生成的用量，位置与其它行一致；
> 配置子结构 `field 3.field 28` 里还能读到用户选择的模型 `gemini-3.8-flash-high`。

### 3.3 时间戳

`metadata.field 1` 是标准 `google.protobuf.Timestamp`：

```
{ field 1: seconds, field 2: nanos }     // UTC
1792494577.453109000  →  2026-10-20T11:09:37.453Z
```

与 `transcript_full.jsonl` 的 `created_at`（`2026-10-20T11:09:37Z`）逐条对到秒。
`ts`（毫秒） = `seconds * 1000 + nanos // 1_000_000`。

---

## 4. 判据与验证（最硬的一环）

### 4.1 候选结构的「自洽」判据

`field 9` 结构在所有 9 条生成上满足：

* `field 2`、`field 3` 均存在且为正，量级 10²~10⁴；
* `field 9 + field 10 == field 3`（9/9 成立，如 `371+58=429`、`841+721=1562`）；
* `field 5` 只在第 9 次生成出现，值 `16462`；
* `field 1 = 1318`、`field 6 = 24` 恒为常量。

### 4.2 与 transcript 逐条对照（ground truth）

| step | 时间(UTC) | db `f2` | db `f3` | db `f5` | transcript `input` | `output` | `cache_read` | 一致 |
|---|---|---|---|---|---|---|---|---|
| 1  | 11:09:37 | 7603  | 429  | –     | 7603  | 429  | 0     | ✅ |
| 3  | 11:09:44 | 9107  | 137  | –     | 9107  | 137  | 0     | ✅ |
| 5  | 11:09:49 | 9888  | 79   | –     | 9888  | 79   | 0     | ✅ |
| 7  | 11:09:52 | 11144 | 73   | –     | 11144 | 73   | 0     | ✅ |
| 9  | 11:09:55 | 12447 | 132  | –     | 12447 | 132  | 0     | ✅ |
| 11 | 11:10:16 | 14525 | 84   | –     | 14525 | 84   | 0     | ✅ |
| 13 | 11:10:19 | 14751 | 153  | –     | 14751 | 153  | 0     | ✅ |
| 15 | 11:10:22 | 16452 | 613  | –     | 16452 | 613  | 0     | ✅ |
| 17 | 11:10:37 | 2315  | 1562 | 16462 | 2315  | 1562 | 16462 | ✅ |

结论：`field 2 = 输入（未含缓存命中）`、`field 3 = 输出`、`field 5 = 缓存命中输入`、
`field 1/6` 不是 token 计数。**9/9 全等**。

> 先验假设（标准 Gemini `UsageMetadata`：1=prompt、2=candidates、3=total 且
> `3 == 1 + 2`）在本产品上**不成立**：`1318 + 7603 ≠ 429`。所以本模块没有按
> 先验字段号硬套，而是按实测布局解析（并保留对标准布局的兼容兜底，见第 6 节）。

---

## 5. 映射表（`scan()` 实际输出）

| 事件字段 | 来源 | 说明 |
|---|---|---|
| `ts` | `steps.metadata.field 1`（Timestamp） | `seconds*1000 + nanos//1e6` |
| `model` | `gen_metadata.data.field 1.field 19` | UTF-8 字符串；缺失填 `"antigravity"` |
| `input` | usage `field 2` | 未含缓存命中的输入 token |
| `output` | usage `field 3` | 输出 token（= `field 9 + field 10`） |
| `cache_read` | usage `field 5`（缺省 0） | 缓存命中输入 token |
| `cache_creation` | 无对应字段 | 固定 `0` |
| `session_id` | `trajectory_meta.cascade_id` / `metadata.field 20.field 4` | 缺失时用 db 文件名 |

事件粒度：**每次模型生成一条**（= `steps` 里 `step_type=15` 且带 usage 的那 9 条），
工具调用 step 不产生事件。

本次机器上的合计：

```
input=98,232  output=3,262  cache_read=16,462  cache_creation=0
input+output=101,494        含缓存命中总计=117,956
```

---

## 6. 实现要点（`backend/antigravity.py`）

* 纯标准库：`sqlite3`、`glob`、`os`、`pathlib`、`json`、`datetime`、`sys`。
* db 一律 `file:…?mode=ro` 只读打开；`glob("conversations/*.db")` 兼容将来多个库。
* 路径不硬编码：默认 `~/.gemini/antigravity`，可用 `TOKENLENS_ANTIGRAVITY_HOME`（或
  `TOKENLENS_ANTIGRAVITY_DIR`）整体覆盖、`TOKENLENS_ANTIGRAVITY_CONVERSATIONS` 单独覆盖会话目录。
* `scan()` 最外层 `try/except` 兜底：目录不存在、db 损坏、表缺失、单个库解析失败
  都只跳过，返回已解析到的部分或 `[]`（已实测：不存在目录/空目录/坏 db 均返回 `[]`，不抛异常）。
* usage 识别兼容两种布局：
  * `schema A`（本机实测）：`field 2/3/5`，用 `field 9 + field 10 == field 3` 等信号打分（阈值 5 分）；
  * `schema B`（标准 Gemini `UsageMetadata` 兜底）：仅在 `field 3 == field 1 + field 2` 时启用。
* 时间戳兜底链：`metadata.field 1` → `conversation_summaries.db.last_modified_time`
  （按 UTC 解析）→ db 文件 mtime。
* `scan()` 实测耗时约 **110 ms**（1 个库、27 个 blob），可直接在 server 侧调用；
  `sources.py` 里已有 120 s 缓存，无需额外处理。

---

## 7. 样例（已脱敏）

`scan()` 前 5 条（`session_id` 打码）：

```json
{"ts": 1792494577453, "model": "gemini-3.8-flash-n", "input": 7603,  "output": 429,  "cache_read": 0,     "cache_creation": 0, "session_id": "37c8ec57-****-****-****-************"}
{"ts": 1792494584280, "model": "gemini-3.8-flash-n", "input": 9107,  "output": 137,  "cache_read": 0,     "cache_creation": 0, "session_id": "37c8ec57-****-****-****-************"}
{"ts": 1792494589350, "model": "gemini-3.8-flash-n", "input": 9888,  "output": 79,   "cache_read": 0,     "cache_creation": 0, "session_id": "37c8ec57-****-****-****-************"}
{"ts": 1792494592751, "model": "gemini-3.8-flash-n", "input": 11144, "output": 73,   "cache_read": 0,     "cache_creation": 0, "session_id": "37c8ec57-****-****-****-************"}
{"ts": 1792494595401, "model": "gemini-3.8-flash-n", "input": 12447, "output": 132,  "cache_read": 0,     "cache_creation": 0, "session_id": "37c8ec57-****-****-****-************"}
```

第 9 条（唯一命中缓存的）：

```json
{"ts": 1792494637202, "model": "gemini-3.8-flash-n", "input": 2315, "output": 1562, "cache_read": 16462, "cache_creation": 0, "session_id": "37c8ec57-****-****-****-************"}
```

### 自测输出（`python backend\antigravity.py` 实跑）

```
== Antigravity token 用量扫描 ==
会话库目录 : ~/.gemini/antigravity/conversations
db 文件数  : 1
扫描 blob  : 27 (steps.metadata=18, gen_metadata.data=9)
命中 usage : 18 (steps=9, gen_metadata=9)
事件总数   : 9
前 5 条事件(JSON):
  {"ts": 1792494577453, "model": "gemini-3.8-flash-n", "input": 7603, "output": 429, "cache_read": 0, "cache_creation": 0, "session_id": "37c8ec57-****-****-****-************"}
  {"ts": 1792494584280, "model": "gemini-3.8-flash-n", "input": 9107, "output": 137, "cache_read": 0, "cache_creation": 0, "session_id": "37c8ec57-****-****-****-************"}
  {"ts": 1792494589350, "model": "gemini-3.8-flash-n", "input": 9888, "output": 79, "cache_read": 0, "cache_creation": 0, "session_id": "37c8ec57-****-****-****-************"}
  {"ts": 1792494592751, "model": "gemini-3.8-flash-n", "input": 11144, "output": 73, "cache_read": 0, "cache_creation": 0, "session_id": "37c8ec57-****-****-****-************"}
  {"ts": 1792494595401, "model": "gemini-3.8-flash-n", "input": 12447, "output": 132, "cache_read": 0, "cache_creation": 0, "session_id": "37c8ec57-****-****-****-************"}
  ... 其余 4 条略
合计 token : input=98232 output=3262 cache_read=16462 cache_creation=0
            input+output=101494  (含缓存命中总计=117956)
```

---

## 8. 已知不确定性

1. **`field 1 = 1318` / `field 6 = 24` 语义未确证。**
   两者在所有生成里恒定。`field 1` 看起来像系统提示词/基线上下文的 token 数
   （配置里也把 `1318` 当作一个顶级字段），但它**不是** cache：
   `cache_read=0` 的请求里它仍是 1318，而真正的 cache 命中在 `field 5`。
   本模块不把它们计入任何事件字段。

2. **`field 9` / `field 10` 的语义未 100% 确证。**
   二者之和恒等于 `field 3`，且 transcript 的 `output_tokens` 也等于 `field 3`。
   推测是「thinking token / 可见输出 token」的拆分（`field 9` 在有 thinking 文本的
   3 个 step 上明显更大：371/578/841 vs 14~96），但 transcript 没有做这个拆分，
   无法逐项对死。**不影响 `output` 总量**（用 `field 3`）。

3. **output 是否包含 thinking token。**
   若第 2 条的推测成立，则 Antigravity 的 `output_tokens` 已经把 thinking 算进去
   （`f3 = f9 + f10`）。对 TokenLens 这类按 output 计费的看板，这与各家
   「output 含 reasoning」的口径一致；但无法从本库单独拆出「纯可见输出」。

4. **模型名存在两个版本，含义不确定。**
   * 每条生成记录：`gen_metadata.data.field 1.field 19 = "gemini-3.8-flash-n"`；
   * 同一份配置快照：`field 3.field 28 = "gemini-3.8-flash-high"`，而 transcript 里
     用户设置是「Gemini 3.8 Flash (High)」。

   两者不一致（`-n` vs `-high`），无法判定哪个是「对外的模型 id」。
   `scan()` 采用**每条生成自带的** `field 19`；若缺失填 `"antigravity"`。
   若看板更想显示用户所选模型，可改用配置里的 `gemini-3.8-flash-high`（需另写解析）。

5. **时间戳精度。** db 是 `google.protobuf.Timestamp`（纳秒精度），但 transcript 的
   `created_at` 只到秒；对照只能验到秒级。事件 `ts` 为毫秒（纳秒截断），可信。
   若某条事件丢失 `metadata.field 1`，兜底用 summary 的 `last_modified_time`（按 UTC
   解析，与 Timestamp 同基准）→ db 文件 mtime，此时多条事件会共享同一时间戳。

6. **`cache_creation` 无字段。** Antigravity 走 Gemini 隐式缓存，库里只有「命中」
   （`field 5`），没有「写入」计数，故固定 0。若将来出现 `cache_creation` 字段，
   需要重新逆向。

7. **版本通用性只在 1 台机器 / 1 个 db / 1 个产品版本上验证过。**
   本机只有 1 个会话（18 steps、9 次生成）。解析器对布局变化做了启发式容错
   （深度搜索 + 标准 Gemini 布局兜底），但新版本如果改了字段号或换了用量结构，
   可能返回空或漏条；届时用本文件第 2 节的方法重扫一遍即可。

8. **多次提交/多个 db 的去重。** `scan()` 会扫 `conversations/*.db` 全部文件；
   若同一会话被复制成多份 db（或备份），会重复计数。正常情况下一个会话一个库。
   同一库内 `steps` 与 `gen_metadata` 是「同一份数据的两个副本」，代码做了配对，
   不会双计（本机 9 条 usage 对 9 条 usage，事件仍是 9）。

9. **WAL 与并发读取。** 以 `mode=ro` 读 WAL 模式库不会阻塞 IDE 写入；若 IDE 正在写
   且数据尚未 checkpoint，读取可能看不到最新一两条（`-wal`/`-shm` 存在时可正常读）。
   不做任何写入/checkpoint。
