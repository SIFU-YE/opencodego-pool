#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归测试：流式响应必须「能读完」且「静默时不断流」。

覆盖三代问题（每代都真实踩过一次线上故障）：

1) 2026-10-16 终结块套娃：repair_sse 分支曾用 `wchunk(b"0\\r\\n\\r\\n")` 发终结符，
   wchunk() 会再套一层长度前缀 → 真终结块从未发出 → HTTP/1.1 客户端拿到全部内容与
   [DONE] 之后仍等不到流结束，只能挂到自己的 idle 超时（DSH 90s/300s）。
   断言：带 tools 的流式响应能被客户端在超时内读完。

2) 2026-10-17 上游长静默被客户端误杀：上游会成簇地静默 90s+（upstream-stall.log
   里 15 分钟窗口 p50 从 ~7s 跳到 76–170s），静默期 sidecar 一个字节都不发 →
   客户端 `pi-ai stream idle timeout after 90000ms`。修复是静默期补 SSE 注释心跳。
   断言：上游中途静默 2s（心跳间隔设 0.3s）时，响应体里出现 `: hb` 注释帧，
        且流仍然正常收尾。

3) 2026-10-17 流式中途失败挂死：上游中途 RST 时，旧代码会在**响应头已发出后**
   再 send_response(502)，把状态行写进 body 且不补终止块 → 客户端干等。
   断言：客户端立即读到「流结束」而不是超时。

本测试不起真实上游/不联网：在 127.0.0.1:4000 起假上游，以子进程启动真实的
session-sidecar.py（固定监听 127.0.0.1:4001），用带 tools 的请求驱动 repair_sse 分支。

用法：python3 pool/tools/test_sidecar_stream_termination.py
     SIDECAR_PATH=<待测的 session-sidecar.py>  可用于验证旧版本会失败。
"""
import http.client
import http.server
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SIDECAR = os.path.abspath(os.environ.get("SIDECAR_PATH",
                                         os.path.join(HERE, "..", "session-sidecar.py")))
UP_PORT = 4000
SIDE_PORT = 4001
CLIENT_TIMEOUT = 8.0
HB_INTERVAL = 0.3          # 测试用的心跳间隔（线上默认 15s）
SILENCE = 2.0              # 假上游的静默时长，需 > 若干倍 HB_INTERVAL

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

fails = []


def ok(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  :: " + str(detail)) if detail else ""))
    if not cond:
        fails.append(name)


def port_busy(port):
    s = socket.socket()
    try:
        s.settimeout(0.4)
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def frame(choices_delta, finish=None):
    d = {"id": "chatcmpl-test", "object": "chat.completion.chunk", "created": 1,
         "choices": [{"index": 0, "delta": choices_delta, "finish_reason": finish}]}
    return ("data: " + json.dumps(d) + "\n").encode("utf-8")


# 畸形 tool_call（空 id + 空 name）→ 必然走 repair_sse 的 "hold" 逻辑
BAD_TC = {"tool_calls": [{"index": 0, "type": "function", "id": "",
                          "function": {"name": "", "arguments": ""}}]}


class FakeUpstream(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _begin(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _send(self, payload):
        self.wfile.write(b"%x\r\n" % len(payload) + payload + b"\r\n")
        self.wfile.flush()

    def _end(self):
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        scenario = (self.headers.get("X-Test-Scenario") or "normal").strip()

        if scenario == "midfail":
            # 上游中途 RST：已发出两个完整帧后强行重置连接（不带终结块）
            self._begin()
            self._send(frame({"role": "assistant", "content": ""}))
            self._send(frame({"content": "partial-answer"}))
            time.sleep(0.05)
            try:
                self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                           struct.pack("ii", 1, 0))
                self.connection.close()
            except Exception:
                pass
            self.close_connection = True
            return

        self._begin()
        head = [
            frame({"role": "assistant", "content": ""}),
            frame({"content": "working"}),
        ]
        tail = [
            frame(BAD_TC),
            frame({"tool_calls": [{"index": 0, "function": {"arguments": "{\"path\":\"a.txt\"}"}}]}),
            frame({}, finish="tool_calls"),
            b"data: [DONE]\n\n",
        ]
        for c in head:
            self._send(c)
            time.sleep(0.02)
        if scenario == "silence":
            # 模拟上游长静默：这段时间一个字节都不发（客户端只能靠 sidecar 心跳续命）
            time.sleep(SILENCE)
        for c in tail:
            self._send(c)
            time.sleep(0.02)
        self._end()


def post(scenario):
    """发一条带 tools 的流式请求，返回 (端到端耗时, 是否读完, 响应体, 错误)。"""
    body = json.dumps({
        "model": "deepseek-v4.1-flash", "stream": True,
        "tools": [{"type": "function",
                   "function": {"name": "read_file", "parameters": {"type": "object"}}}],
        "messages": [{"role": "user", "content": "read a.txt [pool-tree: term-test-1]"}],
    }).encode("utf-8")
    headers = {"Authorization": "Bearer sk-test", "Content-Type": "application/json"}
    if scenario != "normal":
        headers["X-Test-Scenario"] = scenario
    conn = http.client.HTTPConnection("127.0.0.1", SIDE_PORT, timeout=CLIENT_TIMEOUT)
    t0 = time.time()
    ended, err, raw = False, "", b""
    try:
        conn.request("POST", "/v1/chat/completions", body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()          # 读不到「响应结束」就会在此超时
        ended = True
    except Exception as e:  # noqa: BLE001
        err = "%s: %s" % (type(e).__name__, str(e)[:80])
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return time.time() - t0, ended, raw, err


def main():
    if not os.path.exists(SIDECAR):
        print("SKIP: sidecar not found at %s" % SIDECAR)
        return 0
    for p in (UP_PORT, SIDE_PORT):
        if port_busy(p):
            print("SKIP: 端口 %d 已被占用（本机可能有其它实例）" % p)
            return 0

    upstream = http.server.ThreadingHTTPServer(("127.0.0.1", UP_PORT), FakeUpstream)
    upstream.daemon_threads = True
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    env = dict(os.environ)
    env["POOL_SSE_HEARTBEAT_S"] = str(HB_INTERVAL)
    env["POOL_SSE_MAX_SILENCE_S"] = "30"
    side = subprocess.Popen([sys.executable, SIDECAR], cwd=os.path.dirname(SIDECAR),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    try:
        for _ in range(50):
            if port_busy(SIDE_PORT):
                break
            time.sleep(0.1)
        ok("sidecar 已监听 %d" % SIDE_PORT, port_busy(SIDE_PORT))

        # ---- 场景 1：正常流（含畸形 tool_call → repair/hold）必须能读完 ----
        dur, ended, raw, err = post("normal")
        ok("① 带 tools 的流式响应能被读完（响应体有终止）", ended,
           "耗时 %.2fs %s" % (dur, err))
        ok("① 响应内含 [DONE]", b"data: [DONE]" in raw, "bytes=%d" % len(raw))
        ok("① 正常流不该有心跳噪声", b": hb" not in raw, "bytes=%d" % len(raw))

        # ---- 场景 2：上游静默 2s，sidecar 必须补心跳且流仍收尾 ----
        dur, ended, raw, err = post("silence")
        ok("② 上游静默期间仍能读完（心跳保活）", ended,
           "耗时 %.2fs %s" % (dur, err))
        ok("② 响应体含 SSE 注释心跳 `: hb`", b": hb" in raw,
           "hb 次数=%d" % raw.count(b": hb"))
        ok("② 静默后内容仍完整到达", b"data: [DONE]" in raw, "bytes=%d" % len(raw))

        # ---- 场景 3：上游中途 RST，客户端必须「快速」读到流结束而不是挂死 ----
        dur, ended, raw, err = post("midfail")
        ok("③ 上游中途断开后客户端能读到流结束（不挂到 idle 超时）", ended,
           "耗时 %.2fs %s" % (dur, err))
        ok("③ 断开后客户端很快返回（< 3s）", ended and dur < 3.0, "耗时 %.2fs" % dur)
        ok("③ 已转发的部分内容仍在", b"partial-answer" in raw, "bytes=%d" % len(raw))
        ok("③ 响应里没有把 HTTP 状态行写进正文", b"HTTP/1.1 502" not in raw)
    finally:
        try:
            side.terminate()
            side.wait(timeout=5)
        except Exception:
            pass
        upstream.shutdown()

    print()
    if fails:
        print("FAILED %d: %s" % (len(fails), fails))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
