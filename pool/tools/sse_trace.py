#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""流式载荷逐块到达明细：不聚合成 tps，而是打印「每次 socket read 的到达时刻/字节数/含几个 SSE delta」。

用途：判断「一段一段」的颗粒度到底出现在哪一跳。用法:
    python3 sse_trace.py <target> [max_tokens]
    例: python3 sse_trace.py ll 300
        python3 sse_trace.py up4+np 300
输出前 N 条：t(ms相对首token)  bytes  n_events  first_chars
"""
import sys
import sse_probe as P

N = 60


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "ll"
    max_tok = sys.argv[2] if len(sys.argv) > 2 else "300"
    import os
    os.environ["PROBE_MAXTOK"] = max_tok
    os.environ["PROBE_USAGE"] = "1"
    r = P.one_run(target)
    print("target=%s status=%s tps=%s dur_ms=%s events=%s reads=%s ev_per_read=%s batched=%s"
          % (r.get("target"), r.get("status"), r.get("tps"), r.get("dur_ms"),
             r.get("events"), r.get("reads"), r.get("ev_per_read"), r.get("batched_ratio")))
    print("profile(每秒字符): %s" % (r.get("profile"),))
    print("trace(相邻事件间隔, ms): %s" % (r.get("trace"),))


if __name__ == "__main__":
    main()
