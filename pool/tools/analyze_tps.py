#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用新观测字段（tps/ttft_ms/chunks/gen_ms）看实时质量：按天/小时/账号给出 p50·p90。

只统计带 `tps` 字段的记录（2026-10-24 之后的新记录），老记录自动跳过。

用法：
    python3 analyze_tps.py            # 近 2 天，按小时
    python3 analyze_tps.py --day      # 按天
    python3 analyze_tps.py --acct     # 按账号（keyRef 尾 8 位）
    python3 analyze_tps.py --min-chars 500
"""
import glob
import gzip
import json
import sys
import time
from collections import defaultdict

ARGS = sys.argv[1:]
BY_DAY = "--day" in ARGS
BY_ACCT = "--acct" in ARGS
MIN_CT = 100
if "--min-ct" in ARGS:
    MIN_CT = int(ARGS[ARGS.index("--min-ct") + 1])


def lines(path):
    op = gzip.open if path.endswith(".gz") else open
    with op(path, "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            yield line


rows = []
for p in sorted(glob.glob("/opt/litellm/pool-requests*")):
    for line in lines(p):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("tps") is None or (r.get("ct") or 0) < MIN_CT:
            continue
        rows.append(r)

if not rows:
    sys.exit("还没有带 tps 的记录（2026-10-24 之后的新记录才带）")


def pct(xs, p):
    xs = sorted(xs)
    return xs[int(round((len(xs) - 1) * p))]


print("带 tps 的新记录：%d 条（ct>=%d）" % (len(rows), MIN_CT))
print("%-12s %6s %8s %8s %9s %9s %8s" %
      ("窗口", "n", "tps_p50", "tps_p90", "ttft_p50", "ttfv_p50", "chk_p50"))

g = defaultdict(list)
for r in rows:
    if BY_ACCT:
        k = str(r.get("keyRef") or "")[-8:]
    elif BY_DAY:
        k = time.strftime("%m-%d", time.localtime(r["ts"] / 1000.0))
    else:
        k = time.strftime("%m-%d %H", time.localtime(r["ts"] / 1000.0))
    g[k].append(r)

for k in sorted(g):
    rs = g[k]
    tps = [r["tps"] for r in rs]
    ttft = [r["ttft_ms"] for r in rs if r.get("ttft_ms") is not None]
    ttfv = [r["ttfv_ms"] for r in rs if r.get("ttfv_ms") is not None]
    chk = [r["chunks"] for r in rs if r.get("chunks")]
    print("%-12s %6d %8.1f %8.1f %9s %9s %8s" %
          (k, len(rs), pct(tps, .5), pct(tps, .9),
           pct(ttft, .5) if ttft else "-", pct(ttfv, .5) if ttfv else "-",
           pct(chk, .5) if chk else "-"))
