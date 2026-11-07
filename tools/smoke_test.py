#!/usr/bin/env python3
"""TokenLens API 自检：起一个示例数据源，把 7 个端点全跑一遍。

用法：
    python tools/smoke_test.py            # 示例数据（零依赖，CI 也跑这个）
    python tools/smoke_test.py --local    # 本机数据（需要你电脑上真有客户端）

退出码 0 = 全部通过；非 0 = 有接口没通过（打印原因）。
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

import server  # noqa: E402


def get(port: int, path: str):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=60) as r:
        body = r.read()
        ctype = r.headers.get("Content-Type", "")
        return (json.loads(body.decode("utf-8")) if "json" in ctype else body), ctype


def main() -> int:
    local = "--local" in sys.argv
    if local:
        from source_local import SOURCE
    else:
        from source_demo import SOURCE
    server.setup(SOURCE)
    src = SOURCE
    try:
        src.warm_up()
    except Exception:
        pass

    port = 8799
    srv = server.build_server("127.0.0.1", port)
    import threading
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.4)

    failures = []

    def check(name, fn):
        try:
            fn()
            print(f"  ✓ {name}")
        except Exception as exc:
            failures.append((name, exc))
            print(f"  ✗ {name}: {exc}")

    try:
        meta = {}

        def t_meta():
            d, _ = get(port, "/api/meta")
            meta.update(d)
            assert d.get("apps"), "apps 为空"
            assert d.get("models"), "models 为空"
            assert isinstance(d.get("sources"), dict), "sources 必须是 dict"
            assert d.get("source"), "缺少 source 元信息（数据源模式）"

        def t_overview():
            d, _ = get(port, "/api/overview")
            t = d["totals"]
            for k in ("requests", "cost_usd", "total_tokens", "cache_hit_rate",
                      "p95_latency_ms", "success_rate", "errors", "input_tokens",
                      "output_tokens", "cache_read_tokens", "cache_creation_tokens"):
                assert k in t, f"totals 缺字段 {k}"
            assert d["series"], "series 为空"
            assert d["by_app"], "by_app 为空"
            assert d["by_model"], "by_model 为空"
            assert d["series_by_app"], "series_by_app 为空"
            assert d["status_codes"], "status_codes 为空"
            assert d["latency_buckets"], "latency_buckets 为空"
            assert "prev" in d, "缺少 prev（环比）"

        def t_overview_range():
            # 「全部」：超大时间窗，不能让接口因数据量大而崩
            d, _ = get(port, "/api/overview?from=0&to=9999999999999")
            assert d["totals"]["requests"] >= 0

        def t_requests():
            d, _ = get(port, "/api/requests?limit=5")
            assert d["total"] > 0
            assert d["rows"], "rows 为空"
            r = d["rows"][0]
            for k in ("ts", "app", "model", "input_tokens", "output_tokens",
                      "cache_read_tokens", "cache_creation_tokens", "cost_usd",
                      "latency_ms", "status", "session_id"):
                assert k in r, f"请求行缺字段 {k}"

        def t_requests_filter():
            d, _ = get(port, "/api/requests?status=error&limit=5")
            for r in d["rows"]:
                assert (r["status"] or 0) >= 400, "status=error 过滤失效"

        def t_requests_order():
            d, _ = get(port, "/api/requests?limit=5&order=-cost")
            costs = [r["cost_usd"] for r in d["rows"]]
            assert costs == sorted(costs, reverse=True), "order=-cost 排序失效"

        def t_csv():
            body, ctype = get(port, "/api/requests/export")
            assert b"\xef\xbb\xbf" in body[:4], "CSV 缺少 BOM"
            assert b"\xe6\x97\xb6\xe9\x97\xb4" in body[:80], "CSV 缺少中文表头"

        def t_chats():
            d, _ = get(port, "/api/chats?limit=5")
            assert d["total"] > 0
            assert d["rows"], "会话列表为空"
            assert d["rows"][0]["id"], "会话缺少 id"

        def t_chat_detail():
            d, _ = get(port, "/api/chats?limit=1")
            sid = d["rows"][0]["id"]
            detail, _ = get(port, "/api/chats/" + urllib.parse.quote(sid, safe=""))
            assert detail["messages"], "会话没有 messages"
            assert "blocks" in detail["messages"][0], "消息缺少 blocks"

        def t_chat_export():
            d, _ = get(port, "/api/chats?limit=1")
            sid = d["rows"][0]["id"]
            body, _ = get(port, "/api/chats/" + urllib.parse.quote(sid, safe="") + "/export?format=md")
            assert b"# " in body, "Markdown 导出为空"

        def t_chats_zip():
            body, ctype = get(port, "/api/chats/export-all")
            assert body[:2] == b"PK", "zip 导出无效"
            assert "zip" in ctype

        def t_balances():
            d, _ = get(port, "/api/balances")
            assert "items" in d
            for it in d["items"]:
                assert "ok" in it, "余额项缺少 ok 字段"
                # 凭据绝不能被返回
                blob = json.dumps(it, ensure_ascii=False)
                assert "api_key" not in blob and "sk-" not in blob, "余额响应疑似泄漏凭据"

        def t_providers():
            d, _ = get(port, "/api/providers")
            assert isinstance(d["items"], list)
            for it in d["items"]:
                assert "settings_config" not in it, "供应商列表不得含凭据字段"

        def t_settings():
            d, _ = get(port, "/api/settings")
            assert "port" in d["settings"]

        def t_static():
            req = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10)
            body = req.read()
            assert b"TokenLens" in body, "首页没渲染出来"

        print(f"\nTokenLens API 自检（{'本机数据' if local else '示例数据'}）")
        for name, fn in [
            ("GET /api/meta", t_meta),
            ("GET /api/overview", t_overview),
            ("GET /api/overview 超大时间窗", t_overview_range),
            ("GET /api/requests", t_requests),
            ("GET /api/requests 状态过滤", t_requests_filter),
            ("GET /api/requests 成本排序", t_requests_order),
            ("GET /api/requests/export CSV", t_csv),
            ("GET /api/chats", t_chats),
            ("GET /api/chats/{id}", t_chat_detail),
            ("GET /api/chats/{id}/export", t_chat_export),
            ("GET /api/chats/export-all", t_chats_zip),
            ("GET /api/balances（不泄漏凭据）", t_balances),
            ("GET /api/providers（不泄漏凭据）", t_providers),
            ("GET /api/settings", t_settings),
            ("GET / 静态页面", t_static),
        ]:
            check(name, fn)
    finally:
        srv.shutdown()

    print()
    if failures:
        print(f"❌ {len(failures)} 项失败")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    import urllib.parse  # noqa: E402  (t_chat_detail / t_chat_export 里用到)
    sys.exit(main())
