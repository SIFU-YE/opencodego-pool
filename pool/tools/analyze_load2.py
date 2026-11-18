#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""轻量：每天请求量 / 账号数 / 头部账号占比（流式聚合，不保存全部行）。"""
import glob
import gzip
import json
import time
from collections import defaultdict, Counter

files = sorted(glob.glob("/opt/litellm/pool-requests*"))
reqs = Counter()
keys = defaultdict(Counter)
long_tps = defaultdict(list)


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
        if not t0 or ts <= t0 or ct <= 0:
            continue
        day = time.strftime("%m-%d", time.localtime(ts / 1000.0))
        reqs[day] += 1
        keys[day][str(r.get("keyRef") or "")[-8:]] += 1
        if ct >= 1500 and 100000 <= (r.get("pt") or 0) < 300000:
            long_tps[day].append(ct / ((ts - t0) / 1000.0))


def pct(xs, p):
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[int(round((len(xs) - 1) * p))]


print("%-6s %7s %5s %6s %9s %6s  %s" % ("day", "reqs", "acct", "top%", "p50_tps", "n_long", "top3"))
for d in sorted(reqs):
    kc = keys[d]
    top = kc.most_common(1)[0][1] / float(reqs[d]) * 100
    xs = long_tps[d]
    print("%-6s %7d %5d %5.0f%% %9.1f %6d  %s"
          % (d, reqs[d], len(kc), top, pct(xs, .5), len(xs),
             ",".join("%s:%d" % (k, v) for k, v in kc.most_common(3))))
