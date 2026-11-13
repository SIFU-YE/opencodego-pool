#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上下文长度 vs 解码速率：用真实流量回归「长上下文是否拖慢 t/s」。

tps = ct/(ts-t0) 含 TTFT；用 ct>=1500 的长回答摊薄 TTFT，并按 cached 比例分层，
把「前缀缓存是否命中」这个 TTFT 主因也分离出来。
"""
import glob
import gzip
import json
import time
from collections import defaultdict

files = sorted(glob.glob("/opt/litellm/pool-requests*"))
rows, seen = [], set()


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
        k = (r.get("ts"), r.get("t0"), r.get("ct"))
        if k in seen:
            continue
        seen.add(k)
        t0, ts, ct = r.get("t0") or 0, r.get("ts") or 0, r.get("ct") or 0
        pt = r.get("pt") or 0
        if not t0 or ts <= t0 or ct <= 0 or pt <= 0:
            continue
        r["_tps"] = ct / ((ts - t0) / 1000.0)
        rows.append(r)

tmax = max(r["ts"] for r in rows)
week = [r for r in rows if r["ts"] > tmax - 7 * 86400 * 1000]
print("总记录 %d，近 7 天 %d" % (len(rows), len(week)))


def pct(xs, p):
    xs = sorted(xs)
    return xs[int(round((len(xs) - 1) * p))]


BUCKETS = [(0, 20000), (20000, 50000), (50000, 100000), (100000, 200000),
           (200000, 300000), (300000, 500000), (500000, 10 ** 9)]

print("\n【A】近 7 天，长回答 ct>=1500，按 prompt 长度分桶")
print("%-16s %6s %8s %8s %8s %9s" % ("pt 区间", "n", "tps_p50", "tps_p90", "avg_ct", "cached%"))
for lo, hi in BUCKETS:
    sel = [r for r in week if lo <= r["pt"] < hi and r["ct"] >= 1500]
    if not sel:
        continue
    xs = [r["_tps"] for r in sel]
    cac = sum((r.get("cached") or 0) / float(r["pt"]) for r in sel) / len(sel) * 100
    print("%-16s %6d %8.1f %8.1f %8.0f %8.0f%%"
          % ("%dk-%dk" % (lo // 1000, hi // 1000 if hi < 10 ** 9 else 999),
             len(xs), pct(xs, .5), pct(xs, .9),
             sum(r["ct"] for r in sel) / len(sel), cac))

print("\n【B】同样分桶，但只看「前缀缓存基本全命中」(cached/pt>=0.9) —— 排除 prefill 影响")
print("%-16s %6s %8s %8s" % ("pt 区间", "n", "tps_p50", "tps_p90"))
for lo, hi in BUCKETS:
    sel = [r for r in week if lo <= r["pt"] < hi and r["ct"] >= 1500
           and (r.get("cached") or 0) >= 0.9 * r["pt"]]
    if not sel:
        continue
    xs = [r["_tps"] for r in sel]
    print("%-16s %6d %8.1f %8.1f"
          % ("%dk-%dk" % (lo // 1000, hi // 1000 if hi < 10 ** 9 else 999),
             len(xs), pct(xs, .5), pct(xs, .9)))

print("\n【C】近 7 天按天，只取 pt 在 100k-300k 且 ct>=1500（可比口径的时间趋势）")
g = defaultdict(list)
for r in week:
    if 100000 <= r["pt"] < 300000 and r["ct"] >= 1500:
        g[time.strftime("%m-%d", time.localtime(r["ts"] / 1000.0))].append(r["_tps"])
print("%-8s %6s %8s %8s" % ("day", "n", "p50", "p90"))
for d in sorted(g):
    xs = g[d]
    print("%-8s %6d %8.1f %8.1f" % (d, len(xs), pct(xs, .5), pct(xs, .9)))

print("\n【D】近 7 天按天，短上下文 pt<50k 且 ct>=1500（对照）")
g = defaultdict(list)
for r in week:
    if r["pt"] < 50000 and r["ct"] >= 1500:
        g[time.strftime("%m-%d", time.localtime(r["ts"] / 1000.0))].append(r["_tps"])
print("%-8s %6s %8s %8s" % ("day", "n", "p50", "p90"))
for d in sorted(g):
    xs = g[d]
    print("%-8s %6d %8.1f %8.1f" % (d, len(xs), pct(xs, .5), pct(xs, .9)))

print("\n【E】ttft 代理：短回答 ct<300 的 ct/(ts-t0) 与 pt 分桶（TTFT 主导）")
print("%-16s %6s %8s" % ("pt 区间", "n", "dur_p50_s"))
for lo, hi in BUCKETS:
    sel = [r for r in week if lo <= r["pt"] < hi and r["ct"] < 300]
    if not sel:
        continue
    du = [(r["ts"] - r["t0"]) / 1000.0 for r in sel]
    print("%-16s %6d %8.2f"
          % ("%dk-%dk" % (lo // 1000, hi // 1000 if hi < 10 ** 9 else 999),
             len(sel), pct(du, .5)))
