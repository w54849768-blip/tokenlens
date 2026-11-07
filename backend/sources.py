"""TokenLens 数据源层。

只读用户本机已有的数据，不做任何写入：
  * CC Switch 代理日志  ~/.cc-switch/cc-switch.db -> proxy_request_logs
  * Claude Code 会话    ~/.claude/projects/**/*.jsonl

产出 docs/API.md 里约定的结构。全部标准库实现。
"""

from __future__ import annotations

import csv
import glob
import io
import json
import os
import sqlite3
import threading
import time
from datetime import datetime

HOME = os.path.expanduser("~")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")

DEFAULT_SETTINGS = {
    "port": 8787,
    "auto_refresh_sec": 30,
    "balance_refresh_sec": 600,
    "cc_switch_db": os.path.join(HOME, ".cc-switch", "cc-switch.db"),
    "claude_dir": os.path.join(HOME, ".claude"),
    "dsh_dir": os.path.join(HOME, ".dsh"),
    "antigravity_dir": os.path.join(HOME, ".gemini", "antigravity"),
}

# cc-switch 里由「会话日志回填」产生的伪供应商
PSEUDO_PROVIDERS = {
    "_session": "Claude Code 会话日志",
    "_opencode_session": "OpenCode 会话日志",
    "_codex_session": "Codex 会话日志",
}
APP_LABELS = {
    "dsh": "DeepSeek Harness",
    "claude": "Claude Code",
    "claude-desktop": "Claude Desktop",
    "codex": "Codex",
    "opencode": "OpenCode",
    "gemini": "Gemini CLI",
    "antigravity": "Antigravity",
    "cursor": "Cursor",
    "grokbuild": "Grok",
    "grokbot": "Grok Bot",
    "hermes": "Hermes",
    "openclaw": "OpenClaw",
    "workbuddy": "WorkBuddy AI",
    "codebuddy": "CodeBuddy",
    "doubao": "豆包",
}
SOURCE_LABELS = {
    "proxy": "代理拦截",
    "session_log": "Claude 会话日志",
    "opencode_session": "OpenCode 会话日志",
    "codex_session": "Codex 会话日志",
}


# --------------------------------------------------------------------------- #
# 设置
# --------------------------------------------------------------------------- #
def load_settings() -> dict:
    cfg = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as fh:
            cfg.update(json.load(fh) or {})
    except FileNotFoundError:
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(SETTINGS_PATH, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass
    except Exception:
        pass
    return cfg


def save_settings(patch: dict) -> dict:
    cfg = load_settings()
    for k in ("port", "auto_refresh_sec", "balance_refresh_sec"):
        if k in patch and isinstance(patch[k], (int, float)):
            cfg[k] = int(patch[k])
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
    return cfg


def _cfg_path(key: str) -> str:
    return os.path.expanduser(load_settings().get(key) or DEFAULT_SETTINGS[key])


def cc_db_path() -> str:
    return _cfg_path("cc_switch_db")


def claude_dir() -> str:
    return _cfg_path("claude_dir")


def _ro(db: str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")
    return con


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _f(v, default=0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _i(v, default=0) -> int:
    try:
        if v is None:
            return default
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _pct(a, b):
    return round(a / b, 4) if b else None


def _percentile(values, p):
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return int(round(s[lo] + (s[hi] - s[lo]) * (k - lo)))


def _bucket_start(ts: int, gran: str) -> int:
    dt = datetime.fromtimestamp(ts)
    if gran == "hour":
        dt = dt.replace(minute=0, second=0, microsecond=0)
    else:
        dt = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(dt.timestamp() * 1000)


def _bucket_label(ts_ms: int, gran: str) -> str:
    dt = datetime.fromtimestamp(ts_ms / 1000)
    return dt.strftime("%m-%d %H:00" if gran == "hour" else "%m-%d")


# --------------------------------------------------------------------------- #
# CC Switch 请求日志
# --------------------------------------------------------------------------- #
_ROW_COLS = (
    "request_id,provider_id,app_type,model,request_model,pricing_model,"
    "input_tokens,output_tokens,cache_read_tokens,cache_creation_tokens,"
    "input_cost_usd,output_cost_usd,cache_read_cost_usd,cache_creation_cost_usd,"
    "total_cost_usd,latency_ms,first_token_ms,duration_ms,status_code,error_message,"
    "session_id,provider_type,is_streaming,cost_multiplier,created_at,data_source"
)


def _provider_index(con) -> dict:
    idx = {}
    try:
        for r in con.execute(
            "SELECT id,name,app_type,category,website_url,is_current FROM providers"
        ):
            idx[r["id"]] = {
                "name": r["name"],
                "app_type": r["app_type"],
                "category": r["category"],
                "website_url": r["website_url"],
                "is_current": bool(r["is_current"]),
            }
    except sqlite3.Error:
        pass
    return idx


def _where(f: dict):
    sql, params = [], []
    if f.get("from"):
        sql.append("created_at >= ?")
        params.append(int(f["from"]) / 1000)
    if f.get("to"):
        sql.append("created_at <= ?")
        params.append(int(f["to"]) / 1000)
    if f.get("app"):
        sql.append("app_type = ?")
        params.append(f["app"])
    if f.get("model"):
        sql.append("model = ?")
        params.append(f["model"])
    st = f.get("status")
    if st:
        if st == "error":
            sql.append("status_code >= 400")
        elif st == "4xx":
            sql.append("status_code BETWEEN 400 AND 499")
        elif st == "5xx":
            sql.append("status_code >= 500")
        elif st == "ok":
            sql.append("(status_code < 400 OR status_code IS NULL)")
        else:
            sql.append("status_code = ?")
            params.append(_i(st))
    q = (f.get("q") or "").strip()
    if q:
        like = f"%{q}%"
        sql.append(
            "(model LIKE ? OR request_model LIKE ? OR session_id LIKE ? "
            "OR error_message LIKE ? OR request_id LIKE ? OR provider_id LIKE ?)"
        )
        params += [like] * 6
    return (" WHERE " + " AND ".join(sql)) if sql else "", params


def _norm_row(r, pidx: dict) -> dict:
    pid = r["provider_id"] or ""
    prov = pidx.get(pid)
    pname = (prov or {}).get("name") or PSEUDO_PROVIDERS.get(pid)
    if not pname:
        pname = f"未识别供应商 ({pid[:8]})" if len(pid) > 16 else (pid or "未知")
    cache_cost = _f(r["cache_read_cost_usd"]) + _f(r["cache_creation_cost_usd"])
    return {
        "id": r["request_id"],
        "ts": _i(r["created_at"]) * 1000,
        "app": r["app_type"],
        "app_label": APP_LABELS.get(r["app_type"], r["app_type"]),
        "provider": pid,
        "provider_name": pname,
        "model": r["model"] or r["request_model"] or "unknown",
        "input_tokens": _i(r["input_tokens"]),
        "output_tokens": _i(r["output_tokens"]),
        "cache_read_tokens": _i(r["cache_read_tokens"]),
        "cache_creation_tokens": _i(r["cache_creation_tokens"]),
        "cost_usd": round(_f(r["total_cost_usd"]), 6),
        "input_cost_usd": round(_f(r["input_cost_usd"]), 6),
        "output_cost_usd": round(_f(r["output_cost_usd"]), 6),
        "cache_cost_usd": round(cache_cost, 6),
        "latency_ms": _i(r["latency_ms"]) if r["latency_ms"] is not None else None,
        "first_token_ms": _i(r["first_token_ms"]) if r["first_token_ms"] is not None else None,
        "status": _i(r["status_code"]) if r["status_code"] is not None else None,
        "error": r["error_message"],
        "session_id": r["session_id"],
        "streaming": _i(r["is_streaming"]),
        "source": r["data_source"],
        "source_label": SOURCE_LABELS.get(r["data_source"], r["data_source"]),
        "pricing_model": r["pricing_model"],
    }


def fetch_rows(f: dict):
    db = cc_db_path()
    native = _native_rows(f)
    if not os.path.exists(db):
        return native, {}
    con = _ro(db)
    try:
        pidx = _provider_index(con)
        where, params = _where(f)
        rows = con.execute(f"SELECT {_ROW_COLS} FROM proxy_request_logs{where}", params).fetchall()
        out = [_norm_row(r, pidx) for r in rows]
        out.extend(native)
        return out, pidx
    finally:
        con.close()


def list_requests(f: dict) -> dict:
    rows, _ = fetch_rows(f)
    order = (f.get("order") or "-ts").strip()
    desc = order.startswith("-")
    key = order.lstrip("-")
    keyfn = {
        "ts": lambda r: r["ts"],
        "cost": lambda r: r["cost_usd"],
        "latency": lambda r: r["latency_ms"] or 0,
        "tokens": lambda r: r["input_tokens"] + r["output_tokens"] + r["cache_read_tokens"],
    }.get(key, lambda r: r["ts"])
    rows.sort(key=keyfn, reverse=desc)
    total = len(rows)
    offset = max(0, _i(f.get("offset")))
    limit = _i(f.get("limit")) or 50
    limit = max(1, min(limit, 500))
    return {"total": total, "rows": rows[offset : offset + limit], "offset": offset, "limit": limit}


def _aggregate_totals(rows, latencies) -> dict:
    t = {
        "requests": len(rows),
        "errors": sum(1 for r in rows if (r["status"] or 0) >= 400),
        "input_tokens": sum(r["input_tokens"] for r in rows),
        "output_tokens": sum(r["output_tokens"] for r in rows),
        "cache_read_tokens": sum(r["cache_read_tokens"] for r in rows),
        "cache_creation_tokens": sum(r["cache_creation_tokens"] for r in rows),
        "cost_usd": round(sum(r["cost_usd"] for r in rows), 6),
    }
    t["prompt_tokens"] = t["input_tokens"] + t["cache_read_tokens"] + t["cache_creation_tokens"]
    t["total_tokens"] = t["prompt_tokens"] + t["output_tokens"]
    t["success_rate"] = _pct(t["requests"] - t["errors"], t["requests"])
    t["cache_hit_rate"] = _pct(t["cache_read_tokens"], t["prompt_tokens"])
    t["avg_latency_ms"] = int(sum(latencies) / len(latencies)) if latencies else None
    t["p50_latency_ms"] = _percentile(latencies, 0.50)
    t["p95_latency_ms"] = _percentile(latencies, 0.95)
    return t


def overview(f: dict) -> dict:
    f = dict(f)
    now_ms = int(time.time() * 1000)
    # 没给时间范围时，默认取最近 14 天（且必须真正参与过滤，不能只用于画标签）
    if not f.get("from") and not f.get("to"):
        probe, _ = fetch_rows({k: f.get(k) for k in ("app", "model", "status", "q")})
        to_ms = max((r["ts"] for r in probe), default=now_ms)
        f["from"] = to_ms - 13 * 86400 * 1000
        f["to"] = to_ms

    rows, _ = fetch_rows(f)
    to_ms = _i(f.get("to")) or (max([r["ts"] for r in rows]) if rows else now_ms)
    from_ms = _i(f.get("from")) or (to_ms - 13 * 86400 * 1000)
    span = max(1, to_ms - from_ms)
    gran = "hour" if span <= 2 * 86400 * 1000 else "day"

    latencies = [r["latency_ms"] for r in rows if r["latency_ms"]]
    totals = _aggregate_totals(rows, latencies)

    # 环比：上一个等长窗口
    prev_f = dict(f)
    prev_f["from"] = from_ms - span
    prev_f["to"] = from_ms
    prev_rows, _ = fetch_rows(prev_f)
    prev = _aggregate_totals(prev_rows, [r["latency_ms"] for r in prev_rows if r["latency_ms"]])
    prev_summary = {
        k: prev[k]
        for k in ("requests", "cost_usd", "cache_hit_rate", "avg_latency_ms", "total_tokens", "errors")
    }

    # 时间序列（补零）
    buckets = {}
    step = 3600 * 1000 if gran == "hour" else 86400 * 1000
    start = _bucket_start(from_ms // 1000, gran)
    end = _bucket_start(to_ms // 1000, gran)
    cur = start
    while cur <= end:
        buckets[cur] = {
            "t": cur,
            "label": _bucket_label(cur, gran),
            "requests": 0,
            "errors": 0,
            "cost_usd": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "lat_sum": 0,
            "lat_n": 0,
        }
        cur += step

    for r in rows:
        b = buckets.get(_bucket_start(r["ts"] // 1000, gran))
        if not b:
            continue
        b["requests"] += 1
        b["errors"] += 1 if (r["status"] or 0) >= 400 else 0
        b["cost_usd"] += r["cost_usd"]
        b["input_tokens"] += r["input_tokens"]
        b["output_tokens"] += r["output_tokens"]
        b["cache_read_tokens"] += r["cache_read_tokens"]
        b["cache_creation_tokens"] += r["cache_creation_tokens"]
        if r["latency_ms"]:
            b["lat_sum"] += r["latency_ms"]
            b["lat_n"] += 1

    series = []
    for b in buckets.values():
        item = dict(b)
        item["avg_latency_ms"] = int(b["lat_sum"] / b["lat_n"]) if b["lat_n"] else None
        item["cost_usd"] = round(b["cost_usd"], 6)
        item.pop("lat_sum")
        item.pop("lat_n")
        series.append(item)

    def group(key_fn, extra=None):
        acc = {}
        for r in rows:
            k = key_fn(r)
            g = acc.setdefault(
                k,
                {"key": k, "requests": 0, "errors": 0, "cost_usd": 0.0,
                 "input": 0, "output": 0, "cache_read": 0, "cache_creation": 0},
            )
            g["requests"] += 1
            g["errors"] += 1 if (r["status"] or 0) >= 400 else 0
            g["cost_usd"] += r["cost_usd"]
            g["input"] += r["input_tokens"]
            g["output"] += r["output_tokens"]
            g["cache_read"] += r["cache_read_tokens"]
            g["cache_creation"] += r["cache_creation_tokens"]
        out = sorted(acc.values(), key=lambda x: -x["requests"])
        for g in out:
            g["cost_usd"] = round(g["cost_usd"], 6)
            g["tokens"] = g["input"] + g["output"] + g["cache_read"] + g["cache_creation"]
            g["prompt_tokens"] = g["input"] + g["cache_read"] + g["cache_creation"]
            g["cache_hit_rate"] = _pct(g["cache_read"], g["prompt_tokens"])
        return out

    by_provider = group(lambda r: r["provider"])
    for g in by_provider:
        g["name"] = next((r["provider_name"] for r in rows if r["provider"] == g["key"]), g["key"])
    by_app = group(lambda r: r["app"] or "unknown")
    for g in by_app:
        g["name"] = APP_LABELS.get(g["key"], g["key"])
    by_model = group(lambda r: r["model"] or "unknown")

    # 每个应用的每日序列 + 该应用下的模型明细（首页「各产品用量」用）
    bucket_keys = list(buckets.keys())
    idx_map = {k: i for i, k in enumerate(bucket_keys)}
    models_by_app = {}
    acc_app = {g["key"]: [{"requests": 0, "tokens": 0, "cost_usd": 0.0} for _ in bucket_keys]
               for g in by_app}
    for r in rows:
        a = r["app"] or "unknown"
        tok = (r["input_tokens"] + r["output_tokens"]
               + r["cache_read_tokens"] + r["cache_creation_tokens"])
        i = idx_map.get(_bucket_start(r["ts"] // 1000, gran))
        if i is not None and a in acc_app:
            cell = acc_app[a][i]
            cell["requests"] += 1
            cell["tokens"] += tok
            cell["cost_usd"] += r["cost_usd"]
        mm = models_by_app.setdefault(a, {})
        mk = r["model"] or "unknown"
        e = mm.setdefault(mk, {"model": mk, "requests": 0, "tokens": 0, "cost_usd": 0.0})
        e["requests"] += 1
        e["tokens"] += tok
        e["cost_usd"] += r["cost_usd"]

    for g in by_app:
        g["last_ts"] = max((r["ts"] for r in rows if (r["app"] or "unknown") == g["key"]),
                           default=None)
        lst = sorted(models_by_app.get(g["key"], {}).values(), key=lambda x: -x["requests"])[:6]
        for x in lst:
            x["cost_usd"] = round(x["cost_usd"], 6)
        g["models"] = lst
        g["cost_share"] = _pct(g["cost_usd"], totals["cost_usd"])
        g["token_share"] = _pct(g["tokens"], totals["total_tokens"])

    series_by_app = []
    for g in by_app:
        cells = acc_app.get(g["key"]) or []
        series_by_app.append({
            "app": g["key"],
            "name": g["name"],
            "requests": [c["requests"] for c in cells],
            "tokens": [c["tokens"] for c in cells],
            "cost": [round(c["cost_usd"], 6) for c in cells],
        })

    status_codes = {}
    for r in rows:
        c = r["status"] if r["status"] is not None else 0
        status_codes[c] = status_codes.get(c, 0) + 1

    lat_edges = [(0, 1000, "<1s"), (1000, 3000, "1–3s"), (3000, 5000, "3–5s"),
                 (5000, 10000, "5–10s"), (10000, 30000, "10–30s"), (30000, 10**9, ">30s")]
    lat_buckets = [
        {"label": lbl, "count": sum(1 for v in latencies if lo <= v < hi)}
        for lo, hi, lbl in lat_edges
    ]

    return {
        "range": {
            "from": from_ms,
            "to": to_ms,
            "granularity": gran,
            "label": _bucket_label(from_ms, gran) + " → " + _bucket_label(to_ms, gran),
        },
        "totals": totals,
        "prev": prev_summary,
        "series": series,
        "series_by_app": series_by_app,
        "by_app": by_app,
        "by_model": by_model[:14],
        "by_provider": by_provider[:10],
        "status_codes": [{"code": k, "count": v} for k, v in sorted(status_codes.items())],
        "latency_buckets": lat_buckets,
    }


def requests_csv(f: dict) -> str:
    f = dict(f)
    f["limit"] = 500
    f["offset"] = 0
    data = list_requests(f)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["时间", "应用", "供应商", "模型", "输入", "输出", "缓存读", "缓存写",
                "成本USD", "延迟ms", "首字ms", "状态码", "错误", "会话", "来源"])
    for r in data["rows"]:
        w.writerow([
            datetime.fromtimestamp(r["ts"] / 1000).strftime("%Y-%m-%d %H:%M:%S"),
            r["app_label"], r["provider_name"], r["model"],
            r["input_tokens"], r["output_tokens"], r["cache_read_tokens"],
            r["cache_creation_tokens"], r["cost_usd"], r["latency_ms"] or "",
            r["first_token_ms"] or "", r["status"] or "", r["error"] or "",
            r["session_id"] or "", r["source_label"],
        ])
    return "\ufeff" + buf.getvalue()   # BOM：Excel 直接打开中文不乱码


def export_all_chats() -> bytes:
    """把所有会话导出为一个 zip（Markdown + 索引）。"""
    import re
    import zipfile

    entries = [("claude", sid, m) for sid, m in claude_index().items()]
    entries += [("dsh", "dsh:" + m["id"], m) for m in dsh_sessions()]

    buf = io.BytesIO()
    manifest = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for source, cid, m in entries:
            try:
                text = chat_markdown(cid)
            except Exception:
                continue
            if not text:
                continue
            safe = re.sub(r'[\\/:*?"<>|\r\n]+', "_", (m.get("title") or cid)[:48]).strip(" ._")
            name = f"{source}-{(m.get('id') or cid)[:8]}-{safe or 'chat'}.md"
            z.writestr(name, text)
            manifest.append({
                "id": cid,
                "source": source,
                "title": m.get("title"),
                "project": m.get("project"),
                "models": m.get("models"),
                "updated": m.get("updated"),
                "file": name,
                "usage": m.get("usage"),
            })
        z.writestr("index.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Claude Code 会话
# --------------------------------------------------------------------------- #
_CACHE = {"claude": None, "sig": None, "lock": threading.Lock(), "detail": {},
          "events": [], "at": 0.0, "pricing": None, "pricing_at": 0.0}
MAX_BLOCK = 60000
INDEX_TTL = 4.0          # 秒：避免正在进行的会话每毫秒触发全量重建
NATIVE_PROVIDER = "_native_claude"


def _claude_files():
    base = os.path.join(claude_dir(), "projects")
    return sorted(glob.glob(os.path.join(base, "**", "*.jsonl"), recursive=True))


def _claude_sig():
    files = _claude_files()
    sig = 0
    for p in files:
        try:
            st = os.stat(p)
            sig += st.st_mtime_ns // 1000 + st.st_size
        except OSError:
            pass
    return (len(files), sig)


def _blocks_from_content(content, role, tool_result_text=True):
    blocks = []
    if isinstance(content, str):
        if content.strip():
            blocks.append({"type": "text", "text": content[:MAX_BLOCK]})
        return blocks
    if not isinstance(content, list):
        return blocks
    for b in content:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            txt = b.get("text") or ""
            if txt.strip():
                blocks.append({"type": "text", "text": txt[:MAX_BLOCK]})
        elif t == "thinking":
            txt = b.get("thinking") or ""
            if txt.strip():
                blocks.append({"type": "thinking", "text": txt[:MAX_BLOCK]})
        elif t == "tool_use":
            blocks.append({"type": "tool_use", "name": b.get("name") or "tool",
                           "input": b.get("input"), "id": b.get("id")})
        elif t == "tool_result":
            if not tool_result_text:
                continue
            c = b.get("content")
            if isinstance(c, list):
                c = "\n".join(
                    x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text"
                )
            txt = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
            blocks.append({"type": "tool_result", "text": (txt or "")[:MAX_BLOCK],
                           "is_error": bool(b.get("is_error"))})
        elif t == "image":
            blocks.append({"type": "image"})
    return blocks


def _parse_file(path: str, full: bool = False) -> dict:
    sid = os.path.splitext(os.path.basename(path))[0]
    meta = {
        "id": sid, "file": path, "title": None, "project": None,
        "created": None, "updated": None, "models": [],
        "usage": {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0},
        "message_count": 0, "version": None,
    }
    models, seen_msg = [], set()
    messages = []
    events = []
    first_prompt = None
    seq = 0
    title = None

    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return meta

    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            t = d.get("type")

            if t == "ai-title":
                if d.get("aiTitle"):
                    title = d["aiTitle"]
                continue
            if t == "summary":
                if not title and d.get("summary"):
                    title = d["summary"]
                continue

            ts = None
            raw_ts = d.get("timestamp")
            if raw_ts:
                try:
                    ts = int(datetime.fromisoformat(raw_ts.replace("Z", "+00:00")).timestamp() * 1000)
                except Exception:
                    ts = None

            if d.get("cwd") and not meta["project"]:
                meta["project"] = d["cwd"]
            if d.get("version"):
                meta["version"] = d["version"]
            if d.get("sessionId"):
                meta["id"] = d["sessionId"]

            if t == "user":
                if d.get("isSidechain"):
                    continue
                msg = d.get("message") or {}
                content = msg.get("content")
                blocks = _blocks_from_content(content, "user")
                if not blocks:
                    continue
                has_real_text = any(b["type"] == "text" for b in blocks)
                if not first_prompt and has_real_text:
                    txt = next(b["text"] for b in blocks if b["type"] == "text")
                    first_prompt = " ".join(txt.split())[:70]
                # 纯 tool_result 的回填记录不计入消息数
                if not has_real_text:
                    # 纯工具结果回填：并入上一条助手消息，不单独成条
                    if full and messages and messages[-1]["role"] == "assistant":
                        messages[-1]["blocks"].extend(blocks)
                    continue
                seq += 1
                meta["message_count"] += 1
                if full:
                    messages.append({
                        "role": "user", "ts": ts, "seq": seq, "model": None,
                        "usage": None, "blocks": blocks,
                    })
                if ts:
                    meta["created"] = min(meta["created"] or ts, ts)
                    meta["updated"] = max(meta["updated"] or ts, ts)

            elif t == "assistant":
                msg = d.get("message") or {}
                mid = msg.get("id")
                model = msg.get("model") or ""
                if model and model != "<synthetic>" and model not in models:
                    models.append(model)
                usage = msg.get("usage") or {}
                if mid and mid in seen_msg:
                    # 同一 message 的分块写入：补块，不重复计 token
                    if full and messages and messages[-1].get("mid") == mid:
                        messages[-1]["blocks"].extend(_blocks_from_content(msg.get("content"), "assistant"))
                    continue
                if mid:
                    seen_msg.add(mid)
                u = {
                    "input": _i(usage.get("input_tokens")),
                    "output": _i(usage.get("output_tokens")),
                    "cache_read": _i(usage.get("cache_read_input_tokens")),
                    "cache_creation": _i(usage.get("cache_creation_input_tokens")),
                }
                if u["input"] or u["output"]:
                    for k in meta["usage"]:
                        meta["usage"][k] += u[k]
                    if ts and model and model != "<synthetic>":
                        events.append({
                            "id": mid or f"{sid}:{seq}",
                            "ts": ts,
                            "model": model,
                            "input": u["input"],
                            "output": u["output"],
                            "cache_read": u["cache_read"],
                            "cache_creation": u["cache_creation"],
                            "session_id": sid,
                        })
                blocks = _blocks_from_content(msg.get("content"), "assistant")
                if not blocks:
                    continue
                seq += 1
                meta["message_count"] += 1
                if full:
                    messages.append({
                        "role": "assistant", "ts": ts, "seq": seq, "model": model or None,
                        "usage": u, "blocks": blocks, "mid": mid,
                    })
                if ts:
                    meta["created"] = min(meta["created"] or ts, ts)
                    meta["updated"] = max(meta["updated"] or ts, ts)

    meta["models"] = models
    meta["events"] = events
    meta["first_prompt"] = first_prompt
    meta["title"] = title or first_prompt or (
        os.path.basename(os.path.dirname(path)) + " / " + sid[:8])
    if full:
        meta["messages"] = messages
        for m in meta["messages"]:
            m.pop("mid", None)
    return meta


def claude_index(force: bool = False) -> dict:
    sig = _claude_sig()
    with _CACHE["lock"]:
        fresh = (not force and _CACHE["claude"] is not None and _CACHE["sig"] == sig)
        if fresh:
            return _CACHE["claude"]
        # 正在写入的会话会让 sig 一直变：限流重建，避免每次请求都全量解析
        if (not force and _CACHE["claude"] is not None
                and (time.time() - _CACHE["at"]) < INDEX_TTL):
            return _CACHE["claude"]
        sessions, events = {}, []
        for p in _claude_files():
            try:
                m = _parse_file(p, full=False)
            except Exception:
                continue
            if not m.get("message_count"):
                continue
            sessions[m["id"]] = m
            events.extend(m.get("events") or [])
        _CACHE["claude"] = sessions
        _CACHE["events"] = events
        _CACHE["sig"] = sig
        _CACHE["at"] = time.time()
        _CACHE["detail"] = {}
        return sessions


def _pricing() -> dict:
    """借 cc-switch 的价格表算本地日志的成本（只读）。"""
    if _CACHE["pricing"] is not None and (time.time() - _CACHE["pricing_at"]) < 120:
        return _CACHE["pricing"]
    table = {}
    try:
        db = cc_db_path()
        if os.path.exists(db):
            con = _ro(db)
            for r in con.execute(
                "SELECT model_id,input_cost_per_million,output_cost_per_million,"
                "cache_read_cost_per_million,cache_creation_cost_per_million FROM model_pricing"
            ):
                table[(r["model_id"] or "").lower()] = (
                    _f(r["input_cost_per_million"]), _f(r["output_cost_per_million"]),
                    _f(r["cache_read_cost_per_million"]), _f(r["cache_creation_cost_per_million"]),
                )
            con.close()
    except sqlite3.Error:
        pass
    _CACHE["pricing"] = table
    _CACHE["pricing_at"] = time.time()
    return table


# --------------------------------------------------------------------------- #
# DeepSeek Harness（~/.dsh/sessions/**/session.v4.jsonl.zstd）
#   每条 assistant/message 都带 usage：inputTokens / outputTokens /
#   cacheReadTokens / cacheWriteTokens。文件是 zstd 压缩的 JSONL，
#   Python 3.14 的 compression.zstd 可直接解。
# --------------------------------------------------------------------------- #
_DSH = {"files": {}, "at": 0.0, "lock": threading.Lock(), "disk": None}
DSH_PROVIDER = "_dsh"
DSH_CACHE_FILE = os.path.join(DATA_DIR, "dsh-cache.json")


def _dsh_load_cache() -> dict:
    """把上次的解析结果读回来：只有 mtime/size 变化的文件才重新解压。"""
    try:
        with open(DSH_CACHE_FILE, encoding="utf-8") as fh:
            raw = json.load(fh)
        out = {}
        for path, item in (raw.get("files") or {}).items():
            key = item.get("key")
            data = item.get("data")
            if isinstance(key, list) and isinstance(data, dict):
                out[path] = (tuple(key), data)
        return out
    except Exception:
        return {}


def _dsh_save_cache(files: dict):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        payload = {"v": 1, "files": {
            p: {"key": list(v[0]), "data": v[1]} for p, v in files.items()
        }}
        tmp = DSH_CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, DSH_CACHE_FILE)
    except Exception:
        pass


def dsh_dir() -> str:
    return _cfg_path("dsh_dir")


def antigravity_dir() -> str:
    return _cfg_path("antigravity_dir")


def _dsh_files():
    return sorted(glob.glob(os.path.join(dsh_dir(), "sessions", "**", "*.jsonl.zstd"),
                            recursive=True))


def _decode_project(dirname: str) -> str:
    """--D-code-- → D:\\code"""
    t = dirname.strip("-")
    return t.replace("-", "\\") if t else ""


def _dsh_open(path: str):
    from compression import zstd
    try:
        raw = zstd.open(path, "rb")
    except AttributeError:          # 兼容不同实现
        raw = zstd.ZstdFile(path, "rb")
    return io.TextIOWrapper(raw, encoding="utf-8", errors="replace")


def _dsh_msg_blocks(content):
    """DSH 的 content 数组 -> 统一的 block 结构。"""
    blocks = []
    if not isinstance(content, list):
        return blocks
    for c in content:
        if not isinstance(c, dict):
            continue
        t = c.get("type")
        if t == "text":
            txt = c.get("text") or ""
            if txt.strip():
                blocks.append({"type": "text", "text": txt[:MAX_BLOCK]})
        elif t == "reasoning":
            txt = c.get("text") or ""
            if txt.strip():
                blocks.append({"type": "thinking", "text": txt[:MAX_BLOCK]})
        elif t == "tool-call":
            args = c.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    pass
            blocks.append({"type": "tool_use", "name": c.get("name") or "tool",
                           "input": args, "id": c.get("id")})
        elif t == "tool-result":
            blocks.append({"type": "tool_result", "text": str(c.get("result"))[:MAX_BLOCK],
                           "is_error": False})
    return blocks


def _parse_dsh_file(path: str, full: bool = False) -> dict:
    sid_dir = os.path.basename(os.path.dirname(path))
    sid = sid_dir[8:] if sid_dir.startswith("session-") else sid_dir
    meta = {
        "id": sid,
        "project": _decode_project(os.path.basename(os.path.dirname(os.path.dirname(path)))),
        "title": None, "models": [], "created": None, "updated": None,
        "message_count": 0, "events": [], "messages": [],
    }
    try:
        fh = _dsh_open(path)
    except Exception:
        return meta
    title = None
    messages = meta["messages"]

    def oneline(line):
        try:
            return json.loads(line) if len(line) < 8_000_000 else None
        except Exception:
            return None

    try:
        with fh:
            for line in fh:
                if not line:
                    continue
                head = line[:60]
                if '"assistant/message"' in head:
                    # 快路径：跳过巨大的 stream 字段，只截 usage 小对象
                    k = line.find('"usage":{')
                    if k < 0:
                        continue
                    end = line.find("}", k)
                    try:
                        u = json.loads(line[k + 8:end + 1])
                    except Exception:
                        continue
                    ti = line.find('"time":')
                    ts = _i(line[ti + 7:line.find(",", ti)]) if ti >= 0 else 0
                    mi = line.find('"model":"')
                    model = line[mi + 9:line.find('"', mi + 9)] if mi >= 0 else "unknown"
                    if model and model not in meta["models"]:
                        meta["models"].append(model)
                    ev = {
                        "id": f"{sid}#{len(meta['events'])}",
                        "ts": ts,
                        "model": model or "unknown",
                        "input": _i(u.get("inputTokens")),
                        "output": _i(u.get("outputTokens")),
                        "cache_read": _i(u.get("cacheReadTokens")),
                        "cache_creation": _i(u.get("cacheWriteTokens")),
                    }
                    meta["events"].append(ev)
                    meta["message_count"] += 1
                    if ts:
                        meta["created"] = min(meta["created"] or ts, ts)
                        meta["updated"] = max(meta["updated"] or ts, ts)
                    if full:
                        d = oneline(line)
                        if d:
                            d2 = d.get("data") or {}
                            msg = d2.get("message") or d2
                            blocks = _dsh_msg_blocks(msg.get("content"))
                            if blocks:
                                messages.append({
                                    "role": "assistant", "ts": ts, "model": model or None,
                                    "usage": {"input": ev["input"], "output": ev["output"],
                                              "cache_read": ev["cache_read"],
                                              "cache_creation": ev["cache_creation"]},
                                    "blocks": blocks,
                                })
                elif '"user/message"' in head:
                    d = oneline(line)
                    if not d:
                        continue
                    data = d.get("data") or {}
                    msg = data.get("message") or data
                    content = msg.get("content") or []
                    src = data.get("source") or msg.get("source") or {}
                    kind = (src.get("kind") or "").lower()
                    texts = [c.get("text") or "" for c in content if c.get("type") == "text"]
                    joined = "\n".join(texts).strip()
                    noise = (kind == "runtime-context"
                             or joined.startswith("<system-reminder>")
                             or joined.startswith("Current runtime context"))
                    if not noise:
                        meta["message_count"] += 1
                    if not title and not noise and joined:
                        title = " ".join(joined.split())[:70]
                    ti = d.get("time")
                    if ti:
                        meta["created"] = min(meta["created"] or ti, ti)
                    if full and not noise:
                        blocks = _dsh_msg_blocks([c for c in content if c.get("type") == "text"])
                        if blocks:
                            messages.append({"role": "user", "ts": ti, "model": None,
                                             "usage": None, "blocks": blocks})
                elif '"tool/result"' in head:
                    if not full:
                        continue
                    d = oneline(line)
                    if not d:
                        continue
                    data = d.get("data") or {}
                    res = (data.get("result") if data.get("result") is not None
                           else data.get("content") if data.get("content") is not None
                           else data.get("output") if data.get("output") is not None
                           else data.get("error"))
                    txt = res if isinstance(res, str) else json.dumps(res, ensure_ascii=False)
                    err = bool(data.get("isError") or data.get("error"))
                    if messages and messages[-1]["role"] == "assistant":
                        messages[-1]["blocks"].append({
                            "type": "tool_result", "text": (txt or "")[:MAX_BLOCK],
                            "is_error": err,
                        })
    except Exception:
        pass
    meta["title"] = (title or (meta["project"] + " / " + sid[:8] if meta["project"]
                               else sid[:8]))
    return meta


def dsh_index(force: bool = False) -> dict:
    """按文件增量解析：只重解压有变化的会话文件。"""
    with _DSH["lock"]:
        now = time.time()
        if not force and _DSH["files"] and (now - _DSH["at"]) < INDEX_TTL:
            return _DSH["files"]
        if _DSH["disk"] is None:
            _DSH["disk"] = _dsh_load_cache()
            for p, item in _DSH["disk"].items():
                if p not in _DSH["files"]:
                    _DSH["files"][p] = item
        changed = False
        alive = set()
        for p in _dsh_files():
            alive.add(p)
            try:
                st = os.stat(p)
            except OSError:
                continue
            key = (st.st_mtime_ns, st.st_size)
            cur = _DSH["files"].get(p)
            if cur and cur[0] == key:
                continue
            try:
                _DSH["files"][p] = (key, _parse_dsh_file(p))
            except Exception:
                _DSH["files"][p] = (key, {
                    "id": os.path.basename(os.path.dirname(p)), "events": [],
                    "message_count": 0, "models": [], "title": None, "project": "",
                    "created": None, "updated": None,
                })
            changed = True
        for p in list(_DSH["files"]):
            if p not in alive:
                del _DSH["files"][p]
                changed = True
        _DSH["at"] = now
        if changed:
            _dsh_save_cache(_DSH["files"])
        return _DSH["files"]


def dsh_sessions() -> list:
    try:
        return [v[1] for v in dsh_index().values()]
    except Exception:
        return []


_DSH_DETAIL = {"order": [], "cache": {}, "lock": threading.Lock()}


def _dsh_path_by_sid() -> dict:
    out = {}
    try:
        for p, (_key, meta) in dsh_index().items():
            out[meta.get("id")] = p
    except Exception:
        pass
    return out


def dsh_chat_detail(sid: str):
    """单个 DSH 会话的完整对话（LRU 缓存最近 4 个）。"""
    p = _dsh_path_by_sid().get(sid)
    if not p:
        return None
    with _DSH_DETAIL["lock"]:
        hit = _DSH_DETAIL["cache"].get(sid)
    if hit:
        return hit
    d = _parse_dsh_file(p, full=True)
    d.pop("events", None)
    u = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
    for msg in d.get("messages") or []:
        mu = msg.get("usage")
        if mu:
            for k in u:
                u[k] += _i(mu.get(k))
    d["usage"] = u
    with _DSH_DETAIL["lock"]:
        _DSH_DETAIL["cache"][sid] = d
        _DSH_DETAIL["order"].append(sid)
        while len(_DSH_DETAIL["order"]) > 4:
            old = _DSH_DETAIL["order"].pop(0)
            _DSH_DETAIL["cache"].pop(old, None)
    return d


def _mk_native(ev, app, provider, pname, source, slabel, sid, pricing) -> dict:
    model = ev.get("model") or "unknown"
    inp, out = _i(ev.get("input")), _i(ev.get("output"))
    cr, cc = _i(ev.get("cache_read")), _i(ev.get("cache_creation"))
    p = pricing.get(model.lower())
    if p:
        cost = (inp * p[0] + out * p[1] + cr * p[2] + cc * p[3]) / 1_000_000
    else:
        cost = 0.0
    eid = ev.get("id") or f"{ev.get('ts')}-{inp}-{out}-{cr}"
    return {
        "id": f"{source}:{eid}",
        "ts": _i(ev.get("ts")),
        "app": app,
        "app_label": APP_LABELS.get(app, app),
        "provider": provider,
        "provider_name": pname,
        "model": model,
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_tokens": cr,
        "cache_creation_tokens": cc,
        "cost_usd": round(cost, 6),
        "input_cost_usd": round(inp * p[0] / 1e6, 6) if p else 0.0,
        "output_cost_usd": round(out * p[1] / 1e6, 6) if p else 0.0,
        "cache_cost_usd": round((cr * p[2] + cc * p[3]) / 1e6, 6) if p else 0.0,
        "latency_ms": None,
        "first_token_ms": None,
        "status": 200,
        "error": None,
        "session_id": sid,
        "streaming": 1,
        "source": source,
        "source_label": slabel,
        "pricing_model": model if p else None,
    }


def _antigravity_events() -> list:
    """可选模块：Antigravity 解析器（不存在或解不出就返回空）。"""
    if _CACHE.get("ag_at") and (time.time() - _CACHE["ag_at"]) < 120:
        return _CACHE.get("ag_events") or []
    evs = []
    try:
        import antigravity as ag          # noqa: F401
        evs = ag.scan() or []
    except Exception:
        evs = []
    _CACHE["ag_events"] = evs
    _CACHE["ag_at"] = time.time()
    return evs


def _native_rows(f: dict) -> list:
    """本地直读的数据源：Claude Code jsonl / DeepSeek Harness / Antigravity。

    Claude Code 只补 cc-switch 尚未收录的会话，避免与 session_log 重复计数；
    DSH 与 Antigravity 在 cc-switch 里没有记录，全量计入。
    """
    status_f = f.get("status")
    if status_f and status_f not in ("ok", "200", "local"):
        return []
    from_ms = _i(f.get("from"))
    to_ms = _i(f.get("to"))
    model_f = f.get("model")
    app_f = f.get("app")
    q = (f.get("q") or "").strip().lower()
    pricing = _pricing()
    out = []

    def keep(ev, sid, title):
        ts = ev.get("ts") or 0
        if from_ms and ts < from_ms:
            return False
        if to_ms and ts > to_ms:
            return False
        if model_f and ev.get("model") != model_f:
            return False
        if q:
            hay = f"{ev.get('model')} {sid} {title or ''}".lower()
            if q not in hay:
                return False
        return True

    if not app_f or app_f == "claude":
        covered = _covered_sessions()
        for m in claude_index().values():
            sid = m["id"]
            if sid in covered:
                continue
            for ev in m.get("events") or []:
                if keep(ev, sid, m.get("title")):
                    out.append(_mk_native(ev, "claude", NATIVE_PROVIDER, "Claude Code 本地记录",
                                          "claude_jsonl", "Claude Code 本地记录", sid, pricing))

    if not app_f or app_f == "dsh":
        for m in dsh_sessions():
            sid = m["id"]
            for ev in m.get("events") or []:
                if keep(ev, sid, m.get("title")):
                    out.append(_mk_native(ev, "dsh", DSH_PROVIDER, "DeepSeek Harness 本地记录",
                                          "dsh_session", "DeepSeek Harness 本地记录", sid, pricing))

    if not app_f or app_f == "antigravity":
        for ev in _antigravity_events():
            if not isinstance(ev, dict):
                continue
            if keep(ev, ev.get("session_id") or "", ev.get("model")):
                out.append(_mk_native(ev, "antigravity", "_antigravity", "Antigravity 本地记录",
                                      "antigravity", "Antigravity 本地记录",
                                      ev.get("session_id") or "", pricing))

    return out


def _covered_sessions() -> set:
    key = "_covered"
    cached = _CACHE.get(key)
    if cached is not None and (time.time() - _CACHE.get("covered_at", 0)) < 30:
        return cached
    out = set()
    try:
        db = cc_db_path()
        if os.path.exists(db):
            con = _ro(db)
            for r in con.execute(
                "SELECT DISTINCT session_id FROM proxy_request_logs "
                "WHERE app_type='claude' AND data_source='session_log' AND session_id IS NOT NULL"
            ):
                out.add(r[0])
            con.close()
    except sqlite3.Error:
        pass
    _CACHE[key] = out
    _CACHE["covered_at"] = time.time()
    return out


def chats_list(f: dict) -> dict:
    q = (f.get("q") or "").strip().lower()
    src_f = (f.get("source") or "").strip().lower()
    rows = []

    if not src_f or src_f == "claude":
        sessions = claude_index(force=bool(f.get("refresh")))
        # 用 cc-switch 的 session_log 行补成本/请求数
        cost_by_sid = {}
        try:
            db = cc_db_path()
            if os.path.exists(db):
                con = _ro(db)
                for r in con.execute(
                    "SELECT session_id,COUNT(*) n,SUM(total_cost_usd) c,"
                    "SUM(input_tokens) i,SUM(output_tokens) o,SUM(cache_read_tokens) cr,"
                    "SUM(cache_creation_tokens) cc FROM proxy_request_logs "
                    "WHERE app_type='claude' AND session_id IS NOT NULL GROUP BY session_id"
                ):
                    cost_by_sid[r["session_id"]] = {
                        "requests": r["n"], "cost_usd": round(_f(r["c"]), 6),
                        "input": _i(r["i"]), "output": _i(r["o"]),
                        "cache_read": _i(r["cr"]), "cache_creation": _i(r["cc"]),
                    }
                con.close()
        except sqlite3.Error:
            pass
        for sid, m in sessions.items():
            extra = cost_by_sid.get(sid, {})
            usage = m["usage"]
            rows.append({
                "id": sid,
                "source": "claude",
                "source_label": "Claude Code",
                "title": m["title"] or sid[:8],
                "project": m["project"] or "",
                "models": m["models"],
                "created": m["created"],
                "updated": m["updated"],
                "requests": extra.get("requests", m["message_count"]),
                "message_count": m["message_count"],
                "input": usage["input"],
                "output": usage["output"],
                "cache_read": usage["cache_read"],
                "cache_creation": usage["cache_creation"],
                "cost_usd": extra.get("cost_usd", 0.0),
                "prompt_tokens": usage["input"] + usage["cache_read"] + usage["cache_creation"],
                "cache_hit_rate": _pct(usage["cache_read"],
                                       usage["input"] + usage["cache_read"] + usage["cache_creation"]),
            })

    if not src_f or src_f == "dsh":
        pricing = _pricing()
        for m in dsh_sessions():
            ev = m.get("events") or []
            u = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
            cost = 0.0
            for e in ev:
                for k in u:
                    u[k] += _i(e.get(k))
                p = pricing.get((e.get("model") or "").lower())
                if p:
                    cost += (e["input"] * p[0] + e["output"] * p[1]
                             + e["cache_read"] * p[2] + e["cache_creation"] * p[3]) / 1_000_000
            prompt = u["input"] + u["cache_read"] + u["cache_creation"]
            rows.append({
                "id": "dsh:" + m["id"],
                "source": "dsh",
                "source_label": "DeepSeek Harness",
                "title": m["title"],
                "project": m["project"] or "",
                "models": m["models"],
                "created": m["created"],
                "updated": m["updated"],
                "requests": len(ev),
                "message_count": m["message_count"],
                "input": u["input"], "output": u["output"],
                "cache_read": u["cache_read"], "cache_creation": u["cache_creation"],
                "cost_usd": round(cost, 6),
                "prompt_tokens": prompt,
                "cache_hit_rate": _pct(u["cache_read"], prompt),
            })

    if q:
        rows = [r for r in rows
                if q in (r["title"] or "").lower()
                or q in (r["project"] or "").lower()
                or q in (r["id"] or "").lower()]
    rows.sort(key=lambda r: -(r["updated"] or 0))
    total = len(rows)
    offset = max(0, _i(f.get("offset")))
    limit = max(1, min(_i(f.get("limit")) or 50, 200))
    return {"total": total, "rows": rows[offset: offset + limit], "offset": offset, "limit": limit}


def chat_detail(sid: str) -> dict | None:
    if sid.startswith("dsh:"):
        return dsh_chat_detail(sid[4:])
    sessions = claude_index()
    m = sessions.get(sid)
    if not m:
        return None
    detail = _parse_file(m["file"], full=True)
    detail.pop("file", None)
    return detail


def chat_markdown(sid: str) -> str | None:
    d = chat_detail(sid)
    if not d:
        return None
    u = d["usage"]
    lines = [
        f"# {d['title']}",
        "",
        f"- 会话 ID：`{d['id']}`",
        f"- 项目：`{d['project'] or '-'}`",
        f"- 模型：{', '.join(d['models']) or '-'}",
    ]
    if d["created"]:
        lines.append(f"- 开始：{datetime.fromtimestamp(d['created']/1000):%Y-%m-%d %H:%M:%S}")
    if d["updated"]:
        lines.append(f"- 结束：{datetime.fromtimestamp(d['updated']/1000):%Y-%m-%d %H:%M:%S}")
    lines.append(
        f"- 用量：输入 {u['input']:,} / 输出 {u['output']:,} / 缓存读 {u['cache_read']:,} "
        f"/ 缓存写 {u['cache_creation']:,}"
    )
    lines += ["", "---", ""]
    for msg in d.get("messages", []):
        who = "🧑 用户" if msg["role"] == "user" else "🤖 助手"
        stamp = (datetime.fromtimestamp(msg["ts"] / 1000).strftime("%H:%M:%S")
                 if msg.get("ts") else "")
        head = f"## {who}  <sub>{stamp}</sub>"
        if msg.get("model") and msg["role"] == "assistant":
            head += f"  \n`{msg['model']}`"
        lines.append(head)
        lines.append("")
        for b in msg.get("blocks", []):
            if b["type"] == "text":
                lines.append(b["text"])
            elif b["type"] == "thinking":
                lines += ["> 💭 思考", "> " + (b["text"] or "").replace("\n", "\n> ")]
            elif b["type"] == "tool_use":
                payload = json.dumps(b.get("input"), ensure_ascii=False, indent=2)
                lines += [f"**🔧 工具调用：{b.get('name')}**", "", "```json", payload[:8000], "```"]
            elif b["type"] == "tool_result":
                text = (b.get("text") or "")[:8000]
                lines += ["<details><summary>工具结果</summary>", "", "```", text, "```", "</details>"]
            elif b["type"] == "image":
                lines.append("_[图片]_")
            lines.append("")
        lines.append("")
    return "\n".join(lines)


def chat_export_json(sid: str) -> str | None:
    d = chat_detail(sid)
    return None if not d else json.dumps(d, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------- #
# 元信息
# --------------------------------------------------------------------------- #
def _unsupported_sources() -> list:
    """桌面上装了、但本地确实拿不到 token 的客户端 —— 明确告诉用户为什么。"""
    appdata = os.environ.get("APPDATA", "")
    out = []

    def has(p):
        return bool(p) and os.path.exists(p)

    if has(os.path.join(HOME, ".cursor")) or has(os.path.join(appdata, "Cursor")):
        out.append({"app": "cursor", "name": "Cursor",
                    "reason": "本地只记录会话与代码改动，不落 token",
                    "detail": "state.vscdb 里 49 条 bubble 的 tokenCount 全是 0"})
    if has(os.path.join(HOME, ".grokbot")):
        out.append({"app": "grokbot", "name": "Grok Bot",
                    "reason": "无本地会话/用量记录",
                    "detail": "~/.grokbot 只有配置与进程启动日志"})
    if has(os.path.join(HOME, ".workbuddy-ai")):
        out.append({"app": "workbuddy", "name": "WorkBuddy AI",
                    "reason": "用量表为空",
                    "detail": "workbuddy.db 里 sessions / session_usage 都是 0 行"})
    if has(os.path.join(HOME, ".codebuddy")):
        out.append({"app": "codebuddy", "name": "CodeBuddy",
                    "reason": "仅有内存监控日志",
                    "detail": "~/.codebuddy/logs 下只有 memwatch"})
    if has(os.path.join(HOME, "Doubao")) or has(os.path.join(appdata, "Doubao")):
        out.append({"app": "doubao", "name": "豆包",
                    "reason": "无本地用量数据", "detail": "只有 public_config.json"})
    return out


def meta() -> dict:
    cfg = load_settings()
    db = cc_db_path()
    src = {}
    rng = {"min": None, "max": None}
    apps, models = [], []
    try:
        if os.path.exists(db):
            con = _ro(db)
            n = con.execute("SELECT COUNT(*) FROM proxy_request_logs").fetchone()[0]
            mn, mx = con.execute("SELECT MIN(created_at),MAX(created_at) FROM proxy_request_logs").fetchone()
            apps = [r[0] for r in con.execute("SELECT DISTINCT app_type FROM proxy_request_logs ORDER BY 1")]
            models = [r[0] for r in con.execute(
                "SELECT model,COUNT(*) c FROM proxy_request_logs GROUP BY 1 ORDER BY 2 DESC LIMIT 60")]
            models = [m for m in models if m]
            con.close()
            src["cc_switch"] = {
                "path": db, "ok": True, "rows": n,
                "mtime": int(os.path.getmtime(db)),
                "range": {"min": mn * 1000 if mn else None, "max": mx * 1000 if mx else None},
            }
            rng = {"min": mn * 1000 if mn else None, "max": mx * 1000 if mx else None}
        else:
            src["cc_switch"] = {"path": db, "ok": False, "rows": 0, "error": "数据库不存在"}
    except sqlite3.Error as e:
        src["cc_switch"] = {"path": db, "ok": False, "rows": 0, "error": str(e)}

    files = _claude_files()
    sessions = 0
    try:
        sessions = len(claude_index())
    except Exception:
        pass
    src["claude"] = {
        "path": os.path.join(claude_dir(), "projects"),
        "ok": bool(files), "files": len(files), "sessions": sessions,
    }

    native_events = _CACHE.get("events") or []
    native_max = max((e["ts"] for e in native_events), default=None)
    src["native"] = {
        "ok": bool(native_events),
        "events": len(native_events),
        "latest": native_max,
    }
    if native_max:
        rng["max"] = max(rng["max"] or 0, native_max)
        rng["min"] = min(rng["min"] or native_max, native_max)

    # DeepSeek Harness
    dsh_s = dsh_sessions()
    dsh_n = sum(len(m.get("events") or []) for m in dsh_s)
    dsh_latest = max((e["ts"] for m in dsh_s for e in (m.get("events") or [])), default=None)
    src["dsh"] = {
        "path": os.path.join(dsh_dir(), "sessions"),
        "ok": dsh_n > 0, "sessions": len(dsh_s), "events": dsh_n, "latest": dsh_latest,
    }

    # Antigravity（可选模块）
    ag = _antigravity_events()
    ag_latest = max((e.get("ts") or 0 for e in ag), default=None)
    src["antigravity"] = {
        "path": antigravity_dir(), "ok": bool(ag), "events": len(ag), "latest": ag_latest,
    }

    for latest in (dsh_latest, ag_latest):
        if latest:
            rng["max"] = max(rng["max"] or 0, latest)
            rng["min"] = min(rng["min"] or latest, latest)

    # 数据面覆盖不到、但用户桌面上装着的客户端：明确列出原因
    src["unsupported"] = _unsupported_sources()

    app_set = set(apps or [])
    if dsh_n:
        app_set.add("dsh")
    if ag:
        app_set.add("antigravity")
    model_set = set(models or [])
    for m in dsh_s:
        for mm in m.get("models") or []:
            model_set.add(mm)
    for e in ag:
        if e.get("model"):
            model_set.add(e["model"])
    apps = sorted(app_set)
    models = sorted(model_set)

    return {
        "app": {"name": "TokenLens", "version": "1.0.0"},
        "sources": src,
        "range": rng,
        "apps": apps,
        "models": models,
        "settings": {k: cfg[k] for k in ("port", "auto_refresh_sec", "balance_refresh_sec")},
        "server_time": int(time.time() * 1000),
    }


def providers_raw():
    """给 balances 模块用的 provider 原始行（含 settings_config）。"""
    db = cc_db_path()
    if not os.path.exists(db):
        return []
    con = _ro(db)
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT id,app_type,name,settings_config,website_url,category,is_current "
            "FROM providers ORDER BY app_type,sort_index"
        )]
    finally:
        con.close()
    endpoints = {}
    try:
        con = _ro(db)
        for r in con.execute("SELECT provider_id,url FROM provider_endpoints"):
            endpoints.setdefault(r["provider_id"], []).append(r["url"])
        con.close()
    except sqlite3.Error:
        pass
    for r in rows:
        r["endpoints"] = endpoints.get(r["id"], [])
    return rows


def warm_up():
    """后台预热会话索引，避免首次打开聊天页卡顿。"""
    def run():
        try:
            claude_index()
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()
