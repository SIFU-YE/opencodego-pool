#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归测试：sidecar 对上游偶发误判/错标 400 的**有界重试**（2026-10-17 建；2026-10-27 扩签名 + 预算 2）。

签名白名单（TRANSIENT_400_MARKS）：
  · 旧签名：`[unsupported_request] Provide between 1 and 512 text messages`（unsupported_request 误判）
  · 新签名：`Connection prematurely closed BEFORE response` / `Streaming response failed`
    —— 上游把流式传输故障错标成 400 + type=server_error（2026-10-27 02:16 实锤原文见 PREMATURE_400）

覆盖情形：
  A 旧签名 400 → 重发后 200 → 客户端拿到 200，上游恰收到 2 次
  B 首次 400 但签名不同 → **不得重试**，客户端拿到 400，上游只收到 1 次
  C 首次就 200 → 不得重试（行为与改动前一致）
  D 新签名 400 → 重发后 200 → 客户端拿到 200，上游恰 2 次
  E 新签名 400 持续故障 → 有界 3 次尝试后透传 400（不得无限重发）
  F 连续 2 次新签名 400 → 第 2 次重发成功（预算 2 生效），上游 3 次

跑法（服务器上）：python3 /opt/litellm/tools/test_sidecar_transient_retry.py
退出码 0 = 全过。
"""
import http.client
import importlib.util
import json
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SIDECAR = os.path.abspath(os.environ.get("SIDECAR_PATH") or os.path.join(HERE, "..", "session-sidecar.py"))
if not os.path.exists(SIDECAR) and os.path.exists("/opt/litellm/session-sidecar.py"):
    SIDECAR = "/opt/litellm/session-sidecar.py"

SIGNATURE = (b'{"error":{"type":"server_error","message":"Upstream request failed: '
             b'[unsupported_request] Provide between 1 and 512 text messages."}}')
OTHER_400 = b'{"error":{"type":"invalid_request_error","message":"Messages with role (tool) must be a response."}}'
# 2026-10-27 新签名（02:16 实锤原文）：上游把流式传输故障错标成 400 + type=server_error
PREMATURE_400 = (b'{"error":{"param":"Connection prematurely closed BEFORE response","type":"server_error",'
                 b'"message":"Streaming response failed: [400] Request failed"}}')


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class FakeUpstream:
    """前 error_hits 次按脚本回（默认 1），之后回 200。error_hits=99 模拟持续故障。"""

    def __init__(self, first_body, first_status=400, error_hits=1):
        self.hits = []
        self.first_body = first_body
        self.first_status = first_status
        self.error_hits = error_hits
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n)
                outer.hits.append((self.path, len(body)))
                if len(outer.hits) <= outer.error_hits and outer.first_status != 200:
                    self.send_response(outer.first_status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(outer.first_body)))
                    self.end_headers()
                    self.wfile.write(outer.first_body)
                else:
                    payload = json.dumps({"ok": True, "hit": len(outer.hits)}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)

        self.port = free_port()
        self.srv = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


def load_sidecar():
    # sidecar 会 import 同装在 /opt/litellm 的 toolrepair，先把两侧目录挂进 sys.path
    sys.path.insert(0, os.environ.get("SIDECAR_DIR", "/opt/litellm"))
    sys.path.insert(0, os.path.dirname(os.path.abspath(SIDECAR)))
    spec = importlib.util.spec_from_file_location("sidecar_under_test", SIDECAR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def case(label, first_body, first_status, expect_client, expect_hits, error_hits=1):
    up = FakeUpstream(first_body, first_status, error_hits)
    mod = load_sidecar()
    mod.UP_HOST, mod.UP_PORT = "127.0.0.1", up.port
    logs = []
    mod.slog = logs.append
    port = free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), mod.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    body = json.dumps({
        "model": "deepseek-v4.1-flash",
        "messages": [{"role": "user", "content": "ping [pool-tree: test-retry]"}],
        "tools": [{"type": "function", "function": {"name": "x", "parameters": {"type": "object"}}}],
    }).encode()

    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    c.request("POST", "/v1/chat/completions", body=body,
              headers={"Content-Type": "application/json", "Authorization": "Bearer test"})
    resp = c.getresponse()
    got = resp.status
    resp.read()
    c.close()
    srv.shutdown()

    retried = any("upstream-transient-400" in str(x) for x in logs)
    ok = (got == expect_client and len(up.hits) == expect_hits)
    print("%-4s %-42s client=%s(期望%s) upstream_hits=%d(期望%d) retried=%s"
          % ("PASS" if ok else "FAIL", label, got, expect_client, len(up.hits), expect_hits, retried))
    if not ok:
        print("      logs:", logs[:3])
    return ok


def main():
    results = [
        case("A 旧签名 → 重试后成功", SIGNATURE, 400, 200, 2),
        case("B 其它 400 → 不重试", OTHER_400, 400, 400, 1),
        case("C 直接 200 → 不重试", b"", 200, 200, 1),
        case("D 新签名(流式故障错标400) → 重试后成功", PREMATURE_400, 400, 200, 2),
        case("E 新签名持续故障 → 有界3次后透传", PREMATURE_400, 400, 400, 3, error_hits=99),
        case("F 连续2次签名400 → 第2次重发成功", PREMATURE_400, 400, 200, 3, error_hits=2),
    ]
    print("RESULT:", "ALL PASS" if all(results) else "FAILED")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
