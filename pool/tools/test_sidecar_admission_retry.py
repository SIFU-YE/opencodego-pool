#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归测试：sidecar 对上游容量准入 429 的有界等待重试（2026-10-29 / 2026-11-06）。

背景（2026-10-29 08:59 实锤）：新会话首发偶发
  429 {"error":{"message":"... [ttft_admission_refused] the cell is at capacity;
       retry shortly (after 16 s). Received Model Group=deepseek-v4.1-flash@a9s7"}}
上游建议等待后重试；DSH retryPolicy backoff 初始 800ms/上限 15s，3 次尝试都落在同一饱和窗口，
用户侧表现为「重试延迟 1.7s → 整轮失败」。sidecar 现按建议等待（默认上限 30s）后原体重发，最多 2 次。
2026-11-06 新增 [prefill_cap_refused]（实测 after 5s / after 30s）与 [overloaded]
（the server is overloaded; retry shortly），共用同一重试预算。

覆盖情形：
  A 准入 429（ttft, after 16 s）→ 重发后 200：客户端 200，上游 2 次，等待恰好 16s
  B 配额 429（Go usage limit exceeded）→ **不重试**：客户端 429，上游 1 次，零等待
  C 准入 429 持续故障 → 有界 3 次尝试后透传 429（不得无限重发），等待 16s ×2
  D 建议等待解析：after 16 s→16 / after 99 s→上限 30 / after 9 s→9 / after 30 s→30 / 无提示→默认 5
  E 首次 200 → 不重试（行为与改动前一致）
  F prefill 准入 429（after 5 s）→ 重发后 200，等待 5s
  G prefill 准入 429 持续 → 有界 3 次后透传，等待 5s ×2
  H prefill 准入 429（after 30 s）→ 重发后 200，等待 30s（验证默认上限已提到 30s）
  I overloaded 429（无建议秒数）→ 重发后 200，等待默认 5s
  J overloaded 429 持续 → 有界 3 次后透传，等待 5s ×2

跑法：python3 pool/tools/test_sidecar_admission_retry.py
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

ADMISSION_429 = (b'{"error":{"message":"litellm.RateLimitError: RateLimitError: OpenAIException - '
                 b'Upstream request failed: [ttft_admission_refused] the cell is at capacity; '
                 b'retry shortly (after 16 s). Received Model Group=deepseek-v4.1-flash@a9s7",'
                 b'"type":"throttling_error","param":null,"code":"429"}}')
ADMISSION_429_9S = ADMISSION_429.replace(b"after 16 s", b"after 9 s")
PREFILL_429 = (b'{"error":{"message":"litellm.RateLimitError: RateLimitError: OpenAIException - '
               b'Upstream request failed: [prefill_cap_refused] the cell is at capacity; '
               b'retry shortly (after 5 s). Received Model Group=deepseek-v4.1-flash@a5s2",'
               b'"type":"throttling_error","param":null,"code":"429"}}')
PREFILL_429_30S = PREFILL_429.replace(b"after 5 s", b"after 30 s")
OVERLOADED_429 = (b'{"error":{"message":"litellm.RateLimitError: RateLimitError: OpenAIException - '
                  b'Upstream request failed: [overloaded] the server is overloaded; retry shortly. '
                  b'Received Model Group=deepseek-v4.1-flash@a10s6",'
                  b'"type":"throttling_error","param":null,"code":"429"}}')
QUOTA_429 = (b'{"error":{"message":"litellm.RateLimitError: RateLimitError: OpenAIException - '
             b'Go usage limit exceeded. Received Model Group=deepseek-v4.1-flash@a9s7",'
             b'"type":"throttling_error","param":null,"code":"429"}}')


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class FakeUpstream:
    """前 error_hits 次按脚本回 error_status/error_body，之后回 200。error_hits=99 模拟持续故障。"""

    def __init__(self, error_body, error_status=429, error_hits=1):
        self.hits = []
        self.error_body = error_body
        self.error_status = error_status
        self.error_hits = error_hits
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(n)
                outer.hits.append(self.path)
                if len(outer.hits) <= outer.error_hits and outer.error_status != 200:
                    self.send_response(outer.error_status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(outer.error_body)))
                    self.end_headers()
                    self.wfile.write(outer.error_body)
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
    sys.path.insert(0, os.environ.get("SIDECAR_DIR", "/opt/litellm"))
    sys.path.insert(0, os.path.dirname(os.path.abspath(SIDECAR)))
    spec = importlib.util.spec_from_file_location("sidecar_under_test", SIDECAR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def case(label, error_body, error_status, expect_client, expect_hits, error_hits=1, expect_waits=None):
    up = FakeUpstream(error_body, error_status, error_hits)
    mod = load_sidecar()
    mod.UP_HOST, mod.UP_PORT = "127.0.0.1", up.port
    logs, waits = [], []
    mod.slog = logs.append
    mod._admission_wait = waits.append
    port = free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), mod.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    body = json.dumps({"model": "deepseek-v4.1-flash",
                       "messages": [{"role": "user", "content": "ping [pool-tree: test-adm429]"}]}).encode()
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    c.request("POST", "/v1/chat/completions", body=body,
              headers={"Content-Type": "application/json", "Authorization": "Bearer test"})
    resp = c.getresponse()
    got = resp.status
    resp.read()
    c.close()
    srv.shutdown()

    retried = any("upstream-admission-429" in str(x) for x in logs)
    ok = (got == expect_client and len(up.hits) == expect_hits)
    if expect_waits is not None:
        ok = ok and [round(w) for w in waits] == expect_waits
    print("%-4s %-44s client=%s(期望%s) hits=%d(期望%d) waits=%s retried=%s"
          % ("PASS" if ok else "FAIL", label, got, expect_client, len(up.hits), expect_hits,
             [round(w) for w in waits], retried))
    if not ok:
        print("      logs:", logs[:4])
    return ok


def unit_parse(mod):
    checks = [
        ("after 16 s", 16.0),
        ("after 99 s", mod.ADMISSION_429_MAX_WAIT_S),
        ("after 9 s", 9.0),
        ("after 30 s", 30.0),
        ("after 5.5 s", 5.5),
        ("capacity no hint", mod.ADMISSION_429_DEFAULT_WAIT_S),
    ]
    ok = True
    for text, want in checks:
        body = ("retry shortly (%s)" % text).encode() if "after" in text else text.encode()
        got = mod._admission_retry_after_s(body)
        good = abs(got - want) < 1e-6
        ok = ok and good
        print("%-4s 解析 %-24r → %.1fs（期望 %.1fs）" % ("PASS" if good else "FAIL", body.decode(), got, want))
    return ok


def main():
    mod0 = load_sidecar()
    results = [
        unit_parse(mod0),
        case("A 准入429(after 16s) → 重发后200", ADMISSION_429, 429, 200, 2, expect_waits=[16]),
        case("B 配额429 → 不重试透传", QUOTA_429, 429, 429, 1, expect_waits=[]),
        case("C 准入429持续 → 有界3次后透传", ADMISSION_429, 429, 429, 3, error_hits=99, expect_waits=[16, 16]),
        case("D 准入429(9s) → 重发后200", ADMISSION_429_9S, 429, 200, 2, expect_waits=[9]),
        case("E 直接200 → 不重试", b"", 200, 200, 1, expect_waits=[]),
        case("F prefill准入429(5s) → 重发后200", PREFILL_429, 429, 200, 2, expect_waits=[5]),
        case("G prefill准入429持续 → 有界3次后透传", PREFILL_429, 429, 429, 3, error_hits=99, expect_waits=[5, 5]),
        case("H prefill准入429(30s) → 重发后200", PREFILL_429_30S, 429, 200, 2, expect_waits=[30]),
        case("I overloaded429 → 重发后200", OVERLOADED_429, 429, 200, 2, expect_waits=[5]),
        case("J overloaded429持续 → 有界3次后透传", OVERLOADED_429, 429, 429, 3, error_hits=99, expect_waits=[5, 5]),
    ]
    print("RESULT:", "ALL PASS" if all(results) else "FAILED")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
