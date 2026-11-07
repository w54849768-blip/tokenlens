"""数据源第 0 号：本机数据（默认实现，也是所有适配器的参考实现）。

TokenLens 原本就是「读本机文件」。这套逻辑本身已经是可插拔的一层：
`server.py` 只认 `server.setup(...)` 传进来的对象，不再关心数据从哪来。
本模块把原来的 `sources.py` 原样包一层，行为与单机版完全一致：

  * CC Switch 代理日志   ~/.cc-switch/cc-switch.db -> proxy_request_logs
  * Claude Code 会话     ~/.claude/projects/**/*.jsonl
  * DeepSeek Harness     ~/.dsh/sessions/**/session.v4.jsonl.zstd
  * Antigravity          ~/.gemini/antigravity/conversations/*.db
  * 供应商余额           providers.settings_config 里的凭据直连官方接口

想接云端数据库（Postgres / ClickHouse / 对象存储），照这份实现写一个新的
模块，导出同名方法即可，见 docs/CLOUD.md。
"""

from __future__ import annotations

# sources.py 是单机版的数据层，保持原样不动
import sources


class LocalSource:
    """本机数据源：所有方法都来自 sources.py。"""

    name = "local"

    # ---- 必需：UI 需要的 7 个端点 ----
    def meta(self):
        m = sources.meta()
        m.setdefault("source", {}).update({"name": self.name, "read_only": True, "mode": "local"})
        return m

    def overview(self, f):
        return sources.overview(f)

    def list_requests(self, f):
        return sources.list_requests(f)

    def requests_csv(self, f):
        return sources.requests_csv(f)

    def chats_list(self, f):
        return sources.chats_list(f)

    def chat_detail(self, sid):
        return sources.chat_detail(sid)

    def chat_markdown(self, sid):
        return sources.chat_markdown(sid)

    def chat_export_json(self, sid):
        return sources.chat_export_json(sid)

    def export_all_chats(self):
        return sources.export_all_chats()

    def providers(self):
        """供应商列表（不含凭据）。"""
        out = []
        for r in sources.providers_raw():
            out.append({
                "id": r.get("id"),
                "name": r.get("name"),
                "app_type": r.get("app_type"),
                "app_label": sources.APP_LABELS.get(r.get("app_type"), r.get("app_type")),
                "category": r.get("category"),
                "website_url": r.get("website_url"),
                "is_current": bool(r.get("is_current")),
                "endpoints": r.get("endpoints") or [],
            })
        return out

    def balances(self, force: bool = False):
        """余额查询。凭据只在进程内存里用，绝不返回给前端。"""
        import time
        import traceback
        from urllib.parse import urlparse

        try:
            import balances  # 平台适配器
        except Exception as exc:
            return {"checked_at": int(time.time() * 1000), "items": [],
                    "error": f"balances 模块未就绪：{exc}"}
        try:
            rows = sources.providers_raw()
            cats = {r.get("id"): r.get("category") for r in rows}
            items = _dedupe(balances.query_all(rows), cats)
            return {"checked_at": int(time.time() * 1000), "items": items}
        except Exception as exc:
            return {"checked_at": int(time.time() * 1000), "items": [],
                    "error": f"查询余额失败：{exc}",
                    "trace": traceback.format_exc(limit=3)}

    # ---- 设置 ----
    def get_settings(self):
        return sources.load_settings()

    def save_settings(self, patch):
        return sources.save_settings(patch)

    def warm_up(self):
        sources.warm_up()


def _dedupe(items, category_map=None):
    """同一家（适配器 + 域名）配置了多个应用时合并成一张卡，结果优先取成功的。"""
    from urllib.parse import urlparse
    category_map = category_map or {}
    groups = {}
    for it in items or []:
        base = (it.get("base_url") or "").rstrip("/")
        host = urlparse(base).netloc or base or it.get("id") or "?"
        key = (it.get("adapter") or "unknown", host)
        g = groups.setdefault(key, {"ok": None, "fail": [], "apps": []})
        app = it.get("app_type")
        if app and app not in g["apps"]:
            g["apps"].append(app)
        if it.get("ok") and it.get("balance") is not None:
            if g["ok"] is None:
                g["ok"] = it
        else:
            g["fail"].append(it)

    out = []
    for g in groups.values():
        src = g["ok"] or g["fail"][0]
        item = dict(src)
        if len(g["apps"]) > 1:
            item["app_type"] = " / ".join(g["apps"])
        if g["ok"] and g["fail"]:
            item["detail"] = (item.get("detail") or "") + f"（另有 {len(g['fail'])} 个配置不可用）"
        elif not g["ok"] and len(g["fail"]) > 1:
            item["detail"] = (item.get("detail") or item.get("error") or "")
        cats = {category_map.get(x.get("id")) for x in ([src] + g["fail"]) if x.get("id")}
        item["category"] = "official" if "official" in cats else (
            "aggregator" if "aggregator" in cats else None)
        item["_ok"] = bool(g["ok"])
        out.append(item)
    out.sort(key=lambda x: (not x.get("_ok"), x.get("name") or ""))
    for x in out:
        x.pop("_ok", None)
    return out


SOURCE = LocalSource()
