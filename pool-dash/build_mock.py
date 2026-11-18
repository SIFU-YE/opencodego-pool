#!/usr/bin/env python3
"""从 dashboard.py 生成 mock 看板（罐头数据）用于本地视觉验证，端口 4011。

2026-10-17 起同步驾驶舱改版：
- mock 会真的调用 `render_page()`，把 **管理控制台页签片段**（pool_admin_ui.build_panel）
  用一份假的 AdminApp.state() 注入进去，因此本地截图能覆盖页签化后的完整页面；
- 同时补齐 `/api/admin/*` 的罐头响应，避免页签里全是加载失败；
- 生成的 mock_dashboard.py 落在当前目录（构建产物，验证完请删除，不要入库）。

用法： cd pool-dash && python3 build_mock.py && python3 mock_dashboard.py
"""
src = open("dashboard.py", encoding="utf-8").read()
head = src[:src.index("class H(")]

stub = '''
import time as _t
def _iso(h):  # 生成最近 h 小时前的整点时间串
    return _t.strftime("%Y-%m-%d %H:00:00", _t.localtime(_t.time() - h * 3600))

def summary():
    return {"health": "alive", "models": 35, "deployments": 35, "accounts": 4,
            "model_names": [], "total": 268,
            "by_status": {200: 231, 429: 26, 500: 6, 404: 5},
            "by_model": [{"model": "mimo-v2.6-pro", "hits": 88, "tokens": 820000000},
                         {"model": "deepseek-v4-flash", "hits": 61, "tokens": 154000000},
                         {"model": "glm-5.3", "hits": 42, "tokens": 50000000},
                         {"model": "deepseek-v4-pro", "hits": 31, "tokens": 31000000},
                         {"model": "kimi-k3", "hits": 19, "tokens": 9500000}],
            "by_hour": [{"h": _iso(h), "n": max(1, int(28 - h * 1.3 + (h * 7) % 11)), "err": (h * 3) % 5 if h % 3 == 0 else 0}
                        for h in range(23, -1, -1)],
            "recent": [{"ts": _iso(0), "method": "POST", "path": "/v1/chat/completions", "code": 200},
                       {"ts": _iso(0), "method": "POST", "path": "/v1/responses", "code": 429},
                       {"ts": _iso(1), "method": "GET", "path": "/v1/models", "code": 200},
                       {"ts": _iso(2), "method": "POST", "path": "/v1/chat/completions", "code": 500}],
            "account_usage": [
                {"key": "OCG_KEY_1", "label": "a1 · 主号", "tail": "1a2b", "slot": 1, "inPool": True,
                 "ok": True, "state": "ok", "limited": [],
                 "windows": {"rolling": {"status": "ok", "percent": 12, "resetsAt": ""},
                             "weekly": {"status": "ok", "percent": 34, "resetsAt": "2026-09-28T00:00:00Z"},
                             "monthly": {"status": "ok", "percent": 57, "resetsAt": "2026-10-01T00:00:00Z"}}},
                {"key": "OCG_KEY_2", "label": "a2 · 备号", "tail": "3c4d", "slot": 2, "inPool": True,
                 "ok": True, "state": "limited", "limited": ["monthly"],
                 "windows": {"rolling": {"status": "ok", "percent": 8, "resetsAt": ""},
                             "weekly": {"status": "ok", "percent": 76, "resetsAt": "2026-09-28T00:00:00Z"},
                             "monthly": {"status": "rate-limited", "percent": 100, "resetsAt": "2026-10-03T00:00:00Z"}}},
                {"key": "OCG_KEY_3", "label": "a3 · 备用", "tail": "5e6f", "slot": 3, "inPool": True,
                 "ok": True, "state": "limited", "limited": ["weekly"],
                 "windows": {"rolling": {"status": "ok", "percent": 45, "resetsAt": ""},
                             "weekly": {"status": "rate-limited", "percent": 100, "resetsAt": "2026-09-28T00:00:00Z"},
                             "monthly": {"status": "ok", "percent": 86, "resetsAt": "2026-09-30T00:00:00Z"}}},
                {"key": "OCG_KEY_4", "label": "a4 · 待修", "tail": "7a8b", "slot": 4, "inPool": False,
                 "ok": False, "state": "error", "limited": [], "windows": {},
                 "error": "HTTP 401"}],
            "accounts_limited": 2,
            "usage_totals": {"hits": 268, "pt": 61000000, "ct": 9800000, "tt": 70800000,
                             "firstTs": 1758810000000, "lastTs": 1758890000000},
            "last24": {"hits": 96, "pt": 21000000, "ct": 3300000, "tt": 24300000},
            "by_account": [
                {"account": "a1 · 主号", "tail": "1a2b", "hits": 142, "pt": 32000000, "ct": 5100000,
                 "tt": 37100000, "h24_tt": 12400000, "lastTs": 1758890000000,
                 "models": [{"model": "mimo-v2.6-pro", "hits": 88, "tokens": 24000000},
                            {"model": "deepseek-v4-flash", "hits": 34, "tokens": 9000000},
                            {"model": "glm-5.3", "hits": 20, "tokens": 4100000}]},
                {"account": "a2 · 备号", "tail": "3c4d", "hits": 66, "pt": 15000000, "ct": 2400000,
                 "tt": 17400000, "h24_tt": 8100000, "lastTs": 1758888000000,
                 "models": [{"model": "deepseek-v4-flash", "hits": 27, "tokens": 6400000},
                            {"model": "kimi-k3", "hits": 19, "tokens": 9500000}]},
                {"account": "a3 · 备用", "tail": "5e6f", "hits": 60, "pt": 14000000, "ct": 2300000,
                 "tt": 16300000, "h24_tt": 3800000, "lastTs": 1758884000000,
                 "models": [{"model": "glm-5.3", "hits": 22, "tokens": 4600000},
                            {"model": "deepseek-v4-pro", "hits": 31, "tokens": 31000000}]}],
            "tokens_by_hour": [{"h": _iso(h), "tt": max(1000, int(3200000 - h * 90000 + (h * 13) % 40 * 20000))}
                               for h in range(23, -1, -1)],
            "by_model": [{"model": "mimo-v2.6-pro", "hits": 88, "pt": 39000000, "ct": 6200000, "tokens": 45200000},
                         {"model": "deepseek-v4-flash", "hits": 61, "pt": 12000000, "ct": 1900000, "tokens": 13900000},
                         {"model": "glm-5.3", "hits": 42, "pt": 6000000, "ct": 900000, "tokens": 6900000},
                         {"model": "kimi-k3", "hits": 19, "pt": 3400000, "ct": 610000, "tokens": 4010000}],
            "model_perf": [{"model": "mimo-v2.6-pro", "n": 88, "medMs": 8200, "p90Ms": 21000, "ctPerSec": 41.2},
                           {"model": "deepseek-v4-flash", "n": 61, "medMs": 5200, "p90Ms": 12800, "ctPerSec": 63.8},
                           {"model": "glm-5.3", "n": 42, "medMs": 11500, "p90Ms": 30400, "ctPerSec": 27.5},
                           {"model": "kimi-k3", "n": 19, "medMs": 6800, "p90Ms": 15200, "ctPerSec": 49.1}],
            "concurrency": {"peak": 7, "avg": 1.8, "hours": 24, "requests": 268,
                            "accounts": [{"account": "a1 · 主号", "peak": 7, "avg": 2.4, "hits": 142, "tt": 37100000},
                                         {"account": "a2 · 备号", "peak": 4, "avg": 1.3, "hits": 66, "tt": 17400000},
                                         {"account": "a3 · 备用", "peak": 3, "avg": 1.1, "hits": 60, "tt": 16300000}]},
            "by_source": {"local": {"hits": 214, "pt": 50000000, "ct": 8000000, "tt": 58000000},
                          "ext": {"hits": 54, "pt": 11000000, "ct": 1800000, "tt": 12800000}},
            "src_by_hour": [{"h": _iso(h), "local": max(1, 9 - h % 7), "ext": (h * 3) % 5}
                            for h in range(23, -1, -1)],
            "cache_rate": {"overall": {"pt": 61000000, "cached": 26000000, "rate": 42.6},
                           "by_model": [{"model": "mimo-v2.6-pro", "pt": 39000000, "cached": 19000000, "rate": 48.7, "hits": 88},
                                        {"model": "deepseek-v4-flash", "pt": 12000000, "cached": 5200000, "rate": 43.3, "hits": 61},
                                        {"model": "glm-5.3", "pt": 6000000, "cached": 1400000, "rate": 23.3, "hits": 42}],
                           "by_hour": [{"h": _iso(h), "rate": max(5, 55 - h)} for h in range(23, -1, -1)]},
            "top_sessions": [{"sess": "sess-9f31ab", "hits": 42, "tt": 12400000, "accounts": 1,
                              "models": [{"model": "mimo-v2.6-pro"}, {"model": "glm-5.3"}]},
                             {"sess": "ext-7c02de", "hits": 31, "tt": 8100000, "accounts": 2,
                              "models": [{"model": "deepseek-v4-flash"}]},
                             {"sess": "sess-1b88c4", "hits": 18, "tt": 3900000, "accounts": 1,
                              "models": [{"model": "kimi-k3"}]}],
            "routing": None}

def account_sessions():
    return {"accounts": [
        {"account": "a1 · 主号", "hits": 142, "tokensTotal": 37100000,
         "models": [{"model": "mimo-v2.6-pro", "hits": 88, "tokens": 24000000},
                    {"model": "deepseek-v4-flash", "hits": 34, "tokens": 9000000},
                    {"model": "glm-5.3", "hits": 20, "tokens": 4100000}],
         "sessions": [{"title": "千万字小说生成管线优化", "firstMsg": "帮我优化小说生成管线的并发与限流", "model": "mimo-v2.6-pro",
                       "ws": "wordsandbox", "createdAt": 1758810000000, "lastActive": 1758890000000, "hits": 30},
                      {"title": "agent-team 审计修复", "firstMsg": "继续按审计报告修剩下的问题", "model": "deepseek-v4-flash",
                       "ws": "agent-team", "createdAt": 1758850000000, "lastActive": 1758880000000, "hits": 8}]},
        {"account": "a2 · 备号", "hits": 66, "tokensTotal": 17400000,
         "models": [{"model": "deepseek-v4-flash", "hits": 27, "tokens": 6400000},
                    {"model": "kimi-k3", "hits": 19, "tokens": 9500000}],
         "sessions": []}],
        "orphans": [{"title": "零散问答", "firstMsg": "介绍一下 MCP", "model": "kimi-k3",
                     "ws": "root", "createdAt": 1758860000000, "lastActive": 1758870000000}],
        "sessionsTotal": 9, "logRows": 268, "note": "mock"}

def dsh_models_config():
    return {"available": ["deepseek-v4-flash", "deepseek-v4-pro", "glm-5.3", "kimi-k3", "mimo-v2.6-flash"],
            "providers": {n: {"api": "openai-completions", "displayName": n, "selected": ["glm-5.3", "mimo-v2.6-flash"]}
                          for n in PROVIDERS}}

def teams_config():
    return {"available": ["deepseek-v4-flash", "deepseek-v4-pro", "glm-5.3", "kimi-k3", "mimo-v2.6-flash"],
            "present": True,
            "config": {"memberModel": "mimo-v2.6-flash", "memberMaxDepth": 0, "maxMembers": 8,
                       "profiles": {"pool-team": {"description": "mock 模板",
                                    "members": [{"name": "planner", "role": "teamleader", "model": "deepseek-v4-pro"},
                                                {"name": "worker", "role": "teammate", "model": "glm-5.3"}]}}}}

def sync_dsh_models(sel): return {"ok": True, "backup": "mock", "counts": {}}
def save_teams_config(c, r): return {"ok": True, "backup": "mock", "profiles": 1}
def restart_dsh(): return {"restarted": True, "service": "mock"}

# ---- 管理控制台页签：注入真实片段 + 罐头 /api/admin/* ----
import pool_admin_ui as _pool_admin_ui

class _MockAdmin:
    def state(self):
        return {"project": "OpencodeLitellmPool", "home": "/opt/litellm",
                "config_path": "/opt/litellm/pool-config.json",
                "control_ok": True, "reconciler_ok": True,
                "readonly": False, "uninitialized": False, "reason": "",
                "affinity_mode": "shadow",
                "capabilities": {"models": True, "accounts": True, "keys": True,
                                 "affinity": True, "audit": True}}

_ADMIN = _MockAdmin()

_ADMIN_GET = {
    "status": {"hotPath": "available", "home": "/opt/litellm", "jobs": 0, "previews": 0,
               "capabilities": {"models": True, "accounts": True, "keys": True, "affinity": True, "audit": True},
               "reconciler_ok": True},
    "models": {"allow": ["mimo-v2.6-pro", "deepseek-v4-flash", "glm-5.3"],
               "deployed": ["mimo-v2.6-pro", "deepseek-v4-flash"],
               "available": ["mimo-v2.6-pro", "mimo-v2.6-flash", "deepseek-v4-flash",
                             "deepseek-v4-pro", "glm-5.3", "kimi-k3"],
               "accountCount": 3, "deployments": 18},
    "accounts": {"accounts": [
        {"slot": 1, "key_var": "OCG_KEY_1", "email": "main@example.com", "fingerprint": "1a2b…9f0e",
         "enabled": True, "keyPresent": True},
        {"slot": 2, "key_var": "OCG_KEY_2", "email": "backup@example.com", "fingerprint": "3c4d…7a8b",
         "enabled": True, "keyPresent": True},
        {"slot": 3, "key_var": "OCG_KEY_3", "email": "spare@example.com", "fingerprint": "5e6f…1c2d",
         "enabled": False, "keyPresent": True}],
        "enabledCount": 2, "nextSlot": 4},
    "instance-keys": {"keys": [
        {"label": "dsh-desktop", "tail": "9f31ab", "lastUsed": "—"},
        {"label": "openviking", "tail": "7c02de", "lastUsed": "—"}]},
    "affinity": {"mode": "shadow", "controlMode": "shadow", "mirrorMode": "shadow"},
    "audit": {"entries": [
        {"ts": "2026-10-17T15:30:12+08:00", "action": "models.apply", "target": "+glm-5.3 / -kimi-k3", "result": "ok"},
        {"ts": "2026-10-17T14:02:44+08:00", "action": "accounts.rotate", "target": "a2 · …3c4d", "result": "ok"},
        {"ts": "2026-10-16T22:11:03+08:00", "action": "affinity.apply", "target": "shadow → full", "result": "ok"}]},
}

class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body.encode())
    def log_message(self, *a): pass
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, render_page(), "text/html; charset=utf-8")
        elif path == "/vendor/echarts.min.js":
            # dashfix：mock 也要发 vendored ECharts，否则页面走 bootWarn 分支、图表全空，
            # 本地自检会把「mock 缺静态资源」误判成「页面图表坏了」。
            try:
                with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "vendor", "echarts.min.js"), "rb") as fh:
                    body = fh.read()
            except Exception:
                self._send(404, "not found", "text/plain")
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/javascript; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        elif path in ("/admin", "/admin/"):
            # 与线上 dashboard.py 同一段前缀无关跳转脚本，便于本地验证 /admin 不再是死链
            self._send(200, "<script>location.replace(location.pathname.replace(/admin\\\\/?$/, '')"
                           " + '#admin')</script>", "text/html; charset=utf-8")
        elif path.startswith("/api/admin/"):
            key = path[len("/api/admin/"):]
            if key in _ADMIN_GET:
                self._send(200, json.dumps({"ok": True, "data": _ADMIN_GET[key]}, ensure_ascii=False),
                           "application/json; charset=utf-8")
            else:
                self._send(404, '{"ok":false,"error":{"code":"not_found","message":"mock 未实现"}}',
                           "application/json; charset=utf-8")
        elif path == "/api/summary":
            self._send(200, json.dumps(summary(), ensure_ascii=False), "application/json; charset=utf-8")
        elif path == "/api/account-sessions":
            self._send(200, json.dumps(account_sessions(), ensure_ascii=False), "application/json; charset=utf-8")
        elif path == "/api/models-config":
            self._send(200, json.dumps(dsh_models_config(), ensure_ascii=False), "application/json; charset=utf-8")
        elif path == "/api/teams-config":
            self._send(200, json.dumps(teams_config(), ensure_ascii=False), "application/json; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain")
    def do_POST(self):
        ln = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(ln)
        self._send(200, '{"ok": true, "mock": true}', "application/json; charset=utf-8")

print("mock on http://127.0.0.1:4011")
ThreadingHTTPServer(("127.0.0.1", 4011), H).serve_forever()
'''

open("mock_dashboard.py", "w", encoding="utf-8").write(head + stub)
print("mock_dashboard.py written")
