#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归测试：上游「等到响应头超时」的重试与 502 边界（2026-10-23）。

背景（生产实测，18 小时日志）：
  71 次请求等到 120s 上限；其中 48 次发生在首连，25 次换连接后 ~2s 就 200，
  23 次两次尝试都卡 → 客户端可见 502（`session-sidecar upstream error`）。
  即：多数不是「上游真慢」而是「首条连接卡住」，只重试 1 次会把近半数卡顿变成 502。
  修复：重试次数可配（默认 3）+ 重试前小睡 + 超时日志带 model（归因到账号别名）+ 最终 502 单独记一行。

覆盖：
  A 首两次卡、第三次 200 → 客户端 200，上游恰收到 3 次（重试生效）
  B 每次都卡 → 客户端 502 + 固定错误体，上游收到 == attempts 次
  C 默认 attempts=3（环境变量未设时）
  D 环境变量可覆盖 attempts / 每次预算
  E 超时日志带 model=（可归因），且最终 502 有 upstream-header-timeout-final

跑法：python3 pool/tools/test_header_timeout.py     退出码 0 = 全过
"""
import importlib.util
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
SIDECAR = os.path.abspath(os.environ.get("SIDECAR_PATH") or os.path.join(HERE, "..", "session-sidecar.py"))
if not os.path.exists(SIDECAR) and os.path.exists("/opt/litellm/session-sidecar.py"):
    SIDECAR = "/opt/litellm/session-sidecar.py"

fails = []


def ok(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  :: " + str(detail)) if detail else ""))
    if not cond:
        fails.append(name)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class FakeUpstream:
    """delay_hits: {第几次命中: 延迟秒数}；未列出的命中立刻回 200。"""

    def __init__(self, delay_hits):
        self.delay_hits = dict(delay_hits)
        self.hits = []
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n)
                outer.hits.append(len(body))
                d = outer.delay_hits.get(len(outer.hits), 0)
                if d:
                    time.sleep(d)
                try:
                    model = (json.loads(body) or {}).get("model")
                except Exception:
                    model = None
                payload = json.dumps({"ok": True, "hit": len(outer.hits), "model": model}).encode()
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except Exception:
                    pass

            do_GET = do_POST

        self.port = free_port()
        self.srv = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        self.srv.daemon_threads = True
        # 客户端在「延迟窗口内」主动断开时 Windows 会抛 ConnectionAbortedError，
        # 这是被测行为本身（超时重发），不是测试失败 —— 静音，保持输出可读。
        self.srv.handle_error = lambda request, client_address: None
        self.th = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.th.start()

    def close(self):
        try:
            self.srv.shutdown()
        except Exception:
            pass


def load_sidecar():
    if not os.path.exists(SIDECAR):
        print("SKIP: sidecar not found at %s" % SIDECAR)
        sys.exit(0)
    sys.path.insert(0, os.path.dirname(SIDECAR))
    spec = importlib.util.spec_from_file_location("sc_hdr", SIDECAR)
    sc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sc)
    return sc


def serve(sc, logs):
    port = free_port()
    sc.LISTEN = ("127.0.0.1", port)
    sc._orig_slog = sc.slog
    sc.slog = lambda msg: logs.append(str(msg))
    srv = ThreadingHTTPServer(sc.LISTEN, sc.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


def post(port, body, timeout=120):
    req = urllib.request.Request(
        "http://127.0.0.1:%d/v1/chat/completions" % port,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer test"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return None, str(e)[:120]


sc = load_sidecar()
print("sidecar: %s" % SIDECAR)
print("defaults: timeout=%.1fs attempts=%d retry_delay=%.1fs"
      % (sc.HEADER_WAIT_TIMEOUT_S, sc.HEADER_WAIT_ATTEMPTS, sc.HEADER_WAIT_RETRY_DELAY_S))

# C) 默认配置断言（未设环境变量时应为 3 次机会）
ok("默认重试次数=3（只重试 1 次会把近半数卡顿变成 502）", sc.HEADER_WAIT_ATTEMPTS == 3, sc.HEADER_WAIT_ATTEMPTS)
ok("默认每轮预算仍是 120s", sc.HEADER_WAIT_TIMEOUT_S == 120, sc.HEADER_WAIT_TIMEOUT_S)

BODY = {"model": "deepseek-v4.1-flash@a6s3",
        "messages": [{"role": "user", "content": "ping"}], "max_tokens": 5}

# A) 首两次卡、第三次成功 → 客户端 200，上游 3 次
up = FakeUpstream({1: 0.6, 2: 0.6})
logs = []
srv = None
try:
    sc.UP_HOST, sc.UP_PORT = "127.0.0.1", up.port
    sc.HEADER_WAIT_TIMEOUT_S = 0.3
    sc.HEADER_WAIT_ATTEMPTS = 3
    sc.HEADER_WAIT_RETRY_DELAY_S = 0
    sc.HEADER_WAIT_WARN_S = 0.05
    srv, port = serve(sc, logs)
    code, text = post(port, BODY)
    ok("A 首两次卡、第三次成功 → 客户端拿到 200", code == 200, "%s %s" % (code, text[:60]))
    ok("A 上游恰收到 3 次（重试生效）", len(up.hits) == 3, len(up.hits))
    hp = [l for l in logs if "upstream-header-timeout" in l]
    ok("A 记了 2 条超时日志", len([l for l in hp if "attempt=" in l and "final" not in l]) == 2, hp[:3])
    ok("A 超时日志带 model=（可归因到账号别名）",
       all("model=deepseek-v4.1-flash@a6s3" in l for l in hp), hp[:2])
finally:
    if srv:
        srv.shutdown()
    up.close()

# B) 每次都卡 → 客户端 502 + 固定错误体，上游 attempts 次
up = FakeUpstream({1: 0.6, 2: 0.6, 3: 0.6, 4: 0.6})
logs = []
srv = None
try:
    sc.UP_HOST, sc.UP_PORT = "127.0.0.1", up.port
    sc.HEADER_WAIT_TIMEOUT_S = 0.25
    sc.HEADER_WAIT_ATTEMPTS = 3
    sc.HEADER_WAIT_RETRY_DELAY_S = 0
    srv, port = serve(sc, logs)
    code, text = post(port, BODY)
    ok("B 全部卡住 → 502", code == 502, "%s %s" % (code, text[:80]))
    ok("B 错误体是固定文案（客户端按此判读）",
       "session-sidecar upstream error" in text, text[:120])
    ok("B 上游收到 attempts=3 次", len(up.hits) == 3, len(up.hits))
    ok("B 最终 502 有单独一行 upstream-header-timeout-final",
       any("upstream-header-timeout-final" in l and "-> 502" in l for l in logs),
       [l for l in logs if "final" in l][:2])
finally:
    if srv:
        srv.shutdown()
    up.close()

# D) 环境变量可覆盖
saved = dict(os.environ)
try:
    os.environ["POOL_HEADER_ATTEMPTS"] = "5"
    os.environ["POOL_HEADER_TIMEOUT_S"] = "45"
    os.environ["POOL_HEADER_RETRY_DELAY_S"] = "0.5"
    sc2 = load_sidecar()
    ok("D POOL_HEADER_ATTEMPTS 可覆盖", sc2.HEADER_WAIT_ATTEMPTS == 5, sc2.HEADER_WAIT_ATTEMPTS)
    ok("D POOL_HEADER_TIMEOUT_S 可覆盖", sc2.HEADER_WAIT_TIMEOUT_S == 45, sc2.HEADER_WAIT_TIMEOUT_S)
    ok("D POOL_HEADER_RETRY_DELAY_S 可覆盖", sc2.HEADER_WAIT_RETRY_DELAY_S == 0.5, sc2.HEADER_WAIT_RETRY_DELAY_S)
    os.environ["POOL_HEADER_ATTEMPTS"] = "0"
    sc3 = load_sidecar()
    ok("D attempts 非法值(0)被夹到 1", sc3.HEADER_WAIT_ATTEMPTS == 1, sc3.HEADER_WAIT_ATTEMPTS)
finally:
    os.environ.clear()
    os.environ.update(saved)

# E) 关闭预算（0）→ 不重试，行为回到旧逻辑
up = FakeUpstream({})
logs = []
srv = None
try:
    sc.UP_HOST, sc.UP_PORT = "127.0.0.1", up.port
    sc.HEADER_WAIT_TIMEOUT_S = 0
    sc.HEADER_WAIT_ATTEMPTS = 3
    srv, port = serve(sc, logs)
    code, text = post(port, BODY)
    ok("E 预算=0 时不设超时、正常 200", code == 200, code)
    ok("E 预算=0 时只打 1 次", len(up.hits) == 1, len(up.hits))
finally:
    if srv:
        srv.shutdown()
    up.close()

print()
if fails:
    print("FAILED %d: %s" % (len(fails), fails))
    sys.exit(1)
print("ALL PASS")
