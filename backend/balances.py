# -*- coding: utf-8 -*-
"""TokenLens 供应商余额查询模块（唯一职责：抽凭据 + 按平台查余额）。

数据来源：CC Switch 的 SQLite 库 ``~/.cc-switch/cc-switch.db`` 里的 ``providers`` 表。
``settings_config`` 是 JSON 字符串，实测有 5 种形态（见 ``extract_credentials``）。

对外契约（docs/API.md 的 /api/balances items）::

    query_all(provider_rows) -> list[dict]
    每项：{"id","name","app_type","base_url","adapter","ok",
           "balance","currency","detail","error","latency_ms"}

设计要点：
  * 纯标准库（urllib / sqlite3 / json），不引入任何第三方依赖；
  * 单平台总预算 ≤ 8 秒（``_PROVIDER_BUDGET``），失败一律降级为 ``ok: false``；
  * 同 ``base_url + api_key`` 去重，避免对同一家中转站重复发起请求；
  * **安全**：``api_key`` 只在内存里拼 ``Authorization`` 头，
    绝不写日志、不打印、不进入返回结构（连片段都不进）。

安全边界：本模块不写任何文件；读库一律 ``mode=ro``（CC Switch 可能正在运行，
WAL 模式下只读是安全的）。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import ssl
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

__all__ = ["query_all", "extract_credentials", "read_provider_rows", "DEFAULT_DB_PATH"]

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: cc-switch 数据库默认位置（可用环境变量 TOKENLENS_CCSWITCH_DB 覆盖）
DEFAULT_DB_PATH = os.environ.get(
    "TOKENLENS_CCSWITCH_DB",
    str(Path.home() / ".cc-switch" / "cc-switch.db"),
)

#: 单家供应商的总预算（秒）—— 契约要求"单平台超时 ≤8 秒"
_PROVIDER_BUDGET = 8.0
#: 单次 HTTP 请求的上限（秒）；还要被剩余预算进一步压缩
_REQUEST_TIMEOUT = 6.0
#: 中转站探测时单次请求的上限（秒），保证多家串行时总耗时不失控
_RELAY_PROBE_TIMEOUT = 3.5
#: 响应体最多读取字节数（防止把整页 HTML 读进内存）
_MAX_BODY_BYTES = 256 * 1024
_USER_AGENT = "TokenLens/0.1 (+local balance probe)"

_CURRENCY_SYMBOL = {"CNY": "¥", "RMB": "¥", "USD": "$", "EUR": "€"}


# ---------------------------------------------------------------------------
# 小工具：类型转换 / 金额格式化
# ---------------------------------------------------------------------------

def _to_float(value) -> float | None:
    """尽量把任意值转成 float；转不了返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if text in ("", "-", "null", "None", "nil"):
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _fmt_money(value: float | None, currency: str | None) -> str:
    """把金额格式化成带币种符号的人话（¥12.34 / $1.20）。"""
    if value is None:
        return "-"
    cur = (currency or "").upper()
    symbol = _CURRENCY_SYMBOL.get(cur)
    if symbol:
        return "%s%.2f" % (symbol, value)
    return "%s %.2f" % (cur, value) if cur else "%.2f" % value


# ---------------------------------------------------------------------------
# 凭据抽取：settings_config（5 种形态）→ {"base_url","api_key","kind"}
# ---------------------------------------------------------------------------

#: 已知的 base_url 字段名（不区分大小写比较）
_BASE_KEYS = frozenset({
    "base_url", "baseurl", "base_uri", "api_base", "api_url",
    "anthropic_base_url", "openai_base_url", "openai_api_base",
    "google_gemini_base_url", "gemini_base_url",
})

#: 已知的 api_key 字段名（不区分大小写比较）
_KEY_KEYS = frozenset({
    "api_key", "apikey", "api_key_value", "auth_token", "access_token",
    "anthropic_auth_token", "anthropic_api_key", "openai_api_key",
    "gemini_api_key", "google_api_key",
})

#: 明显不是真实密钥的占位值
_PLACEHOLDERS = frozenset({
    "", "-", "null", "none", "your_api_key", "your-api-key", "sk-xxx",
    "sk-your-key", "xxx", "changeme", "placeholder",
})

_TOML_BASE_RE = re.compile(r"""(?im)^\s*base_url\s*=\s*["']([^"']+)["']""")
_TOML_KEY_RE = re.compile(r"""(?im)^\s*api_key\s*=\s*["']([^"']+)["']""")


def _clean_str(value) -> str:
    """去空白 / 去引号；非字符串返回空串。"""
    if not isinstance(value, str):
        return ""
    return value.strip().strip('"').strip("'").strip()


def _looks_like_key(value: str) -> bool:
    """粗略判断一个字符串是否像 API key。"""
    text = _clean_str(value)
    if not text or text.lower() in _PLACEHOLDERS:
        return False
    if len(text) < 8 or len(text) > 4096:
        return False
    if any(ch.isspace() for ch in text):
        return False
    return True


def _deep_find(obj, key_names: frozenset, max_depth: int = 6):
    """在嵌套 dict/list 里按字段名（不区分大小写）找第一个字符串值。"""
    if max_depth <= 0 or obj is None:
        return None
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str) and key.lower().replace("-", "_") in key_names:
                text = _clean_str(value)
                if text:
                    return value
        for value in obj.values():
            found = _deep_find(value, key_names, max_depth - 1)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _deep_find(item, key_names, max_depth - 1)
            if found is not None:
                return found
    return None


def _normalize_base_url(raw: str) -> str:
    """规范化 base_url：补 scheme、去首尾空白/引号/尾斜杠。"""
    text = _clean_str(raw)
    if not text:
        return ""
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", text):
        # 多数供应商配置省略了 https://
        text = "https://" + text.lstrip("/")
    parts = urlsplit(text)
    if not parts.netloc:
        return ""
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def _structured_base_url(cfg: dict) -> str:
    """按 5 种已知形态的固定路径找 base_url（优先，避免误抓）。"""
    env = cfg.get("env")
    if isinstance(env, dict):
        for key in (
            "ANTHROPIC_BASE_URL", "OPENAI_BASE_URL", "OPENAI_API_BASE",
            "GOOGLE_GEMINI_BASE_URL", "GEMINI_BASE_URL", "ANTHROPIC_API_URL",
        ):
            value = _clean_str(env.get(key))
            if value:
                return value

    options = cfg.get("options")
    if isinstance(options, dict):
        for key in ("baseURL", "baseUrl", "base_url", "api_base"):
            value = _clean_str(options.get(key))
            if value:
                return value

    for key in ("base_url", "baseUrl", "baseURL", "api_base"):
        value = _clean_str(cfg.get(key))
        if value:
            return value

    auth = cfg.get("auth")
    if isinstance(auth, dict):
        for key in ("base_url", "baseUrl", "baseURL"):
            value = _clean_str(auth.get(key))
            if value:
                return value

    # codex 形态：base_url 藏在 TOML 文本里
    config_text = cfg.get("config")
    if isinstance(config_text, str) and config_text:
        match = _TOML_BASE_RE.search(config_text)
        if match:
            return match.group(1)

    return ""


def _structured_api_key(cfg: dict) -> str:
    """按 5 种已知形态的固定路径找 api_key（优先）。"""
    env = cfg.get("env")
    if isinstance(env, dict):
        for key in (
            "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
            "GEMINI_API_KEY", "GOOGLE_API_KEY", "DEEPSEEK_API_KEY",
            "MOONSHOT_API_KEY", "SILICONFLOW_API_KEY", "API_KEY", "AUTH_TOKEN",
        ):
            value = _clean_str(env.get(key))
            if _looks_like_key(value):
                return value

    options = cfg.get("options")
    if isinstance(options, dict):
        for key in ("apiKey", "api_key", "apiKeyValue", "token"):
            value = _clean_str(options.get(key))
            if _looks_like_key(value):
                return value

    for key in ("api_key", "apiKey", "apikey", "token", "auth_token"):
        value = _clean_str(cfg.get(key))
        if _looks_like_key(value):
            return value

    auth = cfg.get("auth")
    if isinstance(auth, dict):
        for key in ("OPENAI_API_KEY", "api_key", "apiKey", "token", "access_token"):
            value = _clean_str(auth.get(key))
            if _looks_like_key(value):
                return value

    # codex 形态：key 也可能写在 TOML 里
    config_text = cfg.get("config")
    if isinstance(config_text, str) and config_text:
        match = _TOML_KEY_RE.search(config_text)
        if match and _looks_like_key(match.group(1)):
            return match.group(1)

    return ""


def _detect_kind(cfg: dict, base_url: str, api_key: str) -> str:
    """标注凭据来自哪种配置形态，便于排障（不参与业务判断）。"""
    if isinstance(cfg.get("config"), str) and ("model_providers" in cfg["config"] or "base_url" in cfg["config"]):
        return "codex_toml"
    if "npm" in cfg or isinstance(cfg.get("options"), dict):
        return "npm_options"
    if isinstance(cfg.get("env"), dict):
        return "env"
    if base_url or api_key:
        return "flat"
    return ""


def extract_credentials(settings_config: str, app_type: str, endpoints: list[str]) -> dict:
    """从 ``providers.settings_config`` 抽出统一的凭据结构。

    支持的 5 种实测形态：

    1. ``{"env": {"ANTHROPIC_BASE_URL": ..., "ANTHROPIC_AUTH_TOKEN": ...}}``（claude / claude-desktop）
    2. 同 1，但地址是 One API 系中转站（``https://www.oneapi.work``）
    3. ``{"npm": "@ai-sdk/openai-compatible",
         "options": {"baseURL": ..., "apiKey": ...}}``（opencode：deepseek/siliconflow/kimi/modelscope）
    4. ``{"base_url": ..., "api_key": ...}`` / ``{"baseUrl": ..., "apiKey": ...}``（hermes / openclaw）
    5. ``{"auth": {"OPENAI_API_KEY": ...}, "config": "<TOML>"}``（codex，base_url 在 TOML 里）

    返回 ``{"base_url": str, "api_key": str, "kind": str}``，抽不到的字段是空串。
    ``api_key`` 仅供应内部请求使用，调用方不得把它写进日志或返回给前端。
    """
    result = {"base_url": "", "api_key": "", "kind": ""}
    if not isinstance(settings_config, str) or not settings_config.strip():
        # 没有 settings_config 时仍可用 provider_endpoints 兜底
        base = _first_endpoint(endpoints)
        result["base_url"] = _normalize_base_url(base)
        result["kind"] = "endpoints_only" if result["base_url"] else ""
        return result

    cfg = None
    raw = settings_config.strip()
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            cfg = parsed
    except (ValueError, TypeError):
        cfg = None

    if cfg is None:
        # 极端情况：settings_config 本身是一段 TOML
        base = ""
        key = ""
        match = _TOML_BASE_RE.search(raw)
        if match:
            base = match.group(1)
        match = _TOML_KEY_RE.search(raw)
        if match:
            key = match.group(1)
        result["base_url"] = _normalize_base_url(base or _first_endpoint(endpoints))
        result["api_key"] = key if _looks_like_key(key) else ""
        result["kind"] = "toml" if (result["base_url"] or result["api_key"]) else ""
        return result

    base_url = _structured_base_url(cfg)
    api_key = _structured_api_key(cfg)

    # 兜底：任意层级里出现 base_url / api_key 字样
    if not base_url:
        base_url = _clean_str(_deep_find(cfg, _BASE_KEYS))
    if not api_key:
        candidate = _clean_str(_deep_find(cfg, _KEY_KEYS))
        if _looks_like_key(candidate):
            api_key = candidate

    # 最后兜底：providers.provider_endpoints.url
    if not base_url:
        base_url = _first_endpoint(endpoints)

    result["base_url"] = _normalize_base_url(base_url)
    result["api_key"] = api_key if _looks_like_key(api_key) else ""
    result["kind"] = _detect_kind(cfg, result["base_url"], result["api_key"])
    return result


def _first_endpoint(endpoints) -> str:
    """取 provider_endpoints 里第一条像 URL 的记录。"""
    if not isinstance(endpoints, (list, tuple)):
        return ""
    for item in endpoints:
        if isinstance(item, dict):
            item = item.get("url") or ""
        text = _clean_str(item)
        if text.startswith(("http://", "https://")):
            return text
    return ""


def _endpoint_list(row: dict) -> list[str]:
    """从 provider 行里取 endpoints（read_provider_rows 会附加该字段）。"""
    for key in ("endpoints", "endpoint_urls", "provider_endpoints"):
        value = row.get(key)
        if isinstance(value, (list, tuple)):
            return [x for x in value if isinstance(x, (str, dict))]
    return []


# ---------------------------------------------------------------------------
# base_url → 适配器选择
# ---------------------------------------------------------------------------

def _pick_adapter(base_url: str) -> str:
    """按 host 选适配器；未识别的中转站走通用 ``oneapi`` / ``relay`` 探测。"""
    try:
        host = (urlsplit(base_url).hostname or "").lower()
    except Exception:
        host = ""
    if not host:
        return "unsupported"
    if host == "deepseek.com" or host.endswith(".deepseek.com"):
        return "deepseek"
    if "moonshot" in host:
        return "moonshot"
    if "siliconflow" in host:
        return "siliconflow"
    if "openrouter" in host:
        return "openrouter"
    if "modelscope" in host:
        # ModelScope 推理 API 只提供模型调用，没有账户余额端点（实测全部 404）
        return "unsupported"
    if any(tag in host for tag in ("oneapi", "one-api", "newapi", "new-api")):
        return "oneapi"
    return "relay"


def _origin(url: str) -> str:
    """取 scheme://host:port（丢掉 path）。"""
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return ""
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _api_root(base_url: str) -> str:
    """返回拼接 API 路径用的根：保留 base 的路径，空路径补 ``/v1``。"""
    parts = urlsplit(base_url)
    path = parts.path.rstrip("/")
    if not path:
        path = "/v1"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


# ---------------------------------------------------------------------------
# HTTP 基础设施
# ---------------------------------------------------------------------------

def _remaining(deadline: float, cap: float) -> float:
    """本次请求可用的超时：min(cap, 剩余预算)，至少 1 秒避免立刻炸。"""
    return max(1.0, min(cap, deadline - time.monotonic()))


def _request(url: str, api_key: str | None, timeout: float, accept: str = "application/json") -> dict:
    """发一个 GET，返回 ``{status, body, json, error, ms}``，永不抛异常。

    ``api_key`` 只在这里拼进 Authorization 头，不会出现在返回值里。
    """
    result = {"status": None, "body": "", "json": None, "error": None, "ms": 0}
    headers = {"User-Agent": _USER_AGENT, "Accept": accept}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    req = urllib.request.Request(url, headers=headers, method="GET")
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as resp:
            raw = resp.read(_MAX_BODY_BYTES)
            result["status"] = int(resp.status)
    except urllib.error.HTTPError as exc:
        result["status"] = int(exc.code)
        try:
            raw = exc.read(_MAX_BODY_BYTES)
        except Exception:  # noqa: BLE001 - 读错误响应体失败不影响主流程
            raw = b""
    except (urllib.error.URLError, TimeoutError, OSError, ssl.SSLError) as exc:
        result["error"] = _short_network_error(exc)
        result["ms"] = int((time.monotonic() - started) * 1000)
        return result
    except Exception as exc:  # noqa: BLE001 - 任何意外都降级
        result["error"] = "请求异常：%s" % type(exc).__name__
        result["ms"] = int((time.monotonic() - started) * 1000)
        return result

    result["ms"] = int((time.monotonic() - started) * 1000)
    text = raw.decode("utf-8", "replace")
    result["body"] = text
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            result["json"] = json.loads(text)
        except ValueError:
            result["json"] = None
    return result


def _short_network_error(exc: Exception) -> str:
    """把网络异常压成一句人话（绝不带 headers / 密钥）。"""
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", None)
        text = str(reason) if reason is not None else str(exc)
    else:
        text = str(exc)
    text = text.replace("\n", " ").strip()[:80]
    lowered = text.lower()
    if "timed out" in lowered or "timeout" in lowered:
        return "请求超时"
    if "ssl" in lowered or "certificate" in lowered:
        return "TLS 握手失败"
    if "getaddrinfo" in lowered or "name or service not known" in lowered:
        return "域名解析失败"
    if "refused" in lowered or "拒绝" in text:
        return "连接被拒绝"
    if "unreachable" in lowered or "unreachable" in text:
        return "网络不可达"
    return "网络异常（%s）" % text[:48] if text else "网络不可达"


def _http_error(res: dict) -> str:
    """把一次失败的 HTTP 响应翻译成简短错误原因（不含响应体原文）。"""
    if res.get("error"):
        return res["error"]
    status = res.get("status")
    if status == 401:
        return "密钥无效或已过期（401）"
    if status == 402:
        return "账户余额不足或计费受限（402）"
    if status == 403:
        return "密钥无权限（403）"
    if status == 404:
        return "余额接口不存在（404）"
    if status == 410:
        return "余额接口已下线（410）"
    if status == 429:
        return "请求过于频繁（429）"
    if status is None:
        return "无响应"
    if 500 <= int(status) < 600:
        return "平台服务异常（%s）" % status
    return "HTTP %s" % status


def _payload_code(payload) -> str:
    """取返回体里的业务错误码（只取数字/短码，不取 message 原文）。"""
    if isinstance(payload, dict):
        for key in ("code", "scode", "error_code"):
            value = payload.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return str(int(value))
            if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-Fx]{1,8}", value.strip()):
                return value.strip()
    return ""


# ---------------------------------------------------------------------------
# 结果构造
# ---------------------------------------------------------------------------

def _result(provider_id: str, name: str, app_type: str, base_url: str, adapter: str) -> dict:
    """构造一条符合 /api/balances items 契约的空结果。"""
    return {
        "id": provider_id,
        "name": name,
        "app_type": app_type,
        "base_url": base_url or "",
        "adapter": adapter,
        "ok": False,
        "balance": None,
        "currency": None,
        "detail": "",
        "error": None,
        "latency_ms": None,
    }


def _ok(item: dict, balance: float | None, currency: str | None, detail: str, adapter: str | None = None) -> dict:
    item["ok"] = balance is not None
    item["balance"] = balance
    item["currency"] = (currency or "").upper() or None
    item["detail"] = detail
    item["error"] = None if balance is not None else "返回体缺少可用余额字段"
    if adapter:
        item["adapter"] = adapter
    return item


def _fail(item: dict, error: str, adapter: str | None = None) -> dict:
    item["ok"] = False
    item["balance"] = None
    item["currency"] = None
    item["detail"] = ""
    item["error"] = error
    if adapter:
        item["adapter"] = adapter
    return item


# ---------------------------------------------------------------------------
# 各平台适配器
# ---------------------------------------------------------------------------

def _adapt_deepseek(cred: dict, deadline: float, item: dict) -> dict:
    """DeepSeek 官方：GET {origin}/user/balance（也可 /v1/user/balance）。"""
    base = cred["base_url"]
    key = cred["api_key"]
    origin = _origin(base)
    path = urlsplit(base).path.rstrip("/")
    candidates: list[str] = []
    if path in ("", "/v1", "/beta"):
        candidates.append(origin + "/user/balance")
    else:
        # 例如 base 是 https://api.deepseek.com/anthropic，先按原样拼，再退回根域
        candidates.append(base + "/user/balance")
        candidates.append(origin + "/user/balance")

    last = None
    for url in candidates:
        res = _request(url, key, _remaining(deadline, _REQUEST_TIMEOUT))
        last = res
        if res["status"] == 200 and isinstance(res["json"], dict):
            payload = res["json"]
            infos = payload.get("balance_infos")
            info = infos[0] if isinstance(infos, list) and infos and isinstance(infos[0], dict) else {}
            currency = _clean_str(info.get("currency")).upper() or "CNY"
            total = _to_float(info.get("total_balance"))
            granted = _to_float(info.get("granted_balance"))
            topped = _to_float(info.get("topped_up_balance"))
            if total is None:
                return _fail(item, "返回体缺少 total_balance")
            detail = "总余额 %s（赠金 %s，充值 %s）" % (
                _fmt_money(total, currency),
                _fmt_money(granted, currency),
                _fmt_money(topped, currency),
            )
            if payload.get("is_available") is False:
                detail += "，账户当前不可用"
            return _ok(item, total, currency, detail)
    return _fail(item, _http_error(last or {}))


def _adapt_moonshot(cred: dict, deadline: float, item: dict) -> dict:
    """Moonshot / Kimi：GET {origin}/v1/users/me/balance（月之暗面官方接口）。"""
    base = cred["base_url"]
    key = cred["api_key"]
    origin = _origin(base)
    path = urlsplit(base).path.rstrip("/")
    url = base + "/users/me/balance" if path.endswith("/v1") else origin + "/v1/users/me/balance"
    res = _request(url, key, _remaining(deadline, _REQUEST_TIMEOUT))
    if res["status"] == 200 and isinstance(res["json"], dict):
        data = res["json"].get("data")
        if not isinstance(data, dict):
            return _fail(item, "返回体缺少 data 字段")
        available = _to_float(data.get("available_balance"))
        voucher = _to_float(data.get("voucher_balance"))
        cash = _to_float(data.get("cash_balance"))
        if available is None:
            return _fail(item, "返回体缺少 available_balance")
        # 大陆站 api.moonshot.cn 的单位是 CNY，国际站 api.moonshot.ai 是 USD
        currency = "CNY" if (urlsplit(url).hostname or "").endswith(".cn") else "USD"
        detail = "可用余额 %s（赠金 %s，现金 %s）" % (
            _fmt_money(available, currency),
            _fmt_money(voucher, currency),
            _fmt_money(cash, currency),
        )
        return _ok(item, available, currency, detail)
    return _fail(item, _http_error(res))


def _adapt_siliconflow(cred: dict, deadline: float, item: dict) -> dict:
    """SiliconFlow：GET {origin}/v1/user/info。

    注意：该接口已于 2026 年初下线（带有效 key 访问返回 HTTP 410 / code 20092），
    这里保留实现以便官方恢复或替代接口出现时立即可用。
    """
    base = cred["base_url"]
    key = cred["api_key"]
    origin = _origin(base)
    path = urlsplit(base).path.rstrip("/")
    url = base + "/user/info" if path.endswith("/v1") else origin + "/v1/user/info"
    res = _request(url, key, _remaining(deadline, _REQUEST_TIMEOUT))
    if res["status"] == 200 and isinstance(res["json"], dict):
        payload = res["json"]
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        total = _to_float(data.get("balance"))
        charge = _to_float(data.get("chargeBalance"))
        total_balance = _to_float(data.get("totalBalance"))
        picked = total if total is not None else (total_balance if total_balance is not None else charge)
        if picked is None:
            return _fail(item, "返回体缺少 balance 字段")
        currency = "CNY"
        detail = "账户余额 %s（充值 %s，总额 %s）" % (
            _fmt_money(picked, currency),
            _fmt_money(charge, currency),
            _fmt_money(total_balance, currency),
        )
        return _ok(item, picked, currency, detail)
    if res["status"] == 410:
        code = _payload_code(res["json"])
        return _fail(item, "余额接口已下线（HTTP 410%s）" % (("，code " + code) if code else ""))
    return _fail(item, _http_error(res))


def _adapt_openrouter(cred: dict, deadline: float, item: dict) -> dict:
    """OpenRouter：GET {origin}/api/v1/credits（返回总额度与已用量）。"""
    base = cred["base_url"]
    key = cred["api_key"]
    origin = _origin(base)
    url = origin + "/api/v1/credits"
    res = _request(url, key, _remaining(deadline, _REQUEST_TIMEOUT))
    if res["status"] == 200 and isinstance(res["json"], dict):
        payload = res["json"]
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        total_credits = _to_float(data.get("total_credits"))
        total_usage = _to_float(data.get("total_usage"))
        if total_credits is None:
            return _fail(item, "返回体缺少 total_credits")
        balance = total_credits - (total_usage or 0.0)
        detail = "剩余额度 %s（总额度 %s，已用 %s）" % (
            _fmt_money(balance, "USD"),
            _fmt_money(total_credits, "USD"),
            _fmt_money(total_usage or 0.0, "USD"),
        )
        return _ok(item, balance, "USD", detail)
    return _fail(item, _http_error(res))


def _parse_relay_payload(payload) -> tuple[float | None, str | None, str | None]:
    """从各种中转站返回体里抽余额：返回 (balance, currency, detail)。"""
    if not isinstance(payload, dict):
        return None, None, None

    # 形态 A：{"balance": 12.3, "currency": "CNY"} / {"remaining": ...}
    for key in ("balance", "remaining", "total_balance", "available_balance"):
        value = _to_float(payload.get(key))
        if value is not None:
            currency = _clean_str(payload.get("currency") or payload.get("unit")).upper()
            if currency not in ("CNY", "USD"):
                currency = "USD"
            return value, currency, "余额 %s" % _fmt_money(value, currency)

    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("balance", "totalBalance", "total_balance", "available_balance", "remaining"):
            value = _to_float(data.get(key))
            if value is not None:
                currency = _clean_str(data.get("currency")).upper()
                if currency not in ("CNY", "USD"):
                    currency = "USD"
                return value, currency, "余额 %s" % _fmt_money(value, currency)

    # 形态 B：OpenAI 兼容计费接口的额度（单位 USD）
    hard_limit = _to_float(payload.get("hard_limit_usd"))
    if hard_limit is not None:
        return hard_limit, "USD", "额度 %s" % _fmt_money(hard_limit, "USD")
    return None, None, None


def _adapt_relay(cred: dict, deadline: float, item: dict, adapter: str) -> dict:
    """未知中转站：按 One API / New API / OpenAI 兼容计费的常见路径做有界探测。

    探测请求**并行**发出（各家耗时 ≈ 最慢的一次请求，而不是累加），任何一步失败
    都不抛异常；最终给出可读的失败原因。

    真实结论（见 docs/balances-probe.md）：oneapi.work 与 cheaperinference 这类
    站点只开放推理接口，余额挂在控制台登录态后面，``sk-`` 密钥查不到。
    """
    base = cred["base_url"]
    key = cred["api_key"]
    origin = _origin(base)
    api_root = _api_root(base)

    # 候选路径：One API/New API 计费接口、面板登录接口、通用 /user/balance
    probes = [
        ("subscription", api_root + "/dashboard/billing/subscription"),
        ("user_self", origin + "/api/user/self"),
        ("dashboard", origin + "/api/dashboard"),
        ("v1_balance", origin + "/v1/user/balance"),
        ("balance", origin + "/user/balance"),
    ]
    responses: dict[str, dict] = {}
    try:
        with ThreadPoolExecutor(max_workers=len(probes)) as pool:
            futures = {
                name: pool.submit(_request, url, key, _remaining(deadline, _RELAY_PROBE_TIMEOUT))
                for name, url in probes
            }
            for name, future in futures.items():
                try:
                    responses[name] = future.result(timeout=_remaining(deadline, _RELAY_PROBE_TIMEOUT))
                except Exception:  # noqa: BLE001 - 单个探测失败不影响整体
                    responses[name] = {"status": None, "json": None, "error": "探测失败", "ms": 0}
    except Exception:  # noqa: BLE001 - 线程池异常时退回串行（极罕见）
        responses = {name: _request(url, key, _remaining(deadline, _RELAY_PROBE_TIMEOUT)) for name, url in probes}

    notes: list[str] = []

    def _note(name: str, label: str) -> None:
        res = responses.get(name) or {}
        notes.append("%s→%s" % (label, res.get("status") or res.get("error") or "-"))

    # 1) OpenAI 兼容计费接口：订阅额度（USD）+ 已用额度（单位：美分）
    sub = responses.get("subscription") or {}
    if sub.get("status") == 200 and isinstance(sub.get("json"), dict):
        hard_limit = _to_float(sub["json"].get("hard_limit_usd"))
        if hard_limit is not None:
            usage = _request(
                api_root + "/dashboard/billing/usage?start_date=%s&end_date=%s"
                % (_days_ago(30), _days_ago(0)),
                key,
                _remaining(deadline, _RELAY_PROBE_TIMEOUT),
            )
            total_usage = None
            if usage["status"] == 200 and isinstance(usage["json"], dict):
                total_usage = _to_float(usage["json"].get("total_usage"))
            if total_usage is not None:
                balance = hard_limit - total_usage / 100.0
                detail = "剩余额度 %s（总额度 %s，已用 %s）" % (
                    _fmt_money(balance, "USD"),
                    _fmt_money(hard_limit, "USD"),
                    _fmt_money(total_usage / 100.0, "USD"),
                )
                return _ok(item, balance, "USD", detail, adapter)
            return _ok(item, hard_limit, "USD", "总额度 %s（用量查询失败，无法算剩余）" % _fmt_money(hard_limit, "USD"), adapter)
    else:
        _note("subscription", "/v1/dashboard/billing/subscription")

    # 2) New-API 面板登录态接口（sk- 密钥通常 401）
    for name, label in (
        ("user_self", "/api/user/self"),
        ("dashboard", "/api/dashboard"),
        ("v1_balance", "/v1/user/balance"),
        ("balance", "/user/balance"),
    ):
        res = responses.get(name) or {}
        if res.get("status") == 200 and isinstance(res.get("json"), dict):
            balance, currency, detail = _parse_relay_payload(res["json"])
            if balance is not None:
                return _ok(item, balance, currency, detail, adapter)
        _note(name, label)

    joined = "、".join(notes[:4])
    if any(note.endswith("→401") for note in notes):
        return _fail(item, "中转站面板接口需登录态（401），API 密钥无法查询余额（%s）" % joined, adapter)
    return _fail(item, "未找到可用余额接口（%s）" % joined, adapter)


def _days_ago(days: int) -> str:
    """计费接口需要的日期参数（YYYY-MM-DD，UTC 近似即可）。"""
    stamp = time.time() - days * 86400
    return time.strftime("%Y-%m-%d", time.gmtime(stamp))


# ---------------------------------------------------------------------------
# 对外主入口
# ---------------------------------------------------------------------------

def query_all(provider_rows: list[dict]) -> list[dict]:
    """查询所有供应商余额，返回 /api/balances items 列表。

    ``provider_rows`` 是 ``providers`` 表的原始 dict 列表
    （含 ``id`` / ``app_type`` / ``name`` / ``settings_config`` / ``website_url``，
    可选 ``category`` 与 ``endpoints``）。任何异常都在内部消化，绝不向上抛。

    单品结构::

        {"id","name","app_type","base_url","adapter","ok",
         "balance","currency","detail","error","latency_ms"}

    ``latency_ms`` 是真实网络耗时；命中同 ``base_url + api_key`` 去重缓存时复用首次耗时。
    """
    results: list[dict] = []
    try:
        if isinstance(provider_rows, dict):
            rows = [provider_rows]  # 容错：调用方误传单行
        elif isinstance(provider_rows, (list, tuple)):
            rows = list(provider_rows)
        else:
            return results
    except Exception:  # noqa: BLE001 - 入参异常也不能崩
        return results

    # 去重缓存：key = (base_url, api_key)，value = 适配器结果（不含 id/name）
    cache: dict[tuple[str, str], dict] = {}
    for row in rows:
        try:
            results.append(_query_one(row, cache))
        except Exception as exc:  # noqa: BLE001 - 单家失败不影响其他家
            item = _result(_safe_str(row, "id"), _safe_str(row, "name"), _safe_str(row, "app_type"), "", "unsupported")
            item["error"] = "内部错误：%s" % type(exc).__name__
            results.append(item)
    return results


def _safe_str(row, key: str) -> str:
    if not isinstance(row, dict):
        return ""
    try:
        value = row.get(key)
    except Exception:  # noqa: BLE001
        return ""
    return str(value) if value is not None else ""


def _query_one(row: dict, cache: dict) -> dict:
    """查询单家供应商（内部函数，异常由 query_all 兜底）。"""
    if not isinstance(row, dict):
        row = {}
    provider_id = _safe_str(row, "id")
    name = _safe_str(row, "name") or provider_id
    app_type = _safe_str(row, "app_type")
    settings_config = row.get("settings_config") or ""
    endpoints = _endpoint_list(row)

    cred = extract_credentials(settings_config, app_type, endpoints)
    base_url = cred["base_url"]
    api_key = cred["api_key"]
    adapter = _pick_adapter(base_url)
    item = _result(provider_id, name, app_type, base_url, adapter)

    # 契约要求：官方 provider（category=official 且没有 key）不发请求
    if not base_url:
        return _fail(item, "未配置密钥" if not api_key else "未配置 base_url")
    if not api_key:
        return _fail(item, "未配置密钥")
    if adapter == "unsupported":
        host = (urlsplit(base_url).hostname or "").lower()
        if "modelscope" in host:
            return _fail(item, "该平台未提供余额接口（ModelScope 推理 API 无计费端点）")
        return _fail(item, "该平台暂不支持余额查询")

    # 中转站的所有探测都以 origin 为准，故按 origin 去重（/v1 与裸地址算同一家）
    dedupe_base = _origin(base_url) if adapter in ("oneapi", "relay") else base_url
    cache_key = (dedupe_base, api_key)
    cached = cache.get(cache_key)
    if cached is not None:
        # 同 base_url + 同 key：直接复用结果，避免重复打同一家中转站
        item.update({
            "adapter": cached["adapter"],
            "ok": cached["ok"],
            "balance": cached["balance"],
            "currency": cached["currency"],
            "detail": cached["detail"],
            "error": cached["error"],
            "latency_ms": cached["latency_ms"],
        })
        return item

    started = time.monotonic()
    deadline = started + _PROVIDER_BUDGET
    try:
        if adapter == "deepseek":
            _adapt_deepseek(cred, deadline, item)
        elif adapter == "moonshot":
            _adapt_moonshot(cred, deadline, item)
        elif adapter == "siliconflow":
            _adapt_siliconflow(cred, deadline, item)
        elif adapter == "openrouter":
            _adapt_openrouter(cred, deadline, item)
        else:
            _adapt_relay(cred, deadline, item, adapter)
    except Exception as exc:  # noqa: BLE001 - 适配器内部异常也降级
        _fail(item, "查询异常：%s" % type(exc).__name__)

    item["latency_ms"] = int((time.monotonic() - started) * 1000)
    cache[cache_key] = {
        "adapter": item["adapter"],
        "ok": item["ok"],
        "balance": item["balance"],
        "currency": item["currency"],
        "detail": item["detail"],
        "error": item["error"],
        "latency_ms": item["latency_ms"],
    }
    return item


# ---------------------------------------------------------------------------
# 读库（仅供自测入口使用；Lead 的 sources.py 会自己读库后把行传进来）
# ---------------------------------------------------------------------------

def read_provider_rows(db_path: str | None = None) -> list[dict]:
    """只读打开 cc-switch 库，返回 providers 行（附加 endpoints 列表）。

    用 ``mode=ro`` URI：CC Switch 可能正在写库（WAL 模式），只读打开不会加写锁。
    """
    path = db_path or DEFAULT_DB_PATH
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    uri = Path(path).as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=3.0)
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        rows = [dict(r) for r in cursor.execute(
            "SELECT id, app_type, name, settings_config, website_url, category, "
            "is_current, sort_index FROM providers ORDER BY sort_index, id"
        )]
        endpoint_map: dict[str, list[str]] = {}
        try:
            for row in cursor.execute("SELECT provider_id, app_type, url FROM provider_endpoints"):
                url = row["url"]
                if isinstance(url, str) and url.startswith(("http://", "https://")):
                    endpoint_map.setdefault(str(row["provider_id"]), []).append(url)
        except sqlite3.Error:
            endpoint_map = {}
        for row in rows:
            row["endpoints"] = endpoint_map.get(str(row["id"]), [])
        return rows
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 自测入口：python backend\balances.py
# ---------------------------------------------------------------------------

def _display_width(text: str) -> int:
    """中文按 2 列宽计算，保证表格对齐。"""
    width = 0
    for char in str(text):
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def _pad(text: str, width: int) -> str:
    """按显示宽度左对齐补空格。"""
    text = str(text)
    return text + " " * max(0, width - _display_width(text))


def _truncate(text: str, width: int) -> str:
    """按显示宽度截断（超出部分用省略号），保证表格不炸行。"""
    text = str(text)
    if _display_width(text) <= width:
        return text
    kept: list[str] = []
    used = 0
    for char in text:
        char_width = 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        if used + char_width > width - 1:
            break
        kept.append(char)
        used += char_width
    return "".join(kept) + "…"


def _print_table(items: list[dict], db_path: str, elapsed_ms: int) -> None:
    """打印对齐的结果表（列：name / app_type / adapter / ok / balance / currency / detail / error）。"""
    columns = ["name", "app_type", "adapter", "ok", "balance", "currency", "detail", "error"]
    # detail / error 允许换行前截断，其余列按内容自适应宽度
    caps = {"detail": 44, "error": 74}

    table: list[list[str]] = []
    for item in items:
        balance = item.get("balance")
        table.append([
            str(item.get("name") or "-"),
            str(item.get("app_type") or "-"),
            str(item.get("adapter") or "-"),
            "True" if item.get("ok") else "False",
            "-" if balance is None else "%.2f" % float(balance),
            str(item.get("currency") or "-"),
            str(item.get("detail") or "-"),
            str(item.get("error") or "-"),
        ])

    widths = []
    for index, title in enumerate(columns):
        width = max([_display_width(title)] + [_display_width(row[index]) for row in table])
        if title in caps:
            width = min(width, caps[title])
        widths.append(width)

    header = "  ".join(_pad(title, width) for title, width in zip(columns, widths))
    print(header)
    print("-" * _display_width(header))
    ok_count = 0
    for item, row in zip(items, table):
        if item.get("ok"):
            ok_count += 1
        cells = [_pad(_truncate(value, width), width) for value, width in zip(row, widths)]
        print("  ".join(cells).rstrip())
    print("-" * _display_width(header))
    print("共 %d 家 / 成功 %d / 失败 %d / 总耗时 %d ms" % (len(items), ok_count, len(items) - ok_count, elapsed_ms))
    print("数据库：%s（只读 mode=ro）" % db_path)
    print("说明：api_key 仅在内存中用于请求，本表与返回结构不含任何密钥或密钥片段。")


def main(argv: list[str] | None = None) -> int:
    """自测入口：读库 → query_all → 打印对齐表格。"""
    # Windows 控制台可能是 GBK，统一成 UTF-8 避免中文表格乱码
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    db_path = (argv[1] if argv and len(argv) > 1 else DEFAULT_DB_PATH)
    print("TokenLens 供应商余额查询（只读 cc-switch 配置，密钥不落盘）")
    try:
        rows = read_provider_rows(db_path)
    except FileNotFoundError:
        print("找不到 cc-switch 数据库：%s" % db_path)
        return 1
    except sqlite3.Error as exc:
        print("读取数据库失败：%s" % exc.__class__.__name__)
        return 1

    started = time.monotonic()
    items = query_all(rows)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    _print_table(items, db_path, elapsed_ms)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
