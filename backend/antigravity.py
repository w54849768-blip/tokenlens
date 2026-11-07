# -*- coding: utf-8 -*-
"""Google Antigravity IDE 本地会话库 token 用量解析（只读、纯标准库）。

数据来源
--------
默认目录 ``~/.gemini/antigravity``（可用环境变量 ``TOKENLENS_ANTIGRAVITY_HOME``
或 ``TOKENLENS_ANTIGRAVITY_DIR`` 覆盖）：

* ``conversations/*.db``
    SQLite 会话库，关键表（列值除整型外都是 protobuf 二进制 blob）：

    - ``trajectory_meta(trajectory_id, cascade_id, trajectory_type, source)``
    - ``steps(idx, step_type, status, ..., metadata, step_payload, ...)``
    - ``gen_metadata(idx, data, size)``

* ``conversation_summaries.db``
    只有标题/预览/时间之类的元信息，**不含 token**，仅在本模块里当作
    时间戳兜底来源。

探测结论（完整推理与对照见 docs/antigravity-probe.md）
------------------------------------------------------
用量**不在**标准 Gemini ``UsageMetadata`` 里，而在 ``steps.metadata`` 的
``field 9`` 这个嵌套 message 中（``gen_metadata.data.field 1.field 4`` 是同一份
数据的副本，字段号与取值完全一致）::

    UsageLike {                       # steps.metadata.field=9
      field 1  = 1318                 # 常量，语义未确证（不是 cache，见下）
      field 2  = 输入 token（未含缓存命中）
      field 3  = 输出 token（= field 9 + field 10，含 thinking）
      field 5  = 缓存命中输入 token（cache_read，仅命中时出现）
      field 6  = 24                   # 常量，语义未确证
      field 9  + field 10 == field 3  # 输出 token 的二级拆分，疑似 thoughts/candidates
    }

以上映射已用 Antigravity 自己写出的
``brain/<cascade>/.system_generated/logs/transcript_full.jsonl`` 中的
``input_tokens`` / ``cache_read_tokens`` / ``output_tokens`` 逐条核对，
当前这台机器上 9/9 完全一致（该文件不是 SQLite，不参与 scan()，只作为
交叉验证证据，见文档）。

时间戳：``steps.metadata.field=1`` 是 ``google.protobuf.Timestamp``
（``field 1 = seconds, field 2 = nanos``，UTC），与 transcript 的
``created_at`` 逐条一致。

模型名：``gen_metadata.data.field 1.field 19`` 是可读字符串（本机为
``gemini-3.8-flash-n``）。同一份配置里还出现 ``gemini-3.8-flash-high``，
两者不一致，取舍与不确定性见文档。

输出
----
``scan() -> list[dict]``，每项::

    {"ts": int(毫秒), "model": str, "input": int, "output": int,
     "cache_read": int, "cache_creation": int, "session_id": str}

任何异常都不会抛给调用方；解析失败返回 ``[]`` 或已解析到的部分。
"""

from __future__ import annotations

import datetime
import glob
import json
import os
import pathlib
import sqlite3
import sys

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------

_HOME = os.path.expanduser("~")

#: 环境变量覆盖：与 sources.py 的 antigravity_dir 同名语义，便于测试/迁移
_ENV_BASE = ("TOKENLENS_ANTIGRAVITY_HOME", "TOKENLENS_ANTIGRAVITY_DIR")
_ENV_CONV = ("TOKENLENS_ANTIGRAVITY_CONVERSATIONS",)


def _antigravity_dir() -> str:
    """Antigravity 数据根目录（默认 ~/.gemini/antigravity）。"""
    for key in _ENV_BASE:
        val = os.environ.get(key)
        if val:
            return os.path.expanduser(val)
    return os.path.join(_HOME, ".gemini", "antigravity")


def _conversations_dir() -> str:
    """会话库目录（默认 <antigravity>/conversations）。"""
    for key in _ENV_CONV:
        val = os.environ.get(key)
        if val:
            return os.path.expanduser(val)
    return os.path.join(_antigravity_dir(), "conversations")


def _summaries_db() -> str:
    return os.path.join(_antigravity_dir(), "conversation_summaries.db")


# ---------------------------------------------------------------------------
# 极简 protobuf 解析（不依赖 protobuf 库）
# ---------------------------------------------------------------------------

_WIRE_VARINT = 0
_WIRE_64BIT = 1
_WIRE_LEN = 2
_WIRE_32BIT = 5

_MAX_FIELD_NUMBER = 536870911
_MAX_BLOB_PARSE = 4 * 1024 * 1024  # 单条 blob 的解析上限，防意外超大数据


class _ProtoError(Exception):
    """protobuf 数据不合法（内部使用，不对外抛出）。"""


def _read_varint(buf: bytes, pos: int):
    """读一个 varint，返回 (值, 新位置)。数据不合法时抛 _ProtoError。"""
    value = 0
    shift = 0
    size = len(buf)
    while True:
        if pos >= size:
            raise _ProtoError("varint 越过数据末尾")
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return value, pos
        shift += 7
        if shift > 70:
            raise _ProtoError("varint 过长")


def _parse_message(buf):
    """把 buf 当作 protobuf message 解析。

    返回 ``[(field_number, wire_type, value)]``：
    wire=0 时 value 是 int；wire=2 时是 bytes；wire=1/5 时是 bytes（定长）。
    无法完整解析时返回 ``None``（探测阶段宽容，不抛异常）。
    """
    if not isinstance(buf, (bytes, bytearray)) or not buf:
        return None
    if len(buf) > _MAX_BLOB_PARSE:
        return None
    out = []
    pos = 0
    size = len(buf)
    try:
        while pos < size:
            key, pos = _read_varint(buf, pos)
            field = key >> 3
            wire = key & 0x07
            if field == 0 or field > _MAX_FIELD_NUMBER:
                return None
            if wire == _WIRE_VARINT:
                value, pos = _read_varint(buf, pos)
            elif wire == _WIRE_LEN:
                length, pos = _read_varint(buf, pos)
                if length < 0 or pos + length > size:
                    return None
                value = bytes(buf[pos:pos + length])
                pos += length
            elif wire == _WIRE_32BIT:
                if pos + 4 > size:
                    return None
                value = bytes(buf[pos:pos + 4])
                pos += 4
            elif wire == _WIRE_64BIT:
                if pos + 8 > size:
                    return None
                value = bytes(buf[pos:pos + 8])
                pos += 8
            else:
                return None
            out.append((field, wire, value))
    except _ProtoError:
        return None
    return out or None


def _varint_fields(fields):
    """抽取 message 里的 varint 字段：{字段号: 值}。

    同号字段重复出现时不纳入（本模块用到的结构里它们都是单值）。
    """
    out = {}
    for field, wire, value in fields or ():
        if wire != _WIRE_VARINT:
            continue
        if field in out:
            out[field] = None  # 标记为不可用
        else:
            out[field] = value
    return out


def _first_len(fields, number):
    """取第一个 length-delimited 字段的 bytes，取不到返回 None。"""
    for field, wire, value in fields or ():
        if field == number and wire == _WIRE_LEN:
            return value
    return None


def _text(value):
    """把 bytes 尽力解成可读文本；不可读返回 None。"""
    if not isinstance(value, (bytes, bytearray)) or not value:
        return None
    try:
        text = bytes(value).decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not text:
        return None
    # 只接受可打印文本（模型名/会话 id 都是这类）
    if all(0x20 <= ord(ch) <= 0x7E for ch in text):
        return text
    return None


# ---------------------------------------------------------------------------
# 时间戳
# ---------------------------------------------------------------------------

_TS_MIN = 946684800        # 2000-01-01（秒）
_TS_MAX = 4102444800       # 2100-01-01（秒）
_NS_LIMIT = 1000000000


def _timestamp_ms(buf):
    """解析 google.protobuf.Timestamp {1:seconds, 2:nanos} 为毫秒时间戳。"""
    fields = _parse_message(buf)
    if not fields:
        return None
    nums = _varint_fields(fields)
    seconds = nums.get(1)
    if not isinstance(seconds, int) or not (_TS_MIN <= seconds <= _TS_MAX):
        return None
    nanos = nums.get(2)
    if not isinstance(nanos, int) or not (0 <= nanos < _NS_LIMIT):
        nanos = 0
    return seconds * 1000 + nanos // 1000000


# ---------------------------------------------------------------------------
# usage 结构识别
# ---------------------------------------------------------------------------

# 单条 usage 结构很小（本机 118~123 字节），超过该长度不认为它是 usage
_USAGE_MAX_LEN = 8192
# 输入 token 上限（用于把时间戳等大整数排除掉）
_USAGE_MAX_INPUT = 100000000
_USAGE_MAX_OUTPUT = 10000000


def _classify_usage(buf, fields=None):
    """判断一个 message 是否是 token 用量结构；是则返回统一字典。

    支持两种字段布局：

    * ``schema A``（本机 Antigravity 实测布局）::

          field 2 = 输入 token，field 3 = 输出 token，
          field 5 = 缓存命中 token，field 9 + field 10 = 输出 token 拆分

    * ``schema B``（标准 Gemini ``UsageMetadata`` 布局，作为兼容兜底）::

          field 1 = prompt_token_count
          field 2 = candidates_token_count
          field 3 = total_token_count，且 field 3 == field 1 + field 2
          field 4 = cached_content_token_count（可选）

    识别不出来返回 None。判据刻意保守，宁漏不误。
    """
    if not isinstance(buf, (bytes, bytearray)) or not buf:
        return None
    if len(buf) > _USAGE_MAX_LEN:
        return None
    fields = fields if fields is not None else _parse_message(buf)
    if not fields:
        return None
    nums = _varint_fields(fields)
    # 出现重复同号字段 => 不是我们认识的单值结构
    if any(value is None for value in nums.values()):
        return None

    # ---- schema B：标准 Gemini UsageMetadata（total = prompt + candidates）
    if all(key in nums for key in (1, 2, 3)) and isinstance(nums[1], int) \
            and isinstance(nums[2], int) and isinstance(nums[3], int):
        prompt, candidates, total = nums[1], nums[2], nums[3]
        if (prompt > 0 and candidates > 0
                and total == prompt + candidates
                and total <= _USAGE_MAX_INPUT):
            cached = nums.get(4)
            if not isinstance(cached, int) or not (0 <= cached <= prompt):
                cached = 0
            return {
                "input": prompt,
                "output": candidates,
                "cache_read": cached,
                "schema": "gemini-usage-metadata",
                "_score": 9,
            }

    # ---- schema A：Antigravity 内部结构（field 2 = 输入，field 3 = 输出）
    if not (isinstance(nums.get(2), int) and isinstance(nums.get(3), int)):
        return None
    token_in = nums[2]
    token_out = nums[3]
    if not (0 <= token_in <= _USAGE_MAX_INPUT):
        return None
    if not (0 <= token_out <= _USAGE_MAX_OUTPUT):
        return None
    if token_in == 0 and token_out == 0:
        return None

    score = 0
    # 最强判据：输出 token 等于 field 9 + field 10（本机 9/9 成立）
    if isinstance(nums.get(9), int) and isinstance(nums.get(10), int) \
            and nums[9] + nums[10] == token_out:
        score += 4
    if isinstance(nums.get(1), int) and 0 <= nums[1] <= 1000000:
        score += 2
    if isinstance(nums.get(6), int) and 0 <= nums[6] <= 1000000:
        score += 1
    if token_out <= token_in:
        score += 1
    if score < 5:
        return None

    cached = nums.get(5)
    if not isinstance(cached, int) or not (0 <= cached <= _USAGE_MAX_INPUT):
        cached = 0
    return {
        "input": token_in,
        "output": token_out,
        "cache_read": cached,
        "schema": "antigravity-usage",
        "_score": score,
    }


def _collect_usage_candidates(buf, depth=0, max_depth=3, path=()):
    """递归查找 buf 里所有疑似 usage 的嵌套 message。

    返回 ``[(score, depth, path, usage_dict)]``，调用方按
    (score 降序, depth 升序, path 升序) 取第一个。
    """
    found = []
    fields = _parse_message(buf)
    if not fields:
        return found
    usage = _classify_usage(buf, fields)
    if usage:
        found.append((usage["_score"], depth, path, usage))
    if depth >= max_depth:
        return found
    for field, wire, value in fields:
        if wire != _WIRE_LEN or not isinstance(value, (bytes, bytearray)):
            continue
        if not value or len(value) > 65536:
            continue
        found.extend(_collect_usage_candidates(value, depth + 1, max_depth,
                                               path + (field,)))
    return found


def _pick_usage(buf, max_depth=3):
    """在一段 blob 里挑出最可信的 usage 结构。"""
    candidates = _collect_usage_candidates(buf, 0, max_depth)
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
    return candidates[0][3]


# ---------------------------------------------------------------------------
# SQLite 只读访问
# ---------------------------------------------------------------------------


def _open_ro(path):
    """以只读 URI 方式打开 SQLite（file:...?mode=ro）。"""
    uri = pathlib.Path(path).absolute().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=5.0)


def _rows(con, table):
    """读全表，返回 [{列名: 值}]；表不存在返回 []。"""
    try:
        cur = con.execute('SELECT * FROM "%s"' % table.replace('"', '""'))
    except sqlite3.Error:
        return []
    columns = [desc[0] for desc in cur.description or ()]
    try:
        raw = cur.fetchall()
    except sqlite3.Error:
        return []
    return [dict(zip(columns, row)) for row in raw]


def _as_bytes(value):
    """把 sqlite 读出来的 blob 列统一成 bytes（TEXT 会被编码）。"""
    if value is None:
        return b""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str):
        try:
            return value.encode("utf-8")
        except UnicodeEncodeError:
            return b""
    return b""


def _value_text(value):
    """sqlite 文本列取值（兼容 bytes / str）。"""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, str):
        return value
    return ""


# ---------------------------------------------------------------------------
# steps / gen_metadata 解析
# ---------------------------------------------------------------------------


def _metadata_extras(fields):
    """从 metadata 里取会话线索：cascade_id / gen_idx（都来自 field 20 子结构）。

    field 20 是若干小 message，本机见过的形态::

        {field 1: trajectory_id, field 2: step_idx, field 3: gen_idx?, field 4: cascade_id}

    取值全部容错，取不到就返回 None。
    """
    cascade = None
    gen_idx = None
    for field, wire, value in fields or ():
        if field != 20 or wire != _WIRE_LEN:
            continue
        sub = _parse_message(value)
        if not sub:
            continue
        sub_nums = _varint_fields(sub)
        # field 4 是 cascade_id 字符串
        cascade = cascade or _text(_first_len(sub, 4))
        # field 3 在“最后一步”上出现，等于对应的 gen_metadata.idx
        if isinstance(sub_nums.get(3), int) and gen_idx is None:
            gen_idx = sub_nums[3]
    return cascade, gen_idx


def _parse_step(rec):
    """解析单条 steps 记录；没有 metadata 或没有 usage 时返回 None 部分。"""
    blob = _as_bytes(rec.get("metadata"))
    if not blob:
        return None
    fields = _parse_message(blob)
    if not fields:
        return None
    ts = _timestamp_ms(_first_len(fields, 1))
    cascade, gen_idx = _metadata_extras(fields)
    usage = _pick_usage(blob, max_depth=3)
    return {
        "source": "steps",
        "idx": rec.get("idx"),
        "step_type": rec.get("step_type"),
        "ts": ts,
        "usage": usage,
        "cascade": cascade,
        "gen_idx": gen_idx,
    }


def _gen_kv_map(inner_fields):
    """解析 gen_metadata 里的 key/value 小 map（field 20，key 在 field 1，value 在 field 2）。"""
    kv = {}
    for field, wire, value in inner_fields or ():
        if field != 20 or wire != _WIRE_LEN:
            continue
        sub = _parse_message(value)
        if not sub:
            continue
        key = _text(_first_len(sub, 1))
        val = _text(_first_len(sub, 2))
        if key and val is not None:
            kv[key] = val
    return kv


def _parse_gen(rec):
    """解析单条 gen_metadata 记录：usage + 模型名 + 会话线索。"""
    blob = _as_bytes(rec.get("data"))
    if not blob:
        return None
    usage = None
    model = None
    kv = {}

    root = _parse_message(blob)
    if root:
        inner_blob = _first_len(root, 1)
        inner = _parse_message(inner_blob) if inner_blob else None
        if inner:
            # 实测布局：data.field 1.field 4 = usage
            usage_blob = _first_len(inner, 4)
            if usage_blob:
                usage = _classify_usage(usage_blob)
            # 实测布局：data.field 1.field 19 = 模型名字符串
            model = _text(_first_len(inner, 19))
            kv = _gen_kv_map(inner)
    if usage is None:
        # 布局有变化时兜底：深度搜索（限制深度，防大 blob 误报）
        usage = _pick_usage(blob, max_depth=2)
    return {
        "source": "gen_metadata",
        "idx": rec.get("idx"),
        "usage": usage,
        "model": model,
        "cascade": kv.get("cascade_id") or kv.get("root_cascade_id"),
        "request_id": kv.get("request_id"),
    }


# ---------------------------------------------------------------------------
# conversation_summaries.db（只作为时间兜底）
# ---------------------------------------------------------------------------

_DT_FORMATS = (
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S",
)


def _parse_dt_ms(text):
    """把 summaries 里的时间字符串按 UTC 解析成毫秒（与 metadata Timestamp 同基准）。"""
    if not text:
        return None
    raw = str(text).strip().replace("+00:00", "").replace("Z", "")
    for fmt in _DT_FORMATS:
        try:
            dt = datetime.datetime.strptime(raw, fmt)
        except ValueError:
            continue
        return int(dt.replace(tzinfo=datetime.timezone.utc).timestamp() * 1000)
    return None


def _load_summaries():
    """读 conversation_summaries.db：{conversation_id: last_modified_ms}。"""
    out = {}
    path = _summaries_db()
    if not os.path.exists(path):
        return out
    try:
        con = _open_ro(path)
    except Exception:
        return out
    try:
        for rec in _rows(con, "conversation_summaries"):
            conv = _value_text(rec.get("conversation_id"))
            if not conv:
                continue
            ts = _parse_dt_ms(_value_text(rec.get("last_modified_time")))
            if ts:
                out[conv] = ts
    except Exception:
        return out
    finally:
        try:
            con.close()
        except Exception:
            pass
    return out


def _file_mtime_ms(path):
    try:
        return int(os.path.getmtime(path) * 1000)
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# 单库扫描 & 事件拼装
# ---------------------------------------------------------------------------

#: 最近一次 scan() 的统计（自测/排查用，不影响返回值）
_LAST_STATS = {}


def _reset_stats():
    _LAST_STATS.clear()
    _LAST_STATS.update({
        "dbs": 0,
        "step_blobs": 0,
        "gen_blobs": 0,
        "blobs": 0,
        "usage_hits": 0,
        "step_usage": 0,
        "gen_usage": 0,
        "errors": [],
    })


def _bump(key, delta=1):
    _LAST_STATS[key] = _LAST_STATS.get(key, 0) + delta


def _read_session(con, path):
    """会话 id：优先 trajectory_meta.cascade_id，其次 db 文件名。"""
    for rec in _rows(con, "trajectory_meta"):
        cascade = _value_text(rec.get("cascade_id")).strip()
        if cascade:
            return cascade
    return os.path.splitext(os.path.basename(path))[0]


def _read_steps(con):
    out = []
    for rec in _rows(con, "steps"):
        _bump("step_blobs")
        _bump("blobs")
        try:
            step = _parse_step(rec)
        except Exception:
            step = None
        if not step:
            continue
        if step["usage"]:
            _bump("step_usage")
        out.append(step)
    return out


def _read_gens(con):
    out = []
    for rec in _rows(con, "gen_metadata"):
        _bump("gen_blobs")
        _bump("blobs")
        try:
            gen = _parse_gen(rec)
        except Exception:
            gen = None
        if not gen:
            continue
        if gen["usage"]:
            _bump("gen_usage")
        out.append(gen)
    return out


def _match_step(gen, steps, used):
    """把一条 gen 记录对到它对应的 step（拿时间戳）。

    优先 gen_idx 提示，其次 usage 三元组全等，最后按“有 usage 的 step”顺序对齐。
    """
    gen_idx = gen.get("idx")
    if isinstance(gen_idx, int):
        for step in steps:
            if id(step) in used or not step.get("usage"):
                continue
            if step.get("gen_idx") == gen_idx:
                used.add(id(step))
                return step
    usage = gen.get("usage")
    if usage:
        for step in steps:
            if id(step) in used or not step.get("usage"):
                continue
            other = step["usage"]
            if (other["input"] == usage["input"]
                    and other["output"] == usage["output"]
                    and other["cache_read"] == usage["cache_read"]):
                used.add(id(step))
                return step
    if isinstance(gen_idx, int):
        seen = 0
        for step in steps:
            if id(step) in used or not step.get("usage"):
                continue
            if seen == gen_idx:
                used.add(id(step))
                return step
            seen += 1
    return None


def _make_event(usage, ts, model, session_id):
    return {
        "ts": int(ts or 0),
        "model": model or "antigravity",
        "input": int(usage["input"]),
        "output": int(usage["output"]),
        "cache_read": int(usage["cache_read"]),
        "cache_creation": 0,  # Antigravity 的库中未发现 cache creation 字段
        "session_id": session_id or "",
    }


def _scan_db(path, summaries):
    """扫描单个会话库，返回事件列表（任何异常都吞掉）。"""
    events = []
    try:
        con = _open_ro(path)
    except Exception as exc:  # 打不开就跳过
        _LAST_STATS["errors"].append("%s: %s" % (os.path.basename(path), exc))
        return events
    try:
        steps = _read_steps(con)
        gens = _read_gens(con)
        default_session = _read_session(con, path)
    except Exception as exc:
        _LAST_STATS["errors"].append("%s: %s" % (os.path.basename(path), exc))
        return events
    finally:
        try:
            con.close()
        except Exception:
            pass

    # 时间戳兜底：summaries 的 last_modified_time -> db 文件 mtime
    fallback_ts = summaries.get(default_session) or _file_mtime_ms(path)

    used = set()
    for gen in gens:
        usage = gen.get("usage")
        if not usage:
            continue
        step = _match_step(gen, steps, used)
        ts = (step or {}).get("ts") or fallback_ts
        session = (step or {}).get("cascade") or gen.get("cascade") or default_session
        events.append(_make_event(usage, ts, gen.get("model"), session))

    # gen_metadata 缺失/解析失败时，退化为直接读 steps
    for step in steps:
        if id(step) in used or not step.get("usage"):
            continue
        events.append(_make_event(step["usage"], step.get("ts") or fallback_ts,
                                  None, step.get("cascade") or default_session))

    events.sort(key=lambda item: item["ts"] or 0)
    return events


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------


def scan() -> list[dict]:
    """扫描 Antigravity 本地会话库，返回 token 用量事件列表。

    返回 ``[{"ts": int 毫秒, "model": str, "input": int, "output": int,
    "cache_read": int, "cache_creation": int, "session_id": str}]``。

    纯只读；任何异常都不外抛，最坏返回 ``[]``。
    """
    _reset_stats()
    events = []
    try:
        conv_dir = _conversations_dir()
        try:
            summaries = _load_summaries()
        except Exception:
            summaries = {}
        try:
            db_paths = sorted(glob.glob(os.path.join(conv_dir, "*.db")))
        except Exception:
            db_paths = []
        for path in db_paths:
            if not os.path.isfile(path):
                continue
            _bump("dbs")
            try:
                events.extend(_scan_db(path, summaries))
            except Exception as exc:
                # 单个库失败不影响其它库
                _LAST_STATS["errors"].append("%s: %s" % (os.path.basename(path), exc))
    except Exception:
        return events
    _LAST_STATS["usage_hits"] = _LAST_STATS.get("step_usage", 0) + _LAST_STATS.get("gen_usage", 0)
    return events


# ---------------------------------------------------------------------------
# 自测：python backend/antigravity.py
# ---------------------------------------------------------------------------


def _main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    events = scan()
    stats = dict(_LAST_STATS)
    total_input = sum(e["input"] for e in events)
    total_output = sum(e["output"] for e in events)
    total_cache_read = sum(e["cache_read"] for e in events)
    total_cache_creation = sum(e["cache_creation"] for e in events)

    print("== Antigravity token 用量扫描 ==")
    print("会话库目录 :", _conversations_dir())
    print("db 文件数  :", stats.get("dbs", 0))
    print("扫描 blob  :", stats.get("blobs", 0),
          "(steps.metadata=%d, gen_metadata.data=%d)"
          % (stats.get("step_blobs", 0), stats.get("gen_blobs", 0)))
    print("命中 usage :", stats.get("usage_hits", 0),
          "(steps=%d, gen_metadata=%d)"
          % (stats.get("step_usage", 0), stats.get("gen_usage", 0)))
    print("事件总数   :", len(events))
    print("前 5 条事件(JSON):")
    for event in events[:5]:
        print("  " + json.dumps(event, ensure_ascii=False))
    if len(events) > 5:
        print("  ... 其余 %d 条略" % (len(events) - 5))
    print("合计 token : input=%d output=%d cache_read=%d cache_creation=%d"
          % (total_input, total_output, total_cache_read, total_cache_creation))
    print("            input+output=%d  (含缓存命中总计=%d)"
          % (total_input + total_output,
             total_input + total_output + total_cache_read + total_cache_creation))
    if stats.get("errors"):
        print("错误       :")
        for err in stats["errors"]:
            print("  -", err)


if __name__ == "__main__":
    _main()
