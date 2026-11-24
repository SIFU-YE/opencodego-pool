#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归测试：sidecar「等上游响应头」的预算 / 遥测 / 有界重发（T1，2026-10-20）。

背景（现场证据）
------------
故障样本里 `pi-ai stream idle timeout after 90000ms` 的共同点是：sidecar 阻塞在
`conn.getresponse()` 上，**在拿到上游响应头之前一个字节都不发**，客户端只能等到
自己的 idle 看门狗。SSE 注释心跳只覆盖「拿到头之后」，对「头之前」无效。

本测试锁死 `_upstream_getresponse()` 的三条行为：
  1. 第一次等待超时 → 记 `upstream-header-timeout attempt=1` → 换连接重发 → 成功；
  2. 两次都超时 → 抛出（由 `_relay` 外层返回 502，客户端按 SERVER 重试并换号）；
  3. 快速成功 →   不产生任何 header 相关日志（不污染日志、不改变正常路径）。

运行（无需真实上游 / 不占端口）：
    python3 pool/tools/test_sidecar_header_wait.py
"""
import importlib.util
import os
import socket
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
POOL = os.path.dirname(HERE)                      # .../pool
sys.path.insert(0, POOL)                          # sidecar 依赖同目录 toolrepair 等

os.environ["POOL_HEADER_TIMEOUT_S"] = "2"
os.environ["POOL_HEADER_WAIT_WARN_S"] = "0"       # 任何等待都算慢，便于断言遥测

spec = importlib.util.spec_from_file_location("sc", os.path.join(POOL, "session-sidecar.py"))
sc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sc)

LOGS = []
sc.slog = lambda msg: LOGS.append(msg)            # 捕获日志，不打屏
CNT = {"n": 0}


class Resp:
    status = 200

    def getheader(self, *a, **k):
        return ""

    def getheaders(self):
        return []


class Sock:
    def settimeout(self, v):
        pass


class Conn:
    """第一次 getresponse 超时，第二次成功。"""

    def __init__(self, *a, **k):
        self.sock = Sock()

    def request(self, *a, **k):
        pass

    def getresponse(self):
        CNT["n"] += 1
        if CNT["n"] == 1:
            raise socket.timeout("timed out")
        return Resp()

    def close(self):
        pass


class SlowConn(Conn):
    def getresponse(self):
        CNT["n"] += 1
        raise socket.timeout("timed out")


class FastConn(Conn):
    def getresponse(self):
        CNT["n"] += 1
        return Resp()


def new_handler():
    h = sc.Handler.__new__(sc.Handler)
    h.path = "/v1/chat/completions"
    h.headers = {}
    return h


def reset(conn_cls):
    LOGS.clear()
    CNT["n"] = 0
    sc.http.client.HTTPConnection = conn_cls      # 重发也走假连接
    return new_handler()


def main():
    cases = 0

    # 场景 1：超时 → 重发 → 成功
    h = reset(Conn)
    resp = sc.Handler._upstream_getresponse(h, Conn(), "POST", b'{"model":"x"}', {}, "sess-A")
    assert resp.status == 200
    assert CNT["n"] == 2, CNT
    assert any("upstream-header-timeout" in x and "attempt=1" in x for x in LOGS), LOGS
    assert any("upstream-header-slow" in x and "sess=sess-A" in x for x in LOGS), LOGS
    cases += 3

    # 场景 2：全部尝试都超时 → 抛出（外层 502）。次数跟随 HEADER_WAIT_ATTEMPTS
    # （2026-10-29 起预算为 3 次，不再写死 2）。
    h = reset(SlowConn)
    try:
        sc.Handler._upstream_getresponse(h, SlowConn(), "POST", b"{}", {}, "sess-B")
        raise AssertionError("应抛出超时")
    except (socket.timeout, TimeoutError):
        pass
    assert CNT["n"] == sc.HEADER_WAIT_ATTEMPTS, (CNT, sc.HEADER_WAIT_ATTEMPTS)
    # 每次超时一条 attempt 日志（末条是 -final，不计入）
    assert sum(1 for x in LOGS if "upstream-header-timeout sess=" in x) == sc.HEADER_WAIT_ATTEMPTS, LOGS
    cases += 2

    # 场景 3：快速成功 → 无 header 日志
    sc.HEADER_WAIT_WARN_S = 20.0
    sc.HEADER_WAIT_TIMEOUT_S = 120.0
    h = reset(FastConn)
    sc.Handler._upstream_getresponse(h, FastConn(), "POST", b"{}", {}, "sess-C")
    assert CNT["n"] == 1, CNT
    assert not any("upstream-header" in x for x in LOGS), LOGS
    cases += 2

    print("header-wait: pass=%d fail=0" % cases)


if __name__ == "__main__":
    main()
