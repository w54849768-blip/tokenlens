"""TokenLens 本地服务：默认仅监听 127.0.0.1，提供 API 与静态资源。

数据从哪来由 `setup(source)` 决定，路由本身不认识「本机」：
    source = source_local.SOURCE       # 本机文件（默认）
    source = source_demo.SOURCE        # 内置示例数据，没装任何客户端也能看
    source = 你写的云端适配器          # 见 docs/CLOUD.md

单机版行为不变：只绑定回环地址、只读本机数据、不发任何上报。
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")

_SOURCE = None
_BAL_CACHE = {"at": 0.0, "data": None}
_BAL_LOCK = threading.Lock()

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".woff2": "font/woff2",
}


def setup(source):
    """注入数据源，返回它。必须在启动服务前调用（或由 main 自动调用）。"""
    global _SOURCE
    _SOURCE = source
    return source


def current_source():
    if _SOURCE is None:
        from source_local import SOURCE  # 延迟导入：示例模式不需要碰本机文件
        return setup(SOURCE)
    return _SOURCE


def _balances(force: bool = False) -> dict:
    src = current_source()
    cfg = src.get_settings() or {}
    ttl = max(60, int(cfg.get("balance_refresh_sec", 600) or 600))
    with _BAL_LOCK:
        fresh = _BAL_CACHE["data"] is not None and (time.time() - _BAL_CACHE["at"]) < ttl
        if fresh and not force:
            return _BAL_CACHE["data"]
        try:
            data = src.balances(force=force)
        except Exception as exc:
            data = {"checked_at": int(time.time() * 1000), "items": [],
                    "error": f"查询余额失败：{exc}"}
        _BAL_CACHE.update({"at": time.time(), "data": data})
        return data


class Handler(BaseHTTPRequestHandler):
    server_version = "TokenLens/1.0"
    protocol_version = "HTTP/1.1"

    # ---------------- 基础 ----------------
    def log_message(self, fmt, *args):
        if os.environ.get("TOKENLENS_VERBOSE"):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body: bytes, ctype: str, extra_headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _err(self, code, message):
        self._json({"error": {"code": code, "message": message}}, code)

    def _download(self, text, filename: str, ctype="text/plain; charset=utf-8"):
        if isinstance(text, str):
            text = text.encode("utf-8")
        disp = f"attachment; filename*=UTF-8''{quote(filename)}"
        self._send(200, text, ctype, {"Content-Disposition": disp})

    def _filters(self):
        q = parse_qs(urlparse(self.path).query)
        get = lambda k: (q.get(k) or [None])[0]  # noqa: E731
        return {
            "from": get("from"), "to": get("to"),
            "app": get("app"), "model": get("model"), "status": get("status"),
            "q": get("q"), "limit": get("limit"), "offset": get("offset"),
            "order": get("order"), "refresh": get("refresh"), "source": get("source"),
        }

    # ---------------- 路由 ----------------
    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path.startswith("/api/"):
                return self._api(path)
            return self._static(path)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            sys.stderr.write(traceback.format_exc(limit=6))
            try:
                self._err(500, f"{exc}")
            except Exception:
                pass

    def do_PUT(self):
        path = urlparse(self.path).path
        if path == "/api/settings":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                return self._json({"settings": current_source().save_settings(body)})
            except Exception as exc:
                return self._err(400, str(exc))
        self._err(404, "not found")

    def _api(self, path: str):
        src = current_source()
        f = self._filters()
        if path == "/api/meta":
            return self._json(src.meta())
        if path == "/api/overview":
            return self._json(src.overview(f))
        if path == "/api/requests":
            return self._json(src.list_requests(f))
        if path == "/api/requests/export":
            return self._download(src.requests_csv(f), "tokenlens-requests.csv",
                                  "text/csv; charset=utf-8")
        if path == "/api/chats":
            return self._json(src.chats_list(f))
        if path == "/api/chats/export-all":
            fn = "tokenlens-chats-%s.zip" % datetime.now().strftime("%Y%m%d-%H%M")
            return self._download(src.export_all_chats(), fn, "application/zip")
        if path.startswith("/api/chats/"):
            rest = unquote(path[len("/api/chats/"):])
            if rest.endswith("/export"):
                sid = rest[: -len("/export")]
                fmt = (f.get("format") or "md").lower()
                if fmt == "json":
                    text = src.chat_export_json(sid)
                    if text is None:
                        return self._err(404, "会话不存在")
                    return self._download(text, f"{sid}.json", "application/json; charset=utf-8")
                text = src.chat_markdown(sid)
                if text is None:
                    return self._err(404, "会话不存在")
                return self._download(text, f"{sid}.md")
            d = src.chat_detail(rest)
            return self._json(d) if d else self._err(404, "会话不存在")
        if path == "/api/balances":
            return self._json(_balances(force=bool(f.get("refresh"))))
        if path == "/api/providers":
            return self._json({"items": src.providers()})
        if path == "/api/settings":
            return self._json({"settings": src.get_settings()})
        return self._err(404, "unknown endpoint")

    def _static(self, path: str):
        if path in ("/", ""):
            path = "/index.html"
        rel = path.lstrip("/")
        full = os.path.normpath(os.path.join(WEB, rel))
        if not full.startswith(os.path.normpath(WEB) + os.sep) and full != os.path.normpath(WEB):
            return self._err(403, "forbidden")
        if not os.path.isfile(full):
            return self._err(404, "not found")
        ext = os.path.splitext(full)[1].lower()
        with open(full, "rb") as fh:
            body = fh.read()
        self._send(200, body, MIME.get(ext, "application/octet-stream"))


def _pick_port(preferred: int, host: str = "127.0.0.1") -> int:
    for p in range(preferred, preferred + 12):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, p))
                return p
            except OSError:
                continue
    raise SystemExit(f"找不到可用端口（{preferred}-{preferred + 11}）")


def build_server(host: str, port: int) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    return srv


def _arg(args, name, default=None):
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return default


def _force_utf8_stdio():
    """控制台是 GBK/cp1252 时，打印中文会直接抛 UnicodeEncodeError 把服务搞挂。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main():
    """python backend/server.py [--demo] [--host 127.0.0.1] [--port 8787]"""
    _force_utf8_stdio()
    args = sys.argv[1:]
    mode = (_arg(args, "--source") or os.environ.get("TOKENLENS_SOURCE", "") or "local").strip().lower()
    if "--demo" in args:
        mode = "demo"

    if mode == "demo":
        from source_demo import SOURCE as src  # noqa: E402
    else:
        from source_local import SOURCE as src  # noqa: E402
    setup(src)

    host = (_arg(args, "--host") or os.environ.get("TOKENLENS_HOST", "127.0.0.1")).strip() or "127.0.0.1"
    port_arg = _arg(args, "--port") or os.environ.get("TOKENLENS_PORT", "")
    cfg = src.get_settings() or {}
    want = int(port_arg) if str(port_arg).isdigit() else int(cfg.get("port", 8787))
    port = _pick_port(want, host)
    if port != want:
        try:
            src.save_settings({**cfg, "port": port})
        except Exception:
            pass

    srv = build_server(host, port)
    try:
        src.warm_up()          # 先绑定端口再预热，窗口能立刻打开
    except Exception:
        pass

    shown = "127.0.0.1" if host in ("0.0.0.0", "") else host
    url = f"http://{shown}:{port}/"
    title = "TokenLens · 本地 AI 用量仪表盘" if mode != "demo" else "TokenLens · 示例数据"
    print("=" * 58)
    print(f"  {title}")
    print(f"  数据源：{getattr(src, 'name', mode)}")
    print(f"  {url}")
    print("  Ctrl+C 退出")
    print("=" * 58, flush=True)

    if not os.environ.get("TOKENLENS_NO_BROWSER") and host in ("127.0.0.1", "localhost"):
        def open_browser():
            time.sleep(0.6)
            try:
                import webbrowser
                webbrowser.open(url)
            except Exception:
                pass
        threading.Thread(target=open_browser, daemon=True).start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n再见 👋")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
