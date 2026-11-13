#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归测试：账号额度耗尽（配额 429）与上游等头超时后的「当次换号」（2026-11-13）。

背景（生产 11:04–11:06 实锤）：
  · 账号 8 monthly 限额后仍被钉在 `deepseek-v4.1-flash@a8s4`（单账号 model group），
    LiteLLM 无法在同名组内换号；
  · 账号 9（额度剩 2%）的会话 `@a9s2` 等头超时 3 次 → 客户端拿到固定
    502 `{"message":"session-sidecar upstream error"}`。

本测试锁死 sidecar 新增行为：
  A 配额 429（@a9）→ 改写别名到另一个 healthy 账号后重发，客户端 200；
  B 配额 429 持续（所有账号）→ 有界换号后透传 429，不得无限重试；
  C 上游等头超时（@a9 挂住）→ 换别名重发后客户端 200；
  D 非配额 429 的既有容量准入重试行为不变（由 test_sidecar_admission_retry.py 覆盖）。

跑法：python3 pool/tools/test_sidecar_quota_failover.py
退出码 0 = 全过。
"""
import http.client
import importlib.util
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
POOL = os.path.dirname(HERE)
SIDECAR = os.path.abspath(os.environ.get("SIDECAR_PATH") or os.path.join(POOL, "session-sidecar.py"))
TREE = "test-quota-failover"
BASE_MODEL = "deepseek-v4.1-flash"
QUOTA_429 = (b'{"error":{"message":"litellm.RateLimitError: RateLimitError: OpenAIException - '
             b'OpenCode Go usage limit reached (monthly, resets in 6d 5h). '
             b'Received Model Group=deepseek-v4.1-flash@a9s2","code":"429"}}')


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class FakeUpstream:
    """按 model 里的账号号决定响应：quota_accounts → 429；hang_accounts → 挂住等超时；其余 200。"""

    def __init__(self, quota_accounts=(), hang_accounts=()):
        self.hits = []
        self.models = []
        self.quota_accounts = set(quota_accounts or ())
        self.hang_accounts = set(hang_accounts or ())
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n)
                try:
                    model = (json.loads(raw) or {}).get("model") or ""
                except Exception:
                    model = ""
                outer.hits.append(model)
                outer.models.append(model)
                acct = None
                import re as _re
                m = _re.search(r"@a([0-9]+)", model)
                if m:
                    acct = int(m.group(1))
                if acct in outer.hang_accounts:
                    time.sleep(3)
                    try:
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", "2")
                        self.end_headers()
                        self.wfile.write(b"{}")
                    except Exception:
                        pass
                    return
                if acct in outer.quota_accounts:
                    body = QUOTA_429
                    self.send_response(429)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                body = json.dumps({"ok": True, "model": model}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.port = free_port()
        self.srv = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


def write_pool_home(home):
    with open(os.path.join(home, "pool-affinity.json"), "w", encoding="utf-8") as fh:
        json.dump({"mode": "full"}, fh)
    with open(os.path.join(home, "routing-share.json"), "w", encoding="utf-8") as fh:
        json.dump({
            "generated_at": "2026-11-13T00:00:00Z",
            "mode": "shadow", "status": "ok", "source": "test",
            "accounts": [
                {"key_ref": "OCG_KEY_3", "share": 0.45, "hard": False, "low": False,
                 "available": True, "windows": {"monthly": {"status": "ok"}}},
                {"key_ref": "OCG_KEY_5", "share": 0.35, "hard": False, "low": False,
                 "available": True, "windows": {"monthly": {"status": "ok"}}},
                {"key_ref": "OCG_KEY_9", "share": 0.0, "hard": False, "low": True,
                 "available": True, "windows": {"monthly": {"status": "ok"}}},
            ],
        }, fh)
    aliases = []
    for acct in (3, 5, 9):
        for bucket in range(1, 9):
            aliases.append("%s@a%ds%d" % (BASE_MODEL, acct, bucket))
        aliases.append("%s@a%d" % (BASE_MODEL, acct))
    with open(os.path.join(home, "pool-aliases.json"), "w", encoding="utf-8") as fh:
        json.dump({"aliases": aliases}, fh)
    with open(os.path.join(home, "pool-tree-pins.json"), "w", encoding="utf-8") as fh:
        json.dump({"v": 2, "pins": {TREE: {"key": "OCG_KEY_9", "ts": time.time()}}}, fh)


def load_sidecar(home):
    os.environ["POOL_HOME"] = home
    sys.path.insert(0, POOL)
    spec = importlib.util.spec_from_file_location("sidecar_quota_under_test", SIDECAR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_case(label, quota_accounts, hang_accounts, expect_client, expect_hits,
             header_timeout=None):
    home = tempfile.mkdtemp(prefix="sidecar-quota-")
    write_pool_home(home)
    mod = load_sidecar(home)
    if header_timeout is not None:
        mod.HEADER_WAIT_TIMEOUT_S = header_timeout
        mod.HEADER_WAIT_ATTEMPTS = 1
        mod.HEADER_WAIT_RETRY_DELAY_S = 0
        mod.HEADER_WAIT_WARN_S = 0
    up = FakeUpstream(quota_accounts=quota_accounts, hang_accounts=hang_accounts)
    mod.UP_HOST, mod.UP_PORT = "127.0.0.1", up.port
    logs = []
    mod.slog = logs.append
    port = free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), mod.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    body = json.dumps({
        "model": BASE_MODEL,
        "messages": [{"role": "user", "content": "ping [pool-tree: %s]" % TREE}],
    }).encode()
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    c.request("POST", "/v1/chat/completions", body=body,
              headers={"Content-Type": "application/json"})
    try:
        resp = c.getresponse()
        got = resp.status
        resp.read()
    except Exception as e:
        got = "EXC:%s" % str(e)[:60]
    c.close()
    srv.shutdown()
    up.close()
    hits = len(up.hits)
    models = up.models
    failover_logged = any("failover" in str(x) for x in logs)
    ok = (got == expect_client and hits == expect_hits)
    if ok and expect_client == 200:
        # 第一次必须是被钉住的 a9，第二次必须是换号后的其他账号
        ok = bool(models) and "@a9" in models[0] and (len(models) > 1) and "@a9" not in models[1]
        ok = ok and failover_logged
    print("%-4s %-38s client=%s(期望%s) hits=%d(期望%d) models=%s failover=%s"
          % ("PASS" if ok else "FAIL", label, got, expect_client, hits, expect_hits,
             [m.split("@")[-1] for m in models], failover_logged))
    if not ok:
        print("      logs:", logs[:6])
    shutil.rmtree(home, ignore_errors=True)
    return ok


def unit_failover_rewrite(home):
    mod = load_sidecar(home)
    body = json.dumps({
        "model": BASE_MODEL,
        "messages": [{"role": "user", "content": "ping [pool-tree: %s]" % TREE}],
    }).encode()
    out = mod.failover_rewrite(body, {"Content-Type": "application/json"},
                               TREE, BASE_MODEL, {"OCG_KEY_9"})
    ok = bool(out) and "@a9" not in ((json.loads(out[0]) or {}).get("model") or "")
    print("%-4s failover_rewrite 避开 OCG_KEY_9 → %s" %
          ("PASS" if ok else "FAIL", (json.loads(out[0]).get("model") if out else None)))
    return ok


def main():
    home = tempfile.mkdtemp(prefix="sidecar-quota-unit-")
    write_pool_home(home)
    results = [
        unit_failover_rewrite(home),
        run_case("A 配额429(@a9) → 换号后200", quota_accounts={9}, hang_accounts=set(),
                 expect_client=200, expect_hits=2),
        run_case("B 配额429(全部) → 有界换号后透传429", quota_accounts={3, 5, 9},
                 hang_accounts=set(), expect_client=429, expect_hits=3),
        run_case("C 等头超时(@a9) → 换号后200", quota_accounts=set(), hang_accounts={9},
                 expect_client=200, expect_hits=2, header_timeout=1),
    ]
    shutil.rmtree(home, ignore_errors=True)
    print("RESULT:", "ALL PASS" if all(results) else "FAILED")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
