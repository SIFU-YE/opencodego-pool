#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上游静默监控：滚动窗口统计号池请求耗时，识别「偶发长静默」并落状态文件。

背景：上游 mimo 会偶发长时间不吐流分片（历史最慢 1000–2650s，成簇出现），
与账号、模型档位都无关（实测账号轮转采样里 a5 反而最快）。本脚本只做观测与告警。

用法: python3 stall_watch.py [窗口分钟=15] [慢阈值秒=60]
输出: /opt/litellm/upstream-stall.json（状态）+ 异常时追加 /opt/litellm/upstream-stall.log
退出码: 1 = 检测到静默簇（便于 cron/外部告警链），否则 0
"""
import json
import re
import sys
import time

LOG = "/opt/litellm/pool-requests.jsonl"
STATE = "/opt/litellm/upstream-stall.json"
HIST = "/opt/litellm/upstream-stall.log"

win_min = float(sys.argv[1]) if len(sys.argv) > 1 else 15.0
slow_s = float(sys.argv[2]) if len(sys.argv) > 2 else 60.0

now_ms = time.time() * 1000
lo = now_ms - win_min * 60_000

durs = []
per_acct = {}
for line in open(LOG, encoding="utf-8", errors="replace"):
    try:
        r = json.loads(line)
    except Exception:
        continue
    ts = r.get("ts") or 0
    if ts < lo:
        continue
    d = (ts - (r.get("t0") or ts)) / 1000.0
    if d <= 0 or d > 3600:
        continue
    durs.append(d)
    acct = str(r.get("key_want") or "")
    if not acct.startswith("OCG_KEY_"):
        m = re.search(r"@a(\d+)$", str(r.get("model") or ""))
        acct = f"OCG_KEY_{m.group(1)}" if m else "unknown"
    per_acct.setdefault(acct, []).append(d)


def pct(v, q):
    if not v:
        return 0.0
    s = sorted(v)
    return s[min(len(s) - 1, int(len(s) * q))]


p50 = pct(durs, .5)
p90 = pct(durs, .9)
p99 = pct(durs, .99)
mx = max(durs) if durs else 0.0
slow = sum(1 for d in durs if d > slow_s)

if not durs:
    status = "idle"
elif slow >= 5 or (durs and p90 > slow_s):
    status = "stall"
elif slow >= 3:
    status = "suspected"
else:
    status = "ok"

doc = {
    "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "window_min": win_min,
    "slow_threshold_s": slow_s,
    "samples": len(durs),
    "p50_s": round(p50, 2),
    "p90_s": round(p90, 2),
    "p99_s": round(p99, 2),
    "max_s": round(mx, 2),
    "slow_count": slow,
    "status": status,
    "per_account_p90_s": {k: round(pct(v, .9), 2) for k, v in sorted(per_acct.items())},
    "per_account_samples": {k: len(v) for k, v in sorted(per_acct.items())},
    "note": "上游偶发静默；详见 tools/analyze_latency_history.py 与 docs/运维约定.md",
}
with open(STATE, "w", encoding="utf-8") as fh:
    json.dump(doc, fh, ensure_ascii=False, indent=2)

line = (f"{doc['checked_at']} status={status} 窗口={win_min:g}min 样本={doc['samples']} "
        f"p50={doc['p50_s']:.1f}s p90={doc['p90_s']:.1f}s max={doc['max_s']:.1f}s 慢(>{slow_s:g}s)={slow}")
print(line)
if status in ("suspected", "stall"):
    with open(HIST, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
sys.exit(1 if status == "stall" else 0)
