#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同一天不同账号的长回答吞吐对比（判断「账号差异」还是「时间/上游差异」）。

只看 ct>=1500 且 pt 100k-300k（同口径），每格需 n>=15 才打印。
"""
import glob
import gzip
import json
import time
from collections import defaultdict

files = sorted(glob.glob("/opt/litellm/pool-requests*"))
g = defaultdict(list)


def lines(path):
    op = gzip.open if path.endswith(".gz") else open
    with op(path, "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            yield line


for p in files:
    for line in lines(p):
        try:
            r = json.loads(line)
        except Exception:
            continue
        t0, ts, ct = r.get("t0") or 0, r.get("ts") or 0, r.get("ct") or 0
        pt = r.get("pt") or 0
        if not t0 or ts <= t0 or ct < 1500 or not (100000 <= pt < 300000):
            continue
        day = time.strftime("%m-%d", time.localtime(ts / 1000.0))
        key = str(r.get("keyRef") or "")[-8:] or "(none)"
        g[(day, key)].append(ct / ((ts - t0) / 1000.0))


def pct(xs, p):
    xs = sorted(xs)
    return xs[int(round((len(xs) - 1) * p))]


days = sorted(set(d for d, _ in g))
print("%-6s %-10s %5s %8s %8s" % ("day", "key", "n", "p50", "p90"))
for d in days:
    rows = [(k, v) for (dd, k), v in g.items() if dd == d and len(v) >= 15]
    for k, v in sorted(rows, key=lambda kv: -len(kv[1])):
        print("%-6s %-10s %5d %8.1f %8.1f" % (d, k, len(v), pct(v, .5), pct(v, .9)))
