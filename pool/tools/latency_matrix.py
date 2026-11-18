#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""区分「账号相关」还是「时间相关」的缓慢：账号轮转交替采样。

每轮依次请求 a2/a4/a5/a6（同一时刻横向对比），记录耗时与最大静默空档；
若各账号在本轮普遍慢 → 时间相关（上游偶发）；若总是同一个账号慢 → 账号相关。
用法：python3 latency_matrix.py [轮数] [单请求上限秒]
"""
import json
import sys
import time
import urllib.request

rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 4
cap = float(sys.argv[2]) if len(sys.argv) > 2 else 60

master = ""
for line in open("/opt/litellm/.env", encoding="utf-8"):
    if line.startswith("LITELLM_MASTER_KEY="):
        master = line.split("=", 1)[1].strip().strip('"')

tools = [{"type": "function",
          "function": {"name": "get_weather", "description": "查天气",
                       "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                                      "required": ["city"]}}}]
accounts = ["a2", "a4", "a5", "a6"]


def one(acct):
    body = {"model": f"mimo-v2.6-flash@{acct}", "stream": True, "max_tokens": 256,
            "messages": [{"role": "user", "content": "查北京天气，必须调用工具。"}], "tools": tools}
    req = urllib.request.Request("http://127.0.0.1:4001/v1/chat/completions",
                                data=json.dumps(body).encode(),
                                headers={"Authorization": "Bearer " + master,
                                         "Content-Type": "application/json",
                                         "Accept": "text/event-stream"}, method="POST")
    t0 = time.time()
    last = t0
    maxgap = 0.0
    done = False
    try:
        with urllib.request.urlopen(req, timeout=cap) as r:
            for raw in r:
                now = time.time()
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith("data: "):
                    gap = now - last
                    maxgap = max(maxgap, gap)
                    last = now
                    if line == "data: [DONE]":
                        done = True
                        break
                if now - t0 > cap:
                    break
    except Exception as e:
        return time.time() - t0, maxgap, False, type(e).__name__
    return time.time() - t0, maxgap, done, ""


print(f"轮转采样：{rounds} 轮 × {len(accounts)} 账号，单请求上限 {cap:.0f}s")
print(f"{'轮':<4}" + "".join(f"{a:<22}" for a in accounts))
stats = {a: [] for a in accounts}
for r in range(1, rounds + 1):
    cells = []
    for a in accounts:
        wall = time.strftime("%H:%M:%S")
        dur, gap, done, err = one(a)
        stats[a].append(dur)
        mark = "OK" if done else (err or "超时")
        cells.append(f"{dur:5.1f}s/空{gap:4.1f} {mark:<6}")
    print(f"{r:<4}" + "".join(f"{c:<22}" for c in cells) + f"  (轮开始 {wall})")

print("\n各账号汇总：中位/最大耗时")
for a in accounts:
    v = sorted(stats[a])
    med = v[len(v) // 2]
    print(f"  {a}: 中位={med:5.1f}s  最大={v[-1]:5.1f}s  样本={['%.1f' % x for x in stats[a]]}")
