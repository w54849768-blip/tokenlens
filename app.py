"""TokenLens 桌面应用入口。

用 WebView2（经由 pywebview）开一个独立窗口，不再跳浏览器标签页。
- 后台线程里起本地 HTTP 服务（127.0.0.1）
- 若已有实例在跑，直接复用，不重复启动
- 关窗口即退出并停掉服务
- 窗口/任务栏图标使用 web/icon.ico

用法：
    pythonw app.py          正常启动（无控制台）
    python  app.py --debug  带调试输出
    python  app.py --browser 回退到浏览器模式（WebView 不可用时）
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.join(ROOT, "backend")
DATA = os.path.join(ROOT, "data")
ICON = os.path.join(ROOT, "web", "icon.ico")
APP_ID = "TokenLens.Local.1"
APP_TITLE = "TokenLens"
LOG = os.path.join(DATA, "app.log")

_port_holder = {"port": None, "proc": None}


def log(msg: str):
    try:
        os.makedirs(DATA, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def preferred_port() -> int:
    try:
        with open(os.path.join(DATA, "settings.json"), encoding="utf-8") as fh:
            return int(json.load(fh).get("port") or 8787)
    except Exception:
        return 8787


def probe(port: int) -> bool:
    """端口上是否已经跑着我们的服务。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/meta", timeout=1.2) as r:
            data = json.loads(r.read().decode("utf-8"))
            return (data.get("app") or {}).get("name") == "TokenLens"
    except Exception:
        return False


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def find_existing() -> int | None:
    """探测 8787-8798（以及设置里的端口），找到已运行的实例。"""
    base = preferred_port()
    for p in [base] + [x for x in range(8787, 8799) if x != base]:
        if probe(p):
            return p
    return None


def start_server() -> int:
    """后台起服务子进程，返回端口。"""
    base = preferred_port()
    port = next((p for p in range(base, base + 12) if port_free(p)), base)
    env = dict(os.environ)
    env["TOKENLENS_NO_BROWSER"] = "1"
    env["TOKENLENS_PORT"] = str(port)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    logfile = open(os.path.join(DATA, "server.log"), "ab", buffering=0)
    proc = subprocess.Popen(
        [sys.executable, os.path.join(BACKEND, "server.py")],
        cwd=ROOT, env=env, creationflags=creationflags,
        stdout=logfile, stderr=logfile,
    )
    _port_holder["proc"] = proc
    deadline = time.time() + 25
    while time.time() < deadline:
        if probe(port):
            log(f"server ready on {port} (pid={proc.pid})")
            return port
        if proc.poll() is not None:
            raise RuntimeError("本地服务启动失败，详见 data/server.log")
        time.sleep(0.25)
    raise RuntimeError("本地服务启动超时")


def stop_server():
    proc = _port_holder.get("proc")
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def set_app_user_model_id():
    """让任务栏把本窗口当成独立应用（用自己的图标，而不是 Python 的）。"""
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass


def apply_window_icon(stop: threading.Event):
    """窗口出现后把 HWND 的图标换成 icon.ico。"""
    try:
        import win32con
        import win32gui
    except Exception:
        return
    deadline = time.time() + 20
    hwnd = 0
    while time.time() < deadline and not stop.is_set():
        hwnd = win32gui.FindWindow(None, APP_TITLE)
        if hwnd:
            break
        time.sleep(0.2)
    if not hwnd:
        log("icon: window not found")
        return
    try:
        big = win32gui.LoadImage(0, ICON, win32con.IMAGE_ICON, 0, 0,
                                 win32con.LR_LOADFROMFILE | win32con.LR_DEFAULTSIZE)
        small = win32gui.LoadImage(0, ICON, win32con.IMAGE_ICON, 16, 16,
                                   win32con.LR_LOADFROMFILE)
        win32gui.SendMessage(hwnd, win32con.WM_SETICON, win32con.ICON_BIG, big)
        win32gui.SendMessage(hwnd, win32con.WM_SETICON, win32con.ICON_SMALL, small)
        log("icon applied")
    except Exception as exc:
        log(f"icon failed: {exc}")


def probe_viewport(window):
    """记录 WebView 的真实视口/DPI，便于排查高分屏下的布局问题。"""
    js = ("JSON.stringify({w:innerWidth,h:innerHeight,dpr:window.devicePixelRatio,"
          "sw:document.documentElement.scrollWidth,cw:document.documentElement.clientWidth,"
          "zoom:window.visualViewport&&window.visualViewport.scale})")
    for _ in range(20):
        time.sleep(0.5)
        try:
            info = window.evaluate_js(js)
            if info:
                log("viewport: " + str(info))
                return
        except Exception:
            continue
    log("viewport: probe failed")


def run_browser(url: str):
    import webbrowser
    webbrowser.open(url)
    log("opened in browser (fallback)")
    # 浏览器模式下没有窗口可等，服务保持运行直到进程被杀
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


def main() -> int:
    args = set(sys.argv[1:])
    debug = "--debug" in args
    force_browser = "--browser" in args
    os.makedirs(DATA, exist_ok=True)
    set_app_user_model_id()

    try:
        existing = find_existing()
        if existing:
            port = existing
            log(f"reuse running instance on {port}")
        else:
            port = start_server()
        url = f"http://127.0.0.1:{port}/"

        if force_browser:
            run_browser(url)
            return 0

        try:
            import webview
        except Exception as exc:
            log(f"pywebview unavailable ({exc}); fallback to browser")
            run_browser(url)
            return 0

        stop = threading.Event()
        window = webview.create_window(
            APP_TITLE,
            url,
            width=1500,
            height=960,
            min_size=(1080, 680),
            background_color="#070A0E",
            maximized=True,          # 高分屏缩放下按 CSS 像素算，直接最大化最稳
            text_select=False,
        )
        threading.Thread(target=apply_window_icon, args=(stop,), daemon=True).start()
        threading.Thread(target=probe_viewport, args=(window,), daemon=True).start()
        log("starting webview window")
        webview.start(debug=debug)
        stop.set()
        log("window closed")
        return 0
    except Exception as exc:
        log("FATAL: " + traceback.format_exc())
        # 出问题时不要让用户面对一个静默消失的图标
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0, f"TokenLens 启动失败：\n{exc}\n\n详情见 data\\app.log", APP_TITLE, 0x10)
        except Exception:
            pass
        return 1
    finally:
        stop_server()


if __name__ == "__main__":
    sys.exit(main())
