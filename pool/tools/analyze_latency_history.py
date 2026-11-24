#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用 pool-requests.jsonl 的全量历史样本分析延迟：是「账号相关」还是「时间偶发」。

每条记录：ts - t0 = 该请求耗时(ms)；账号取 key_want（S5 起两种协议都有），回退到别名 @aN。
"""
import collections
import json
import re
import time

LOG = "/opt/litellm/pool-requests.jsonl"
rows = []
for line in open(LOG, encoding="utf-8", errors="replace"):
    try:
        r = json.loads(line)
    except Exception:
        continue
    dur = (r.get("ts") or 0) - (r.get("t0") or 0)
    if dur <= 0 or dur > 3_600_000:
        continue
    acct = str(r.get("key_want") or "")
    if not acct.startswith("OCG_KEY_"):
        m = re.search(r"@a(\d+)$", str(r.get("model") or ""))
        acct = f"OCG_KEY_{m.group(1)}" if m else "unknown"
    rows.append((r.get("ts") or 0, acct, dur, str(r.get("model") or "")))


def pct(v, q):
    if not v:
        return 0.0
    s = sorted(v)
    return s[min(len(s) - 1, int(len(s) * q))]


print(f"总样本 {len(rows)} 条（全部历史）")
print(f"\n{'账号':<12}{'样本':<7}{'p50':<9}{'p90':<9}{'p99':<9}{'max':<9}")
per = collections.defaultdict(list)
for _ts, a, d, _m in rows:
    per[a].append(d / 1000.0)
for a in sorted(per):
    v = per[a]
    print(f"{a:<12}{len(v):<7}{pct(v, .5):<9.1f}{pct(v, .9):<9.1f}{pct(v, .99):<9.1f}{max(v):<9.1f}")

# 时间桶：每 30 分钟的整体 p90 → 判断"某时段普遍慢"（偶发）
buckets = collections.defaultdict(list)
for ts, _a, d, _m in rows:
    buckets[ts // 1_800_000].append(d / 1000.0)
slow = sorted(((pct(v, .9), k, len(v)) for k, v in buckets.items()), reverse=True)[:8]
print("\n最慢的 8 个 30 分钟时段（整体 p90）：")
for p90, k, n in slow:
    t = time.strftime("%m-%d %H:%M", time.localtime(k * 1800))
    # 该时段内各账号的样本数（看是否集中在某个账号）
    accts = collections.Counter(a for ts, a, _d, _m in rows if ts // 1_800_000 == k)
    print(f"  {t}  p90={p90:6.1f}s  样本={n:<5} 账号分布={dict(accts)}")

print("\n最慢的 10 条请求：")
for ts, a, d, m in sorted(rows, key=lambda x: -x[2])[:10]:
    print(f"  {time.strftime('%m-%d %H:%M:%S', time.localtime(ts/1000))}  {d/1000:7.1f}s  {a:<11} {m}")
