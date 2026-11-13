#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""审计 sidecar 的 session 轮换（读 /opt/litellm/session-sidecar.log 的 `ps=` 字段）。

用途：确认 POOL_SESSION_ROTATE 档位在生产上真的按预期工作，而不是"看起来在工作"。

判据：
  · 同一 sess 内 ps 数 == 1  → 同一用户轮次内稳定（turn 档正常）
  · 换一条 user 消息后 ps 变化 → 轮换生效
  · 同一 sess 的所有 ps 账号前缀一致 → 账号粘性未被破坏
  · 每小时出现的 distinct ps 数 ≈ 请求数 → 串足够冷（request 档特征）
    distinct ps 远小于请求数 → 串被复用（tree/off 档特征，可能烧热）

用法：
  python3 analyze_rotate.py            # 最近 200 行
  python3 analyze_rotate.py 1000       # 最近 N 行
  python3 analyze_rotate.py --by-hour  # 按小时统计 distinct ps
"""
import re
import sys
import collections

LOG = "/opt/litellm/session-sidecar.log"
RE_LINE = re.compile(r"\[(\d\d:\d\d):\d\d\] POST \S+ sess=(\S+) status=(\d+) ps=(\S+)")

args = [a for a in sys.argv[1:] if not a.startswith("--")]
N = int(args[0]) if args else 200
BY_HOUR = "--by-hour" in sys.argv

rows = []
for line in open(LOG, encoding="utf-8", errors="replace"):
    m = RE_LINE.search(line)
    if m:
        rows.append((m.group(1), m.group(2), m.group(3), m.group(4)))
rows = rows[-N:]
if not rows:
    sys.exit("日志里还没有 ps= 字段（需先部署带 ps 的 sidecar）")

print("样本 %d 条（%s → %s）" % (len(rows), rows[0][0], rows[-1][0]))

# 1) 按会话：请求数 / distinct ps / 账号前缀是否一致
g = collections.defaultdict(list)
for hm, sess, st, ps in rows:
    g[sess].append(ps)
print("\n%-46s %5s %6s %7s %s" % ("sess", "req", "ps数", "账号数", "判定"))
for s, v in sorted(g.items(), key=lambda kv: -len(kv[1]))[:12]:
    accts = {p.split("-")[1] for p in v if p.startswith("dsh-a")}
    verdict = "单轮稳定" if len(set(v)) == 1 else "有轮换(%d 个串)" % len(set(v))
    if len(accts) > 1:
        verdict += " ⚠账号混用"
    print("%-46s %5d %6d %7d %s" % (s[:46], len(v), len(set(v)), len(accts), verdict))

# 2) 按小时：请求数 / distinct ps / 复用率
if BY_HOUR:
    h = collections.defaultdict(lambda: [0, set()])
    for hm, sess, st, ps in rows:
        h[hm][0] += 1
        h[hm][1].add(ps)
    print("\n%-8s %5s %9s %8s" % ("hour", "req", "distinct", "复用率"))
    for k in sorted(h):
        n, s = h[k]
        print("%-8s %5d %9d %7.1f%%" % (k, n, len(s), 100.0 * (1 - len(s) / float(n))))

# 3) 状态码分布（顺带看错误率）
codes = collections.Counter(r[2] for r in rows)
print("\n状态码:", dict(codes))
