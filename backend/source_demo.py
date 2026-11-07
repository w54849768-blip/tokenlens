"""数据源第 1 号：内置示例数据（无任何依赖，零第三方包）。

作用有两个：

1. **开箱即看**：没装 Claude Code / DS Harness 的人 clone 下来就能看到完整界面。
2. **迁移参考**：这里用一份统一的「事件列表」结构，把聚合、筛选、成本、
   会话还原全部写完。云端仪表盘要做的事就是——

       你的数据来源  →  归一化成 RequestEvent  →  后面全部照抄

   任何数据源（Postgres 里的请求表、对象存储里的 JSONL、S3 上的日志）只要能
   产出 RequestEvent，就能直接复用本模块的 `DemoSource` 后半段逻辑。

数据结构（也就是 TokenLens 的「领域模型」）：

    RequestEvent = {
      ts: int(ms), app: str, provider: str, model: str,
      input: int, output: int, cache_read: int, cache_creation: int,
      cost_usd: float, latency_ms: int | None, status: int | None,
      session_id: str, source: str,
    }

运行：
    python backend/server.py --demo
"""

from __future__ import annotations

import csv
import io
import json
import os
import random
import threading
import time
import zipfile
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")

# --------------------------------------------------------------------------- #
# 示例数据的「世界观」：3 个应用、6 个模型、3 个供应商
# --------------------------------------------------------------------------- #
APPS = {
    "dsh": "DeepSeek Harness",
    "claude": "Claude Code",
    "codex": "Codex",
}
PROVIDERS = {
    "deepseek": "DeepSeek 官方",
    "anthropic": "Anthropic 官方",
    "openrouter": "OpenRouter",
}
# (模型, 供应商, 应用, $in, $out, $cache_read, $cache_write, 基础延迟ms)
MODELS = [
    ("deepseek-v4-flash", "deepseek", "dsh", 0.14, 0.28, 0.014, 0.14, 2200),
    ("deepseek-v4-pro", "deepseek", "dsh", 0.55, 2.19, 0.055, 0.55, 4800),
    ("claude-sonnet-5", "anthropic", "claude", 3.00, 15.00, 0.30, 3.75, 3100),
    ("claude-opus-5", "anthropic", "claude", 15.00, 75.00, 1.50, 18.75, 6200),
    ("gpt-5.6", "openrouter", "codex", 1.25, 10.00, 0.125, 1.25, 5400),
    ("gemini-4-flash", "openrouter", "codex", 0.30, 2.50, 0.03, 0.30, 1900),
]
APP_WEIGHT = {"dsh": 0.62, "claude": 0.26, "codex": 0.12}

TITLES = [
    "给仪表盘加一个缓存命中率指标",
    "把后端聚合逻辑拆成可插拔数据源",
    "查一下这个正则为什么匹配不上中文",
    "写个脚本把会话导出成 Markdown",
    "重构请求日志的分页接口",
    "调试 WebView2 在高分屏下的缩放",
    "给图表库补一个堆叠柱状图",
    "整理一下供应商余额接口的错误码",
    "这批 token 成本为什么偏高",
    "把 CSS 变量统一成一套设计系统",
]


def _pct(a, b):
    return round(a / b, 4) if b else None


def _i(v, d=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return d


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
# 生成示例事件（固定种子：每次生成的数据完全一样，便于比对）
# --------------------------------------------------------------------------- #
def generate_events(days: int = 14, seed: int = 20261107) -> list:
    rnd = random.Random(seed)
    end = int(time.time() * 1000)
    start = end - days * 86400 * 1000
    events = []
    for app, label in APPS.items():
        models = [m for m in MODELS if m[2] == app]
        weight = APP_WEIGHT[app]
        per_day = int(160 * weight) + 8
        for d in range(days):
            day0 = start + d * 86400 * 1000
            # 工作日更忙一点，周末打七折
            dow = datetime.fromtimestamp(day0 / 1000).weekday()
            scale = 0.7 if dow >= 5 else 1.0
            n = int(rnd.gauss(per_day, per_day * 0.22) * scale)
            n = max(4, n)
            for _ in range(n):
                model, provider, _app, p_in, p_out, p_cr, p_cw, base_lat = rnd.choice(models)
                # 一次「请求」= 一条用户消息触发的完整回合，token 量偏大是正常的
                inp = int(rnd.lognormvariate(8.6, 0.75)) + 400
                cache_read = int(inp * rnd.uniform(0.55, 0.97)) if app != "codex" else int(inp * rnd.uniform(0.0, 0.4))
                cache_create = int(inp * rnd.uniform(0.02, 0.18))
                out = int(rnd.lognormvariate(6.9, 0.6)) + 30
                cost = (inp * p_in + out * p_out + cache_read * p_cr + cache_create * p_cw) / 1_000_000
                err = rnd.random() < 0.03
                status = rnd.choice([500, 429, 529]) if err else 200
                latency = None if err else int(rnd.gauss(base_lat, base_lat * 0.45) + 200)
                ts = day0 + int(rnd.random() * 86400 * 1000)
                sid = f"{app}-{d:02d}-{rnd.randrange(1000, 9999)}"
                events.append({
                    "ts": min(ts, end),
                    "app": app,
                    "provider": provider,
                    "model": model,
                    "input": inp,
                    "output": out,
                    "cache_read": cache_read,
                    "cache_creation": cache_create,
                    "cost_usd": round(cost, 6),
                    "latency_ms": latency,
                    "status": status,
                    "session_id": sid,
                    "source": "demo",
                })
    events.sort(key=lambda e: e["ts"])
    return events


# --------------------------------------------------------------------------- #
# 会话：从事件里按 session_id 聚合成一段对话（示例正文是编的）
# --------------------------------------------------------------------------- #
def build_sessions(events: list) -> dict:
    by_sid = {}
    for e in events:
        by_sid.setdefault(e["session_id"], []).append(e)
    sessions = {}
    rnd = random.Random(7)
    for i, (sid, evs) in enumerate(sorted(by_sid.items(), key=lambda kv: -kv[1][0]["ts"])):
        evs.sort(key=lambda e: e["ts"])
        title = TITLES[i % len(TITLES)]
        models = sorted({e["model"] for e in evs})
        messages = []
        for j, e in enumerate(evs):
            messages.append({
                "role": "user", "ts": e["ts"] - 40000, "model": None, "usage": None,
                "blocks": [{"type": "text", "text": f"（示例会话）第 {j + 1} 轮：{title}"}],
            })
            blocks = [{"type": "thinking", "text": "先看现有结构，再决定改哪一层。"},
                      {"type": "text", "text": (
                          f"这一轮用了 `{e['model']}`，输入 {e['input']:,} / 输出 {e['output']:,} "
                          f"/ 缓存读 {e['cache_read']:,} tokens，成本 ${e['cost_usd']:.4f}。")}]
            if rnd.random() < 0.5:
                blocks.insert(1, {"type": "tool_use", "name": "Bash",
                                  "input": {"command": "python backend/server.py --demo"}})
                blocks.insert(2, {"type": "tool_result",
                                  "text": "TokenLens · 示例数据\nhttp://127.0.0.1:8787/",
                                  "is_error": False})
            messages.append({
                "role": "assistant", "ts": e["ts"], "model": e["model"],
                "usage": {"input": e["input"], "output": e["output"],
                          "cache_read": e["cache_read"], "cache_creation": e["cache_creation"]},
                "blocks": blocks,
            })
        u = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
        for e in evs:
            for k in u:
                u[k] += e[k]
        sessions[sid] = {
            "id": sid,
            "title": title,
            "project": "~/demo-project" if evs[0]["app"] == "claude" else "demo/workspace",
            "models": models,
            "created": evs[0]["ts"],
            "updated": evs[-1]["ts"],
            "message_count": len(messages),
            "usage": u,
            "cost_usd": round(sum(e["cost_usd"] for e in evs), 6),
            "events": evs,
            "messages": messages,
        }
    return sessions


# --------------------------------------------------------------------------- #
# 数据源实现
# --------------------------------------------------------------------------- #
class DemoSource:
    name = "demo"

    def __init__(self):
        self._lock = threading.Lock()
        self._events = None
        self._sessions = None

    # ---- 懒加载 ----
    def events(self) -> list:
        with self._lock:
            if self._events is None:
                self._events = generate_events()
                self._sessions = build_sessions(self._events)
            return self._events

    def sessions(self) -> dict:
        self.events()
        return self._sessions

    # ---- 必需的 7 个端点 ----
    def meta(self):
        ev = self.events()
        n_app = len(APPS)
        rows_per_app = len(ev) // max(1, n_app)
        return {
            "app": {"name": "TokenLens", "version": "1.0.0-demo"},
            "source": {"name": self.name, "read_only": True, "mode": "demo",
                       "hint": "内置示例数据，数值均为合成，不代表任何真实用量"},
            # 前端的状态区按这三个键位读取；示例模式给出真实的合成计数，
            # 保证界面外观与单机版完全一致（不为了 demo 改 UI）。
            "sources": {
                "cc_switch": {"path": "(内置示例，合成数据)", "ok": True,
                              "rows": rows_per_app,
                              "range": {"min": ev[0]["ts"], "max": ev[-1]["ts"]}},
                "claude": {"path": "(内置示例，合成数据)", "ok": True,
                           "files": rows_per_app, "sessions": rows_per_app},
                "dsh": {"path": "(内置示例，合成数据)", "ok": True,
                        "sessions": rows_per_app, "events": rows_per_app,
                        "latest": ev[-1]["ts"]},
                "antigravity": {"path": "", "ok": False, "events": 0},
                "native": {"ok": True, "events": len(ev), "latest": ev[-1]["ts"]},
                "demo": {"path": "(内置)", "ok": True, "rows": len(ev),
                         "range": {"min": ev[0]["ts"], "max": ev[-1]["ts"]}},
                "unsupported": [],
            },
            "range": {"min": ev[0]["ts"], "max": ev[-1]["ts"]},
            "apps": sorted({e["app"] for e in ev}),
            "models": sorted({e["model"] for e in ev}),
            "settings": self.get_settings(),
            "server_time": int(time.time() * 1000),
        }

    def _filter(self, f):
        from_ms, to_ms = _i(f.get("from")), _i(f.get("to"))
        app, model, status = f.get("app"), f.get("model"), f.get("status")
        q = (f.get("q") or "").strip().lower()
        out = []
        for e in self.events():
            if from_ms and e["ts"] < from_ms:
                continue
            if to_ms and e["ts"] > to_ms:
                continue
            if app and e["app"] != app:
                continue
            if model and e["model"] != model:
                continue
            if status:
                st = e["status"] or 0
                ok = st < 400
                if status == "error" and ok:
                    continue
                if status == "ok" and not ok:
                    continue
                if status == "4xx" and not (400 <= st < 500):
                    continue
                if status == "5xx" and st < 500:
                    continue
                if status.isdigit() and st != int(status):
                    continue
            if q and q not in f"{e['model']} {e['session_id']} {e['provider']}".lower():
                continue
            out.append(e)
        return out

    def list_requests(self, f):
        rows = self._filter(f)
        order = (f.get("order") or "-ts").strip()
        desc = order.startswith("-")
        key = order.lstrip("-")
        keyfn = {
            "ts": lambda r: r["ts"],
            "cost": lambda r: r["cost_usd"],
            "latency": lambda r: r["latency_ms"] or 0,
            "tokens": lambda r: r["input"] + r["output"] + r["cache_read"],
        }.get(key, lambda r: r["ts"])
        rows = sorted(rows, key=keyfn, reverse=desc)
        offset = max(0, _i(f.get("offset")))
        limit = max(1, min(_i(f.get("limit")) or 50, 500))
        shaped = [self._row(r) for r in rows[offset: offset + limit]]
        return {"total": len(rows), "rows": shaped, "offset": offset, "limit": limit}

    @staticmethod
    def _row(e):
        return {
            "id": f"{e['session_id']}-{e['ts']}",
            "ts": e["ts"],
            "app": e["app"],
            "app_label": APPS.get(e["app"], e["app"]),
            "provider": e["provider"],
            "provider_name": PROVIDERS.get(e["provider"], e["provider"]),
            "model": e["model"],
            "input_tokens": e["input"],
            "output_tokens": e["output"],
            "cache_read_tokens": e["cache_read"],
            "cache_creation_tokens": e["cache_creation"],
            "cost_usd": e["cost_usd"],
            "input_cost_usd": 0.0,
            "output_cost_usd": 0.0,
            "cache_cost_usd": 0.0,
            "latency_ms": e["latency_ms"],
            "first_token_ms": None,
            "status": e["status"],
            "error": None,
            "session_id": e["session_id"],
            "streaming": 1,
            "source": e["source"],
            "source_label": "示例数据",
            "pricing_model": e["model"],
        }

    def overview(self, f):
        f = dict(f)
        now_ms = int(time.time() * 1000)
        if not f.get("from") and not f.get("to"):
            probe = self._filter({k: f.get(k) for k in ("app", "model", "status", "q")})
            to_ms = max((e["ts"] for e in probe), default=now_ms)
            f["from"] = to_ms - 13 * 86400 * 1000
            f["to"] = to_ms

        rows = self._filter(f)
        to_ms = _i(f.get("to")) or (max([e["ts"] for e in rows]) if rows else now_ms)
        from_ms = _i(f.get("from")) or (to_ms - 13 * 86400 * 1000)
        span = max(1, to_ms - from_ms)
        gran = "hour" if span <= 2 * 86400 * 1000 else "day"

        lat = [e["latency_ms"] for e in rows if e["latency_ms"]]
        totals = self._totals(rows, lat)

        prev_f = dict(f)
        prev_f["from"] = from_ms - span
        prev_f["to"] = from_ms
        prev_rows = self._filter(prev_f)
        prev = self._totals(prev_rows, [e["latency_ms"] for e in prev_rows if e["latency_ms"]])
        prev_summary = {k: prev[k] for k in
                        ("requests", "cost_usd", "cache_hit_rate", "avg_latency_ms",
                         "total_tokens", "errors")}

        buckets = {}
        step = 3600 * 1000 if gran == "hour" else 86400 * 1000
        cur = _bucket_start(from_ms // 1000, gran)
        end = _bucket_start(to_ms // 1000, gran)
        while cur <= end:
            buckets[cur] = {"t": cur, "label": _bucket_label(cur, gran), "requests": 0,
                            "errors": 0, "cost_usd": 0.0, "input_tokens": 0,
                            "output_tokens": 0, "cache_read_tokens": 0,
                            "cache_creation_tokens": 0, "lat_sum": 0, "lat_n": 0}
            cur += step

        for e in rows:
            b = buckets.get(_bucket_start(e["ts"] // 1000, gran))
            if not b:
                continue
            b["requests"] += 1
            b["errors"] += 1 if (e["status"] or 0) >= 400 else 0
            b["cost_usd"] += e["cost_usd"]
            b["input_tokens"] += e["input"]
            b["output_tokens"] += e["output"]
            b["cache_read_tokens"] += e["cache_read"]
            b["cache_creation_tokens"] += e["cache_creation"]
            if e["latency_ms"]:
                b["lat_sum"] += e["latency_ms"]
                b["lat_n"] += 1

        series = []
        for b in buckets.values():
            item = dict(b)
            item["avg_latency_ms"] = int(b["lat_sum"] / b["lat_n"]) if b["lat_n"] else None
            item["cost_usd"] = round(b["cost_usd"], 6)
            item.pop("lat_sum")
            item.pop("lat_n")
            series.append(item)

        def group(key_fn):
            acc = {}
            for e in rows:
                k = key_fn(e)
                g = acc.setdefault(k, {"key": k, "requests": 0, "errors": 0, "cost_usd": 0.0,
                                       "input": 0, "output": 0, "cache_read": 0,
                                       "cache_creation": 0})
                g["requests"] += 1
                g["errors"] += 1 if (e["status"] or 0) >= 400 else 0
                g["cost_usd"] += e["cost_usd"]
                g["input"] += e["input"]
                g["output"] += e["output"]
                g["cache_read"] += e["cache_read"]
                g["cache_creation"] += e["cache_creation"]
            out = sorted(acc.values(), key=lambda x: -x["requests"])
            for g in out:
                g["cost_usd"] = round(g["cost_usd"], 6)
                g["tokens"] = g["input"] + g["output"] + g["cache_read"] + g["cache_creation"]
                g["prompt_tokens"] = g["input"] + g["cache_read"] + g["cache_creation"]
                g["cache_hit_rate"] = _pct(g["cache_read"], g["prompt_tokens"])
            return out

        by_provider = group(lambda e: e["provider"])
        for g in by_provider:
            g["name"] = PROVIDERS.get(g["key"], g["key"])
        by_app = group(lambda e: e["app"])
        for g in by_app:
            g["name"] = APPS.get(g["key"], g["key"])
        by_model = group(lambda e: e["model"])

        bucket_keys = list(buckets.keys())
        idx_map = {k: i for i, k in enumerate(bucket_keys)}
        models_by_app, acc_app = {}, {}
        for g in by_app:
            acc_app[g["key"]] = [{"requests": 0, "tokens": 0, "cost_usd": 0.0} for _ in bucket_keys]
        for e in rows:
            a = e["app"]
            tok = e["input"] + e["output"] + e["cache_read"] + e["cache_creation"]
            i = idx_map.get(_bucket_start(e["ts"] // 1000, gran))
            if i is not None and a in acc_app:
                cell = acc_app[a][i]
                cell["requests"] += 1
                cell["tokens"] += tok
                cell["cost_usd"] += e["cost_usd"]
            mm = models_by_app.setdefault(a, {})
            x = mm.setdefault(e["model"], {"model": e["model"], "requests": 0,
                                           "tokens": 0, "cost_usd": 0.0})
            x["requests"] += 1
            x["tokens"] += tok
            x["cost_usd"] += e["cost_usd"]

        for g in by_app:
            g["last_ts"] = max((e["ts"] for e in rows if e["app"] == g["key"]), default=None)
            lst = sorted(models_by_app.get(g["key"], {}).values(), key=lambda x: -x["requests"])[:6]
            for x in lst:
                x["cost_usd"] = round(x["cost_usd"], 6)
            g["models"] = lst
            g["cost_share"] = _pct(g["cost_usd"], totals["cost_usd"])
            g["token_share"] = _pct(g["tokens"], totals["total_tokens"])

        series_by_app = [{
            "app": g["key"], "name": g["name"],
            "requests": [c["requests"] for c in acc_app[g["key"]]],
            "tokens": [c["tokens"] for c in acc_app[g["key"]]],
            "cost": [round(c["cost_usd"], 6) for c in acc_app[g["key"]]],
        } for g in by_app]

        status_codes = {}
        for e in rows:
            c = e["status"] or 0
            status_codes[c] = status_codes.get(c, 0) + 1

        edges = [(0, 1000, "<1s"), (1000, 3000, "1–3s"), (3000, 5000, "3–5s"),
                 (5000, 10000, "5–10s"), (10000, 30000, "10–30s"), (30000, 10**9, ">30s")]

        return {
            "range": {"from": from_ms, "to": to_ms, "granularity": gran,
                      "label": _bucket_label(from_ms, gran) + " → " + _bucket_label(to_ms, gran)},
            "totals": totals,
            "prev": prev_summary,
            "series": series,
            "series_by_app": series_by_app,
            "by_app": by_app,
            "by_model": by_model[:14],
            "by_provider": by_provider[:10],
            "status_codes": [{"code": k, "count": v} for k, v in sorted(status_codes.items())],
            "latency_buckets": [{"label": lbl,
                                 "count": sum(1 for v in lat if lo <= v < hi)}
                                for lo, hi, lbl in edges],
        }

    @staticmethod
    def _totals(rows, lat):
        t = {
            "requests": len(rows),
            "errors": sum(1 for e in rows if (e["status"] or 0) >= 400),
            "input_tokens": sum(e["input"] for e in rows),
            "output_tokens": sum(e["output"] for e in rows),
            "cache_read_tokens": sum(e["cache_read"] for e in rows),
            "cache_creation_tokens": sum(e["cache_creation"] for e in rows),
            "cost_usd": round(sum(e["cost_usd"] for e in rows), 6),
        }
        t["prompt_tokens"] = t["input_tokens"] + t["cache_read_tokens"] + t["cache_creation_tokens"]
        t["total_tokens"] = t["prompt_tokens"] + t["output_tokens"]
        t["success_rate"] = _pct(t["requests"] - t["errors"], t["requests"])
        t["cache_hit_rate"] = _pct(t["cache_read_tokens"], t["prompt_tokens"])
        t["avg_latency_ms"] = int(sum(lat) / len(lat)) if lat else None
        t["p50_latency_ms"] = _percentile(lat, 0.50)
        t["p95_latency_ms"] = _percentile(lat, 0.95)
        t["first_token_avg_ms"] = None
        return t

    def requests_csv(self, f):
        f = dict(f)
        f["limit"], f["offset"] = 500, 0
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["时间", "应用", "供应商", "模型", "输入", "输出", "缓存读", "缓存写",
                    "成本USD", "延迟ms", "首字ms", "状态码", "错误", "会话", "来源"])
        for r in self.list_requests(f)["rows"]:
            w.writerow([
                datetime.fromtimestamp(r["ts"] / 1000).strftime("%Y-%m-%d %H:%M:%S"),
                r["app_label"], r["provider_name"], r["model"], r["input_tokens"],
                r["output_tokens"], r["cache_read_tokens"], r["cache_creation_tokens"],
                r["cost_usd"], r["latency_ms"] or "", r["first_token_ms"] or "",
                r["status"] or "", r["error"] or "", r["session_id"] or "", r["source_label"],
            ])
        return "\ufeff" + buf.getvalue()   # BOM：Excel 打开中文不乱码

    def chats_list(self, f):
        q = (f.get("q") or "").strip().lower()
        src_f = (f.get("source") or "").strip().lower()
        rows = []
        for sid, s in self.sessions().items():
            app = s["events"][0]["app"]
            if src_f and src_f != app:
                continue
            u = s["usage"]
            rows.append({
                "id": sid if app != "dsh" else "dsh:" + sid,
                "source": app,
                "source_label": APPS.get(app, app),
                "title": s["title"],
                "project": s["project"],
                "models": s["models"],
                "created": s["created"],
                "updated": s["updated"],
                "requests": len(s["events"]),
                "message_count": s["message_count"],
                "input": u["input"], "output": u["output"],
                "cache_read": u["cache_read"], "cache_creation": u["cache_creation"],
                "cost_usd": s["cost_usd"],
                "prompt_tokens": u["input"] + u["cache_read"] + u["cache_creation"],
                "cache_hit_rate": _pct(u["cache_read"],
                                       u["input"] + u["cache_read"] + u["cache_creation"]),
            })
        if q:
            rows = [r for r in rows if q in r["title"].lower() or q in r["project"].lower()]
        rows.sort(key=lambda r: -(r["updated"] or 0))
        offset = max(0, _i(f.get("offset")))
        limit = max(1, min(_i(f.get("limit")) or 50, 200))
        return {"total": len(rows), "rows": rows[offset: offset + limit],
                "offset": offset, "limit": limit}

    def _session(self, sid):
        s = self.sessions().get(sid) or self.sessions().get(sid[4:] if sid.startswith("dsh:") else sid)
        return s

    def chat_detail(self, sid):
        s = self._session(sid)
        if not s:
            return None
        return {"id": sid, "title": s["title"], "project": s["project"],
                "models": s["models"], "created": s["created"], "updated": s["updated"],
                "usage": s["usage"], "messages": s["messages"]}

    def chat_markdown(self, sid):
        d = self.chat_detail(sid)
        if not d:
            return None
        u = d["usage"]
        lines = [f"# {d['title']}", "", f"- 会话 ID：`{d['id']}`",
                 f"- 项目：`{d['project'] or '-'}`", f"- 模型：{', '.join(d['models']) or '-'}",
                 f"- 用量：输入 {u['input']:,} / 输出 {u['output']:,} "
                 f"/ 缓存读 {u['cache_read']:,} / 缓存写 {u['cache_creation']:,}"]
        lines += ["", "---", ""]
        for msg in d.get("messages") or []:
            who = "🧑 用户" if msg["role"] == "user" else "🤖 助手"
            lines.append(f"## {who}")
            lines.append("")
            for b in msg.get("blocks") or []:
                if b["type"] == "text":
                    lines.append(b["text"])
                elif b["type"] == "thinking":
                    lines += ["> 💭 思考", "> " + (b.get("text") or "")]
                elif b["type"] == "tool_use":
                    lines += [f"**🔧 工具调用：{b.get('name')}**", "", "```json",
                              json.dumps(b.get("input"), ensure_ascii=False, indent=2), "```"]
                elif b["type"] == "tool_result":
                    lines += ["```", b.get("text") or "", "```"]
                lines.append("")
        return "\n".join(lines)

    def chat_export_json(self, sid):
        d = self.chat_detail(sid)
        return None if not d else json.dumps(d, ensure_ascii=False, indent=2)

    def export_all_chats(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            index = []
            for sid, s in self.sessions().items():
                z.writestr(f"{sid}.md", self.chat_markdown(sid) or "")
                index.append({"id": sid, "title": s["title"]})
            z.writestr("index.json", json.dumps(index, ensure_ascii=False, indent=2))
        return buf.getvalue()

    def providers(self):
        return [{"id": pid, "name": name, "app_type": "", "app_label": "",
                 "category": "official", "website_url": "", "is_current": False,
                 "endpoints": []} for pid, name in PROVIDERS.items()]

    def balances(self, force: bool = False):
        return {"checked_at": int(time.time() * 1000), "demo": True, "items": [
            {"id": "deepseek", "name": "DeepSeek 官方", "app_type": "dsh / claude",
             "base_url": "https://api.deepseek.com", "adapter": "deepseek", "ok": True,
             "balance": 42.86, "currency": "CNY", "detail": "示例数据：余额 42.86 CNY",
             "error": None, "latency_ms": 120, "category": "official"},
            {"id": "openrouter", "name": "OpenRouter", "app_type": "codex",
             "base_url": "https://openrouter.ai/api/v1", "adapter": "openrouter", "ok": True,
             "balance": 18.0, "currency": "USD", "detail": "示例数据：余额 18.00 USD",
             "error": None, "latency_ms": 210, "category": "aggregator"},
        ]}

    # ---- 设置（示例模式不落盘）----
    def get_settings(self):
        return {"port": 8787, "auto_refresh_sec": 30, "balance_refresh_sec": 600}

    def save_settings(self, patch):
        return self.get_settings()

    def warm_up(self):
        def run():
            try:
                self.events()
            except Exception:
                pass
        threading.Thread(target=run, daemon=True).start()


SOURCE = DemoSource()
