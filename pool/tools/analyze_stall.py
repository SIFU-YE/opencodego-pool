#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""upstream-stall 日志趋势：上游首字节延迟（TTFT）按天统计，用于判断上游是否在劣化。"""
import re
import sys
from collections import defaultdict

PAT = re.compile(r"^(\S+)T(\d\d):\d\d:\d\dZ status=(\w+) 窗口=(\S+) 样本=(\d+) p50=([\d.]+)s p90=([\d.]+)s max=([\d.]+)s 慢\(>60s\)=(\d+)")

by_day = defaultdict(lambda: {"n": 0, "p50": [], "p90": [], "slow": 0, "samples": 0})
for line in open("/opt/litellm/upstream-stall.cron.log", "r", errors="replace"):
    m = PAT.match(line.strip())
    if not m:
        continue
    day, hh, status, win, samples, p50, p90, mx, slow = m.groups()
    if status != "stall":
        continue
    d = by_day[day]
    d["n"] += 1
    d["p50"].append(float(p50))
    d["p90"].append(float(p90))
    d["slow"] += int(slow)
    d["samples"] += int(samples)


def pct(xs, p):
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[int(round((len(xs) - 1) * p))]


print("%-12s %5s %8s %8s %8s %9s %10s" %
      ("day", "win", "p50ofp50", "p50ofp90", "max_p90", "slow>60s", "samples/win"))
for d in sorted(by_day):
    v = by_day[d]
    print("%-12s %5d %8.1f %8.1f %8.1f %9d %10.0f" %
          (d, v["n"], pct(v["p50"], .5), pct(v["p90"], .5),
           max(v["p90"]), v["slow"], v["samples"] / float(v["n"])))
